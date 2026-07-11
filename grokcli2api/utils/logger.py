"""Logging configuration built on top of ``rich``.

Uses ``rich.logging.RichHandler`` for a readable console output and a single
plain-text file handler under ``logs/`` so calls don't go silent.
"""

from __future__ import annotations

import logging
from logging import Logger
from pathlib import Path
from typing import Optional

from rich.logging import RichHandler

_LOG_FORMAT = "%(message)s"
_DATE_FORMAT = "[%X]"


def configure_logging(level: str = "INFO", log_dir: Optional[Path] = None) -> None:
    """Initialise the root logger. Idempotent -- safe to call multiple times."""

    root = logging.getLogger()

    # Avoid stacking handlers when reload=True or tests call this repeatedly.
    if getattr(root, "_grok2api_configured", False):
        root.setLevel(level.upper())
        return

    root.setLevel(level.upper())

    # Console via rich
    console_handler = RichHandler(
        rich_tracebacks=True,
        markup=False,
        show_path=False,
        show_time=True,
    )
    console_handler.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT))
    root.addHandler(console_handler)

    # Optional file handler
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_dir / "grokcli2api.log", encoding="utf-8")
        file_handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)-8s %(name)s | %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        root.addHandler(file_handler)

    # Quiet down the noisy libraries.
    for noisy in ("httpx", "httpcore", "hpack"):
        logging.getLogger(noisy).setLevel("WARNING")

    root._grok2api_configured = True  # type: ignore[attr-defined]


def get_logger(name: str) -> Logger:
    """Return a logger using the module's qualified name."""

    return logging.getLogger(name)


def silence_http_logs() -> None:
    """Force every HTTP-level logger down to WARNING -- useful when capturing SSE."""

    for noisy in (
        "httpx",
        "httpcore",
        "hpack",
        "h2",
        "asyncio",
        "uvicorn.access",
    ):
        logging.getLogger(noisy).setLevel(logging.WARNING)
