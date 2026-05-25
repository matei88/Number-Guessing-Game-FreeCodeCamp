from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go

from .database import DatabaseManager
from .langfuse_client import LangfuseTracker
from .schema_parser import Table

logger = logging.getLogger(__name__)


class TalkToDataManager:
    def __init__(self, db_manager: DatabaseManager) -> None:
        self.db = db_manager
        self.tracker = LangfuseTracker()
        self.model = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
        self._client = None

    @property
    def client(self):
        if self._client is None:
            from google import genai  # type: ignore

            project = os.environ.get("GOOGLE_CLOUD_PROJECT", "")
            location = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
            api_key = os.environ.get("GOOGLE_API_KEY", "")
            if project:
                self._client = genai.Client(vertexai=True, project=project, location=location)
            elif api_key:
                self._client = genai.Client(api_key=api_key)
            else:
                raise RuntimeError("No Google credentials configured.")
        return self._client

    def query(self, question: str, schema_tables: Dict[str, Table]) -> Dict[str, Any]:
        from google.genai import types  # type: ignore

        schema_desc = self.db.get_schema_description()

        execute_sql_decl = types.FunctionDeclaration(
            name="execute_sql",
            description=(
                "Execute a SQL SELECT query against the PostgreSQL database "
                "and return the results."
            ),
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "sql": types.Schema(
                        type=types.Type.STRING,
                        description="Valid PostgreSQL SELECT statement.",
                    ),
                    "visualization": types.Schema(
                        type=types.Type.STRING,
                        description=(
                            "Best chart type: 'table', 'bar', 'line', 'pie', "
                            "'scatter', or 'none' for a single scalar answer."
                        ),
                    ),
                    "explanation": types.Schema(
                        type=types.Type.STRING,
                        description="One-sentence plain-English summary of what the query does.",
                    ),
                },
                required=["sql", "visualization", "explanation"],
            ),
        )

        prompt = (
            "You are a data analyst. The user has a PostgreSQL database with the "
            "following schema:\n\n"
            f"{schema_desc}\n\n"
            f"User question: {question}\n\n"
            "Call execute_sql with a valid PostgreSQL SELECT query that answers the question. "
            "Choose the most informative visualization type."
        )

        logger.info("Talk-to-data query: %r", question)
        try:
            resp = self.client.models.generate_content(
                model=self.model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.1,
                    tools=[types.Tool(function_declarations=[execute_sql_decl])],
                ),
            )

            for part in resp.candidates[0].content.parts:
                if hasattr(part, "function_call") and part.function_call:
                    fc = part.function_call
                    sql: str = fc.args.get("sql", "")
                    viz: str = fc.args.get("visualization", "table")
                    explanation: str = fc.args.get("explanation", "")

                    logger.debug("Generated SQL (viz=%s): %s", viz, sql)

                    try:
                        df = self.db.execute_query(sql)
                    except Exception:
                        logger.error("SQL execution failed for query: %s", sql, exc_info=True)
                        # re-read exc for the user-facing message
                        import sys
                        exc_msg = str(sys.exc_info()[1])
                        return {
                            "type": "text",
                            "content": f"SQL error: {exc_msg}\n\n```sql\n{sql}\n```",
                        }

                    self.tracker.track_generation(
                        name="talk_to_data",
                        model=self.model,
                        input_text=question,
                        output_text=sql,
                        metadata={"sql": sql, "rows": len(df), "viz": viz},
                    )

                    if df.empty:
                        logger.info("Query returned 0 rows")
                        return {
                            "type": "text",
                            "content": f"{explanation}\n\n*No results found.*",
                        }

                    logger.info("Query returned %d rows, viz=%s", len(df), viz)

                    if viz == "none" or (len(df) == 1 and len(df.columns) == 1):
                        val = df.iloc[0, 0]
                        return {
                            "type": "text",
                            "content": f"{explanation}\n\n**Result:** {val}",
                            "sql": sql,
                        }

                    if viz != "table":
                        chart = self._make_chart(df, viz, question)
                        if chart:
                            return {
                                "type": "plot",
                                "content": chart,
                                "explanation": explanation,
                                "sql": sql,
                            }

                    return {
                        "type": "table",
                        "content": df,
                        "explanation": explanation,
                        "sql": sql,
                    }

            # No function call — return raw text
            logger.warning("Gemini did not produce a function call for question: %r", question)
            text_resp = getattr(resp, "text", "") or "I couldn't generate a query for that."
            return {"type": "text", "content": text_resp}

        except Exception:
            logger.error("talk_to_data query() failed for question: %r", question, exc_info=True)
            import sys
            return {"type": "text", "content": f"Error: {sys.exc_info()[1]}"}

    def stream_query(self, question: str):
        """Yield text chunks for streaming the analysis narrative."""
        from google.genai import types  # type: ignore

        schema_desc = self.db.get_schema_description()
        prompt = (
            f"Database schema:\n{schema_desc}\n\n"
            f"Question: {question}\n\n"
            "Write a concise analytical answer. If SQL is needed, show it in a code block."
        )
        logger.info("Streaming query: %r", question)
        try:
            for chunk in self.client.models.generate_content_stream(
                model=self.model,
                contents=prompt,
                config=types.GenerateContentConfig(temperature=0.2),
            ):
                if chunk.text:
                    yield chunk.text
        except Exception:
            logger.error("stream_query failed for question: %r", question, exc_info=True)
            import sys
            yield f"\n\n*Error: {sys.exc_info()[1]}*"

    # ------------------------------------------------------------------ #

    def _make_chart(
        self, df: pd.DataFrame, viz: str, title: str
    ) -> Optional[go.Figure]:
        try:
            cols = list(df.columns)
            if viz == "bar" and len(cols) >= 2:
                return px.bar(df, x=cols[0], y=cols[1], title=title)
            if viz == "line" and len(cols) >= 2:
                return px.line(df, x=cols[0], y=cols[1], title=title)
            if viz == "pie" and len(cols) >= 2:
                return px.pie(df, names=cols[0], values=cols[1], title=title)
            if viz == "scatter" and len(cols) >= 2:
                return px.scatter(df, x=cols[0], y=cols[1], title=title)
        except Exception:
            logger.error("Chart creation failed (viz=%s, columns=%s)", viz, list(df.columns), exc_info=True)
        return None
