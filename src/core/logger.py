"""Centralized JSON logging to stdout.

All modules import the ``logger`` instance from here. Structured fields:
``timestamp``, ``level``, ``module``, ``filter_status``, ``duration_ms``.

Usage with extra context:
    logger.info("msg", extra={"module": "db", "filter_status": "ANALYZED"})
    logger.debug("took %.1fms", ms, extra={"duration_ms": ms})
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

_RESERVED = {
    "name",
    "msg",
    "args",
    "levelname",
    "levelno",
    "pathname",
    "filename",
    "module",
    "exc_info",
    "exc_text",
    "stack_info",
    "lineno",
    "funcName",
    "created",
    "msecs",
    "relativeCreated",
    "thread",
    "threadName",
    "processName",
    "process",
    "taskName",
    "asctime",
}


class JsonFormatter(logging.Formatter):
    """Emit single-line JSON records to stdout."""

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "module": record.name,
            "message": record.getMessage(),
        }
        # Pull user-supplied extras
        for key, val in record.__dict__.items():
            if key in _RESERVED or key.startswith("_"):
                continue
            entry[key] = val
        # Duration helper: if caller didn't pass duration_ms, compute from record
        if "duration_ms" not in entry and hasattr(record, "duration_ms"):
            entry["duration_ms"] = record.duration_ms
        if record.exc_info:
            entry["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False, default=str)


def setup_logging(level: int = logging.INFO) -> None:
    """Configure root logger to emit JSON lines to stdout.

    Idempotent: safe to call multiple times.
    """
    root = logging.getLogger()
    # Remove existing handlers to avoid duplicate lines on re-setup
    for h in root.handlers[:]:
        root.removeHandler(h)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    root.setLevel(level)
    # Silence noisy third-party logs below WARNING
    for noisy in ("httpx", "httpcore", "urllib3", "asyncio", "playwright"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


# Module-level labeled logger; modules import this singleton.
logger = logging.getLogger("idx_disclosure")