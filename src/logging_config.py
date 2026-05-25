from __future__ import annotations

import logging
import logging.handlers
import os
from pathlib import Path

_LOG_DIR = Path("logs")
_LOG_FILE = _LOG_DIR / "data_assistant.log"
_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
_DATE_FMT = "%Y-%m-%d %H:%M:%S"

# Third-party loggers that are too noisy at INFO level
_QUIET = [
    "httpx",
    "httpcore",
    "google",
    "urllib3",
    "sqlalchemy.engine",
    "sqlalchemy.pool",
    "streamlit",
    "watchdog",
]


def setup_logging(level: str | None = None) -> None:
    """Configure the root logger once.  Safe to call on every Streamlit rerun."""
    root = logging.getLogger()
    if root.handlers:
        return  # already configured

    effective_level = level or os.environ.get("LOG_LEVEL", "INFO").upper()
    root.setLevel(effective_level)

    formatter = logging.Formatter(_FORMAT, datefmt=_DATE_FMT)

    # ── console (stderr) ──────────────────────────────────────────────────────
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)

    # ── rotating file (10 MB × 5 backups) ─────────────────────────────────────
    try:
        _LOG_DIR.mkdir(exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            _LOG_FILE,
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        fh.setFormatter(formatter)
        root.addHandler(fh)
    except OSError as exc:
        root.warning("Could not create log file %s: %s", _LOG_FILE, exc)

    # ── quiet noisy third-party loggers ───────────────────────────────────────
    for name in _QUIET:
        logging.getLogger(name).setLevel(logging.WARNING)

    root.debug("Logging initialised — level=%s, file=%s", effective_level, _LOG_FILE)
