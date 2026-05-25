from __future__ import annotations

import logging
import os
import re
from typing import List, Optional

import pandas as pd
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)


def _mask_url(url: str) -> str:
    """Hide password in a DB URL for safe logging."""
    return re.sub(r"(?<=://)[^:]+:[^@]+@", "***:***@", url)


class DatabaseManager:
    def __init__(self, db_url: str) -> None:
        self.db_url = db_url
        self.engine: Optional[Engine] = None
        self._connect()

    def _connect(self) -> None:
        logger.info("Connecting to database: %s", _mask_url(self.db_url))
        self.engine = create_engine(self.db_url, pool_pre_ping=True)
        with self.engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        logger.info("Database connection established")

    def store_dataframe(self, df: pd.DataFrame, table_name: str) -> None:
        if self.engine is None:
            logger.warning("store_dataframe called but engine is None — skipping %s", table_name)
            return
        try:
            logger.debug("Storing %d rows into table '%s'", len(df), table_name)
            df.to_sql(table_name.lower(), self.engine, if_exists="replace", index=False)
            logger.info("Stored %d rows into table '%s'", len(df), table_name)
        except Exception as exc:
            logger.error("Failed to store table '%s'", table_name, exc_info=True)

    def execute_query(self, sql: str) -> pd.DataFrame:
        if self.engine is None:
            raise RuntimeError("No database connection")
        logger.debug("Executing query: %s", sql[:500])
        try:
            with self.engine.connect() as conn:
                result = conn.execute(text(sql))
                df = pd.DataFrame(result.fetchall(), columns=list(result.keys()))
            logger.debug("Query returned %d rows", len(df))
            return df
        except Exception:
            logger.error("Query execution failed: %s", sql[:500], exc_info=True)
            raise

    def get_table_names(self) -> List[str]:
        if self.engine is None:
            return []
        try:
            inspector = inspect(self.engine)
            return inspector.get_table_names()
        except Exception:
            logger.error("Failed to list table names", exc_info=True)
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
            logger.error("Failed to read schema description", exc_info=True)
            return f"(schema read error: {exc})"

    @staticmethod
    def from_env() -> "DatabaseManager":
        url = os.environ.get(
            "DATABASE_URL",
            "postgresql://postgres:postgres@localhost:5432/data_assistant",
        )
        return DatabaseManager(url)
