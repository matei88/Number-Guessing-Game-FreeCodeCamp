"""Langfuse observability: singleton client, @observe decorator, and helpers."""
from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from typing import Any, Iterator

logger = logging.getLogger(__name__)

# ── singleton ─────────────────────────────────────────────────────────────────
_langfuse: Any = None

try:
    from langfuse import Langfuse, observe as _lf_observe  # type: ignore[import]

    _pk = os.environ.get("LANGFUSE_PUBLIC_KEY", "")
    _sk = os.environ.get("LANGFUSE_SECRET_KEY", "")
    _host = os.environ.get("LANGFUSE_BASE_URL", "http://localhost:3000")

    if _pk and _sk:
        _langfuse = Langfuse(public_key=_pk, secret_key=_sk, host=_host)
        logger.info("Langfuse tracking enabled (host=%s)", _host)
    else:
        logger.debug("Langfuse keys not set — tracking disabled")

    observe = _lf_observe  # re-export the real decorator

except ImportError:
    logger.debug("langfuse package not installed — tracking disabled")
    observe = None  # type: ignore[assignment]
except Exception:
    logger.warning("Langfuse initialisation failed", exc_info=True)
    observe = None  # type: ignore[assignment]

# ── no-op @observe when langfuse is unavailable ───────────────────────────────
if observe is None:
    def observe(*args: Any, **kwargs: Any) -> Any:  # type: ignore[misc]
        """No-op @observe decorator used when Langfuse is unavailable."""
        if len(args) == 1 and callable(args[0]):
            return args[0]          # @observe  (no parens)
        def _deco(fn: Any) -> Any:
            return fn               # @observe(as_type=...) (with parens)
        return _deco


# ── no-op observation used by start_observation() ────────────────────────────
class _NoOpObservation:
    """Returned by start_observation() when Langfuse is disabled."""

    def update(self, **kwargs: Any) -> None:  # noqa: D102
        pass

    def score(self, **kwargs: Any) -> None:  # noqa: D102
        pass


# ── public helpers ────────────────────────────────────────────────────────────

def get_langfuse() -> Any:
    """Return the Langfuse singleton, or None if not configured."""
    return _langfuse


@contextmanager
def start_observation(**kwargs: Any) -> Iterator[Any]:
    """Context manager that creates a Langfuse observation (span or generation).

    Falls back to a no-op when Langfuse is disabled so callers never need to
    branch on whether tracking is active.

    Usage::

        with start_observation(name="my-step", as_type="generation", model="gemini-2.0-flash") as obs:
            result = call_llm(...)
            obs.update(output=result, usage_details={...})
    """
    lf = get_langfuse()
    if lf is not None:
        with lf.start_as_current_observation(**kwargs) as obs:
            yield obs
    else:
        yield _NoOpObservation()


def flush_traces() -> None:
    """Flush all pending spans/generations to the Langfuse server."""
    if _langfuse is not None:
        try:
            _langfuse.flush()
            logger.debug("Langfuse traces flushed")
        except Exception:
            logger.warning("Langfuse flush failed", exc_info=True)
