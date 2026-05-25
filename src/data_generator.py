from __future__ import annotations

import json
import os
import random
from typing import Dict, Generator, List

import pandas as pd

from .langfuse_client import LangfuseTracker
from .schema_parser import Column, ForeignKey, Table, topological_sort

BATCH_SIZE = 50


def _gemini_client():
    from google import genai  # type: ignore

    project = os.environ.get("GOOGLE_CLOUD_PROJECT", "")
    location = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
    api_key = os.environ.get("GOOGLE_API_KEY", "")

    if project:
        return genai.Client(vertexai=True, project=project, location=location)
    if api_key:
        return genai.Client(api_key=api_key)
    raise RuntimeError(
        "Set GOOGLE_CLOUD_PROJECT (Vertex AI) or GOOGLE_API_KEY (AI Studio)"
    )


class DataGenerator:
    def __init__(self, temperature: float = 0.8, max_tokens: int = 8192) -> None:
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.model = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
        self.tracker = LangfuseTracker()
        self._client = None  # lazy init

    @property
    def client(self):
        if self._client is None:
            self._client = _gemini_client()
        return self._client

    # ------------------------------------------------------------------ #
    #  Public API
    # ------------------------------------------------------------------ #

    def generate_all(
        self,
        tables: Dict[str, Table],
        num_rows: int,
        user_prompt: str = "",
    ) -> Generator[tuple[str, pd.DataFrame], None, None]:
        """Yield (table_name, DataFrame) pairs in dependency order."""
        order = topological_sort(tables)
        generated: Dict[str, pd.DataFrame] = {}
        for name in order:
            if name not in tables:
                continue
            df = self._generate_table(tables[name], num_rows, generated, user_prompt)
            generated[name] = df
            yield name, df

    def modify_table(
        self,
        df: pd.DataFrame,
        table: Table,
        instruction: str,
    ) -> pd.DataFrame:
        from google.genai import types  # type: ignore

        sample = df.head(min(10, len(df))).to_dict(orient="records")
        prompt = (
            f"You are modifying synthetic data for the SQL table '{table.name}'.\n\n"
            f"Sample of current data (first {len(sample)} rows):\n"
            f"{json.dumps(sample, indent=2, default=str)}\n\n"
            f"Total rows: {len(df)}\n"
            f"Modification instruction: {instruction}\n\n"
            f"Apply the modification to ALL {len(df)} rows. "
            f"Return a JSON array with the complete modified dataset. "
            f"Keep all columns. Only change what the instruction specifies."
        )
        try:
            resp = self.client.models.generate_content(
                model=self.model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.3,
                    max_output_tokens=self.max_tokens,
                    response_mime_type="application/json",
                ),
            )
            rows = json.loads(resp.text)
            if isinstance(rows, list) and rows:
                return pd.DataFrame(rows)
        except Exception as exc:
            print(f"[DataGenerator] modify error: {exc}")
        return df

    # ------------------------------------------------------------------ #
    #  Internal helpers
    # ------------------------------------------------------------------ #

    def _generate_table(
        self,
        table: Table,
        num_rows: int,
        existing: Dict[str, pd.DataFrame],
        user_prompt: str,
    ) -> pd.DataFrame:
        fk_map = {fk.column: fk for fk in table.foreign_keys}
        fk_values: Dict[str, List] = {}
        for fk in table.foreign_keys:
            if fk.ref_table in existing:
                ref_df = existing[fk.ref_table]
                if fk.ref_column in ref_df.columns:
                    fk_values[fk.column] = ref_df[fk.ref_column].dropna().tolist()

        all_rows: List[dict] = []
        for start in range(0, num_rows, BATCH_SIZE):
            count = min(BATCH_SIZE, num_rows - start)
            batch = self._generate_batch(table, count, fk_values, user_prompt, id_offset=start)
            all_rows.extend(batch)

        return pd.DataFrame(all_rows)

    def _generate_batch(
        self,
        table: Table,
        count: int,
        fk_values: Dict[str, List],
        user_prompt: str,
        id_offset: int,
    ) -> List[dict]:
        from google.genai import types  # type: ignore

        col_lines: List[str] = []
        for col in table.columns:
            if col.auto_increment:
                continue
            line = f"- {col.name} ({col.sql_type})"
            if not col.nullable:
                line += " NOT NULL"
            if col.enum_values:
                line += f" — must be one of: {col.enum_values}"
            if col.check_constraint:
                line += f" — constraint: {col.check_constraint}"
            if col.name in fk_values:
                sample = fk_values[col.name][:20]
                line += f" — FK, choose from: {sample}"
            col_lines.append(line)

        fk_section = ""
        if fk_values:
            fk_lines = [
                f"  {c}: randomly pick from {vs[:10]}..."
                for c, vs in fk_values.items()
                if vs
            ]
            if fk_lines:
                fk_section = "Foreign key constraints (use ONLY listed values):\n" + "\n".join(fk_lines)

        prompt = (
            f"Generate exactly {count} rows of realistic, diverse synthetic data "
            f"for the SQL table '{table.name}'.\n\n"
            f"Columns (skip auto-increment PKs):\n"
            + "\n".join(col_lines)
            + (f"\n\n{fk_section}" if fk_section else "")
            + (f"\n\nAdditional instructions: {user_prompt}" if user_prompt else "")
            + "\n\nRules:\n"
            "1. Use realistic, varied values — no 'Name 1', 'Name 2' placeholders.\n"
            "2. Respect ENUM lists exactly.\n"
            "3. Respect CHECK constraints.\n"
            "4. For FK columns use ONLY the listed values; NULL is OK if the column is nullable and no FK values exist.\n"
            "5. Dates must be realistic and past-tense where appropriate.\n"
            "6. Make data culturally diverse.\n"
            f"\nReturn ONLY a JSON array of {count} objects with the non-auto-increment column names as keys."
        )

        try:
            resp = self.client.models.generate_content(
                model=self.model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=self.temperature,
                    max_output_tokens=self.max_tokens,
                    response_mime_type="application/json",
                ),
            )
            self.tracker.track_generation(
                name=f"generate_{table.name}",
                model=self.model,
                input_text=prompt,
                output_text=resp.text or "",
                metadata={"table": table.name, "count": count},
            )
            rows = json.loads(resp.text)
            if not isinstance(rows, list):
                rows = rows.get("data", rows.get("rows", []))
        except Exception as exc:
            print(f"[DataGenerator] Gemini error for {table.name}: {exc}")
            rows = self._fallback_batch(table, count, fk_values)

        pk = table.primary_key_column
        for i, row in enumerate(rows):
            if pk:
                row[pk] = id_offset + i + 1
        return rows

    def _fallback_batch(
        self,
        table: Table,
        count: int,
        fk_values: Dict[str, List],
    ) -> List[dict]:
        rows = []
        for i in range(count):
            row: dict = {}
            for col in table.columns:
                if col.auto_increment:
                    continue
                if col.name in fk_values and fk_values[col.name]:
                    row[col.name] = random.choice(fk_values[col.name])
                elif col.enum_values:
                    row[col.name] = random.choice(col.enum_values)
                elif not col.nullable:
                    row[col.name] = f"{col.name}_{i + 1}"
                else:
                    row[col.name] = None
            rows.append(row)
        return rows
