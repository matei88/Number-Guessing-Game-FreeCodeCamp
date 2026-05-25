import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)


class LangfuseTracker:
    def __init__(self) -> None:
        self._client = None
        try:
            from langfuse import Langfuse  # type: ignore

            pk = os.environ.get("LANGFUSE_PUBLIC_KEY", "")
            sk = os.environ.get("LANGFUSE_SECRET_KEY", "")
            host = os.environ.get("LANGFUSE_HOST", "http://localhost:3000")
            if pk and sk:
                self._client = Langfuse(public_key=pk, secret_key=sk, host=host)
                logger.info("Langfuse tracking enabled (host=%s)", host)
            else:
                logger.debug("Langfuse keys not set — tracking disabled")
        except ImportError:
            logger.debug("langfuse package not installed — tracking disabled")
        except Exception:
            logger.warning("Langfuse initialisation failed — tracking disabled", exc_info=True)

    def track_generation(
        self,
        name: str,
        model: str,
        input_text: str,
        output_text: str,
        metadata: Optional[dict] = None,
    ) -> None:
        if self._client is None:
            return
        try:
            trace = self._client.trace(name=name)
            trace.generation(
                name=name,
                model=model,
                input=input_text,
                output=output_text,
                metadata=metadata or {},
            )
            logger.debug("Langfuse generation tracked: %s", name)
        except Exception:
            logger.warning("Langfuse tracking failed for '%s'", name, exc_info=True)
