from __future__ import annotations

import os
from typing import List, Optional

import pandas as pd
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine


class DatabaseManager:
    def __init__(self, db_url: str) -> None:
        self.db_url = db_url
        self.engine: Optional[Engine] = None
        self._connect()

    def _connect(self) -> None:
        self.engine = create_engine(self.db_url, pool_pre_ping=True)
        with self.engine.connect() as conn:
            conn.execute(text("SELECT 1"))

    def store_dataframe(self, df: pd.DataFrame, table_name: str) -> None:
        if self.engine is None:
            return
        try:
            df.to_sql(table_name.lower(), self.engine, if_exists="replace", index=False)
        except Exception as exc:
            print(f"[DB] error storing {table_name}: {exc}")

    def execute_query(self, sql: str) -> pd.DataFrame:
        if self.engine is None:
            raise RuntimeError("No database connection")
        with self.engine.connect() as conn:
            result = conn.execute(text(sql))
            return pd.DataFrame(result.fetchall(), columns=list(result.keys()))

    def get_table_names(self) -> List[str]:
        if self.engine is None:
            return []
        try:
            inspector = inspect(self.engine)
            return inspector.get_table_names()
        except Exception:
            return []

    def has_data(self) -> bool:
        return len(self.get_table_names()) > 0

    def get_schema_description(self) -> str:
        if self.engine is None:
            return ""
        try:
            inspector = inspect(self.engine)
            parts = []
            for tname in inspector.get_table_names():
                cols = inspector.get_columns(tname)
                col_strs = [f"  {c['name']} ({c['type']})" for c in cols]
                parts.append(f"Table: {tname}\n" + "\n".join(col_strs))
            return "\n\n".join(parts)
        except Exception as exc:
            return f"(schema read error: {exc})"

    @staticmethod
    def from_env() -> "DatabaseManager":
        url = os.environ.get(
            "DATABASE_URL",
            "postgresql://postgres:postgres@localhost:5432/data_assistant",
        )
        return DatabaseManager(url)
