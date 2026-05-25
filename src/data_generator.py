from __future__ import annotations

import json
import logging
import math
import os
import random
from typing import Dict, Generator, List

import pandas as pd

from .langfuse_client import observe, get_langfuse
from .schema_parser import Column, ForeignKey, Table, topological_sort

logger = logging.getLogger(__name__)

BATCH_SIZE = 50


def _gemini_client():
    from google import genai  # type: ignore

    project = os.environ.get("GOOGLE_CLOUD_PROJECT", "")
    location = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
    api_key = os.environ.get("GOOGLE_API_KEY", "")

    if project:
        logger.info("Gemini client: Vertex AI (project=%s, location=%s)", project, location)
        return genai.Client(vertexai=True, project=project, location=location)
    if api_key:
        logger.info("Gemini client: AI Studio (API key)")
        return genai.Client(api_key=api_key)
    raise RuntimeError(
        "Set GOOGLE_CLOUD_PROJECT (Vertex AI) or GOOGLE_API_KEY (AI Studio)"
    )


class DataGenerator:
    def __init__(self, temperature: float = 0.8, max_tokens: int = 8192) -> None:
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.model = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
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
        logger.info(
            "Starting generation: %d table(s), %d rows each, order=%s",
            len(tables), num_rows, order,
        )
        generated: Dict[str, pd.DataFrame] = {}
        for name in order:
            if name not in tables:
                continue
            df = self._generate_table(tables[name], num_rows, generated, user_prompt)
            generated[name] = df
            logger.info("Table '%s' generated: %d rows", name, len(df))
            yield name, df
        logger.info("Generation complete for all %d table(s)", len(generated))

    @observe(as_type="generation", name="modify-table", capture_input=False)
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
        logger.info("Modifying table '%s': instruction=%r", table.name, instruction[:120])
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
            lf = get_langfuse()
            if lf:
                lf.update_current_generation(
                    model=self.model,
                    model_parameters={"temperature": 0.3, "max_output_tokens": self.max_tokens},
                    input=prompt,
                    output=resp.text or "",
                    metadata={"table": table.name, "instruction": instruction[:200]},
                )
            rows = json.loads(resp.text)
            if isinstance(rows, list) and rows:
                logger.info("Table '%s' modified: %d rows returned", table.name, len(rows))
                return pd.DataFrame(rows)
            logger.warning("modify_table: Gemini returned empty/non-list response for '%s'", table.name)
        except Exception:
            logger.error("modify_table failed for table '%s'", table.name, exc_info=True)
        return df

    # ------------------------------------------------------------------ #
    #  Internal helpers
    # ------------------------------------------------------------------ #

    @observe(as_type="span", name="generate-table", capture_input=False)
    def _generate_table(
        self,
        table: Table,
        num_rows: int,
        existing: Dict[str, pd.DataFrame],
        user_prompt: str,
    ) -> pd.DataFrame:
        fk_values: Dict[str, List] = {}
        for fk in table.foreign_keys:
            if fk.ref_table in existing:
                ref_df = existing[fk.ref_table]
                if fk.ref_column in ref_df.columns:
                    fk_values[fk.column] = ref_df[fk.ref_column].dropna().tolist()
            else:
                logger.debug(
                    "Table '%s': FK ref '%s' not yet generated — column '%s' may be NULL",
                    table.name, fk.ref_table, fk.column,
                )

        num_batches = math.ceil(num_rows / BATCH_SIZE)
        logger.info(
            "Generating table '%s': %d rows in %d batch(es)", table.name, num_rows, num_batches
        )

        all_rows: List[dict] = []
        for start in range(0, num_rows, BATCH_SIZE):
            count = min(BATCH_SIZE, num_rows - start)
            batch = self._generate_batch(table, count, fk_values, user_prompt, id_offset=start)
            all_rows.extend(batch)
            logger.debug("Table '%s': batch offset=%d, got %d rows", table.name, start, len(batch))

        return pd.DataFrame(all_rows)

    @observe(as_type="generation", name="generate-batch", capture_input=False)
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
            lf = get_langfuse()
            if lf:
                lf.update_current_generation(
                    model=self.model,
                    model_parameters={
                        "temperature": self.temperature,
                        "max_output_tokens": self.max_tokens,
                    },
                    input=prompt,
                    output=resp.text or "",
                    metadata={"table": table.name, "count": count, "offset": id_offset},
                )
            rows = json.loads(resp.text)
            if not isinstance(rows, list):
                logger.warning(
                    "Gemini returned non-list JSON for '%s'; trying 'data'/'rows' keys",
                    table.name,
                )
                rows = rows.get("data", rows.get("rows", []))
        except Exception:
            logger.error(
                "Gemini generation failed for table '%s' (offset=%d, count=%d) — using fallback",
                table.name, id_offset, count,
                exc_info=True,
            )
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
