"""Data Assistant — Streamlit application.

Phase 1: Synthetic data generation from a DDL schema.
Phase 2/3: Natural-language querying of the generated data.
"""

from __future__ import annotations

import io
import logging
import os
import zipfile
from pathlib import Path

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

load_dotenv()

from src.logging_config import setup_logging

setup_logging()

logger = logging.getLogger(__name__)

from src.database import DatabaseManager
from src.schema_parser import parse_ddl, topological_sort

# ── page config ──────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Data Assistant",
    page_icon="🗃️",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
    <style>
        [data-testid="stSidebar"] { min-width: 220px; max-width: 220px; }
        .block-container { padding-top: 1.5rem; }
        h3 { margin-bottom: 0.3rem; }
        div[data-testid="stDataFrame"] { border-radius: 6px; }
        .sql-block { font-size: 0.78rem; color: #555; }
    </style>
    """,
    unsafe_allow_html=True,
)

# ── session state defaults ────────────────────────────────────────────────────
_defaults = {
    "generated_data": {},       # table_name → pd.DataFrame
    "schema_tables": {},        # table_name → Table
    "ddl_content": None,
    "messages": [],             # chat history for Talk-to-data
    "db": None,
    "db_error": None,
}
for k, v in _defaults.items():
    if k not in st.session_state:
        st.session_state[k] = v


# ── DB connection (lazy) ──────────────────────────────────────────────────────
def get_db() -> DatabaseManager | None:
    if st.session_state.db is not None:
        return st.session_state.db
    if st.session_state.db_error:
        return None
    try:
        st.session_state.db = DatabaseManager.from_env()
    except Exception:
        logger.error("Database connection failed", exc_info=True)
        import sys
        st.session_state.db_error = str(sys.exc_info()[1])
    return st.session_state.db


# ── sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("## Data Assistant")
    st.markdown("---")
    page = st.radio(
        "nav",
        ["📊  Data Generation", "💬  Talk to your data"],
        label_visibility="collapsed",
    )

    st.markdown("---")
    db = get_db()
    if db:
        st.success("DB connected", icon="✅")
    else:
        err = st.session_state.db_error or "Not connected"
        st.warning(f"DB: {err}", icon="⚠️")

    # Quick-load sample schemas
    st.markdown("#### Sample schemas")
    schemas_dir = Path(__file__).parent / "schemas"
    for ddl_file in sorted(schemas_dir.glob("*.ddl")):
        if st.button(ddl_file.stem, use_container_width=True):
            try:
                st.session_state.ddl_content = ddl_file.read_text()
                st.session_state.schema_tables = parse_ddl(st.session_state.ddl_content)
                st.session_state.generated_data = {}
                logger.info("Sample schema loaded: %s (%d tables)", ddl_file.stem, len(st.session_state.schema_tables))
            except Exception:
                logger.error("Failed to load sample schema: %s", ddl_file.stem, exc_info=True)
            st.rerun()

# =============================================================================
#  PAGE 1 — DATA GENERATION
# =============================================================================
if "Data Generation" in page:

    # ── prompt ────────────────────────────────────────────────────────────────
    st.subheader("Prompt")
    user_prompt = st.text_area(
        "prompt",
        placeholder="Enter your prompt here…",
        height=80,
        label_visibility="collapsed",
    )

    # ── upload ────────────────────────────────────────────────────────────────
    col_btn, col_hint = st.columns([2, 5])
    with col_btn:
        uploaded = st.file_uploader(
            "Upload DDL Schema",
            type=["sql", "txt", "ddl"],
            label_visibility="collapsed",
        )
    with col_hint:
        st.markdown(
            "<span style='color:#888; font-size:0.9rem;'>Supported formats: SQL, JSON, DDL</span>",
            unsafe_allow_html=True,
        )

    if uploaded:
        try:
            content = uploaded.read().decode("utf-8")
            if content != st.session_state.ddl_content:
                st.session_state.ddl_content = content
                st.session_state.schema_tables = parse_ddl(content)
                st.session_state.generated_data = {}
                logger.info("DDL uploaded: %s — %d table(s) parsed", uploaded.name, len(st.session_state.schema_tables))
        except Exception:
            logger.error("Failed to parse uploaded DDL: %s", uploaded.name, exc_info=True)
            st.error("Could not parse the uploaded file. Check that it is valid SQL/DDL.")

    if st.session_state.schema_tables:
        names = list(st.session_state.schema_tables.keys())
        st.caption(f"Schema loaded — {len(names)} tables: {', '.join(names)}")

    # ── advanced parameters ───────────────────────────────────────────────────
    st.markdown("#### Advanced Parameters")
    col_temp, col_maxt = st.columns([3, 1])
    with col_temp:
        st.caption("Temperature")
        temperature = st.slider(
            "Temperature", 0.0, 1.0, 0.8, 0.05, label_visibility="collapsed"
        )
    with col_maxt:
        st.caption("Max Tokens")
        max_tokens = st.number_input(
            "Max Tokens", min_value=256, max_value=65536, value=8192,
            label_visibility="collapsed",
        )

    col_rows, _ = st.columns([2, 3])
    with col_rows:
        st.caption("Rows per table")
        num_rows = st.slider(
            "Rows per table", 10, 1000, 100, 10, label_visibility="collapsed"
        )

    # ── generate button ───────────────────────────────────────────────────────
    if st.button("Generate", type="primary"):
        if not st.session_state.schema_tables:
            st.error("Please upload a DDL schema or select a sample schema first.")
        else:
            from src.data_generator import DataGenerator

            logger.info(
                "Generation requested: %d tables, %d rows, temperature=%.2f",
                len(st.session_state.schema_tables), num_rows, temperature,
            )
            generator = DataGenerator(temperature=temperature, max_tokens=max_tokens)
            tables = st.session_state.schema_tables
            progress = st.progress(0, text="Starting…")
            generated: dict[str, pd.DataFrame] = {}

            total = len(tables)
            try:
                for i, (tname, df) in enumerate(
                    generator.generate_all(tables, num_rows, user_prompt)
                ):
                    generated[tname] = df
                    progress.progress((i + 1) / total, text=f"Generated {tname} ({len(df)} rows)")

                    db = get_db()
                    if db:
                        db.store_dataframe(df, tname)

                st.session_state.generated_data = generated
                progress.empty()
                logger.info("Generation complete: %d tables", len(generated))
                st.success(f"Done! Generated data for {len(generated)} tables.")
            except Exception:
                logger.error("Data generation failed", exc_info=True)
                progress.empty()
                import sys
                st.error(f"Generation failed: {sys.exc_info()[1]}")

    # ── data preview ──────────────────────────────────────────────────────────
    if st.session_state.generated_data:
        st.markdown("---")
        col_title, col_sel = st.columns([3, 1])
        with col_title:
            st.subheader("Data Preview")
        with col_sel:
            table_names = list(st.session_state.generated_data.keys())
            selected = st.selectbox(
                "table",
                options=table_names,
                label_visibility="collapsed",
            )

        if selected:
            df = st.session_state.generated_data[selected]
            st.dataframe(df.head(20), use_container_width=True, height=320)
            st.caption(f"Showing up to 20 of **{len(df)}** rows")

            # ── downloads ─────────────────────────────────────────────────────
            dl1, dl2 = st.columns(2)
            with dl1:
                st.download_button(
                    "⬇ Download CSV",
                    df.to_csv(index=False),
                    file_name=f"{selected}.csv",
                    mime="text/csv",
                )
            with dl2:
                buf = io.BytesIO()
                with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                    for tname, tdf in st.session_state.generated_data.items():
                        zf.writestr(f"{tname}.csv", tdf.to_csv(index=False))
                st.download_button(
                    "⬇ Download All (ZIP)",
                    buf.getvalue(),
                    file_name="all_tables.zip",
                    mime="application/zip",
                )

            # ── quick edit ────────────────────────────────────────────────────
            st.markdown("---")
            col_edit, col_submit = st.columns([5, 1])
            with col_edit:
                edit_instr = st.text_input(
                    "quick_edit",
                    placeholder="Enter quick edit instructions…",
                    label_visibility="collapsed",
                )
            with col_submit:
                if st.button("✏️ Submit", key="quick_edit_btn"):
                    if edit_instr.strip():
                        from src.data_generator import DataGenerator

                        logger.info("Quick edit on '%s': %r", selected, edit_instr[:120])
                        try:
                            with st.spinner("Applying changes…"):
                                gen = DataGenerator(temperature=0.3, max_tokens=max_tokens)
                                updated = gen.modify_table(
                                    df,
                                    st.session_state.schema_tables[selected],
                                    edit_instr,
                                )
                            st.session_state.generated_data[selected] = updated
                            db = get_db()
                            if db:
                                db.store_dataframe(updated, selected)
                            logger.info("Quick edit applied to '%s': %d rows", selected, len(updated))
                        except Exception:
                            logger.error("Quick edit failed for table '%s'", selected, exc_info=True)
                            import sys
                            st.error(f"Edit failed: {sys.exc_info()[1]}")
                        st.rerun()
                    else:
                        st.warning("Please enter an instruction.")

# =============================================================================
#  PAGE 2 — TALK TO YOUR DATA
# =============================================================================
elif "Talk" in page:
    st.subheader("Talk to your data")

    db = get_db()
    has_data = bool(st.session_state.generated_data) or (db and db.has_data())

    if not has_data:
        st.info(
            "No data loaded yet. Generate data in the **Data Generation** tab first, "
            "or ensure your database already contains tables."
        )
        st.stop()

    if st.button("🗑️  Clear conversation", key="clear_chat"):
        st.session_state.messages = []
        st.rerun()

    # render history
    for msg in st.session_state.messages:
        with st.chat_message(msg["role"]):
            mtype = msg.get("type", "text")
            if mtype == "table":
                if msg.get("explanation"):
                    st.markdown(msg["explanation"])
                st.dataframe(msg["content"], use_container_width=True)
                if msg.get("sql"):
                    with st.expander("SQL"):
                        st.code(msg["sql"], language="sql")
            elif mtype == "plot":
                if msg.get("explanation"):
                    st.markdown(msg["explanation"])
                st.plotly_chart(msg["content"], use_container_width=True)
                if msg.get("sql"):
                    with st.expander("SQL"):
                        st.code(msg["sql"], language="sql")
            else:
                st.markdown(msg["content"])

    # chat input
    if question := st.chat_input("Ask a question about your data…"):
        st.session_state.messages.append({"role": "user", "content": question, "type": "text"})
        with st.chat_message("user"):
            st.markdown(question)

        with st.chat_message("assistant"):
            if db is None:
                logger.error("Talk-to-data query attempted but DB is not connected")
                st.error("Database not connected — cannot run queries.")
            else:
                from src.talk_to_data import TalkToDataManager

                logger.info("User question: %r", question)
                mgr = TalkToDataManager(db)

                with st.spinner("Thinking…"):
                    result = mgr.query(question, st.session_state.schema_tables)

                rtype = result.get("type", "text")
                if rtype == "text":
                    st.markdown(result["content"])
                    st.session_state.messages.append(
                        {"role": "assistant", "content": result["content"], "type": "text"}
                    )

                elif rtype == "table":
                    exp = result.get("explanation", "")
                    if exp:
                        st.markdown(exp)
                    st.dataframe(result["content"], use_container_width=True)
                    sql = result.get("sql", "")
                    if sql:
                        with st.expander("SQL"):
                            st.code(sql, language="sql")
                    st.session_state.messages.append(
                        {
                            "role": "assistant",
                            "content": result["content"],
                            "type": "table",
                            "explanation": exp,
                            "sql": sql,
                        }
                    )

                elif rtype == "plot":
                    exp = result.get("explanation", "")
                    if exp:
                        st.markdown(exp)
                    st.plotly_chart(result["content"], use_container_width=True)
                    sql = result.get("sql", "")
                    if sql:
                        with st.expander("SQL"):
                            st.code(sql, language="sql")
                    st.session_state.messages.append(
                        {
                            "role": "assistant",
                            "content": result["content"],
                            "type": "plot",
                            "explanation": exp,
                            "sql": sql,
                        }
                    )
