"""Structured JSON logging to stdout (see plan: Deployment)."""

from __future__ import annotations

import logging
import sys
from typing import cast

import structlog


def configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level)),
        context_class=dict,
        # No `file=` here on purpose: `PrintLoggerFactory()` with no argument
        # makes `PrintLogger` resolve `sys.stdout` fresh on every write
        # instead of capturing a fixed reference at configure time. Passing
        # `file=sys.stdout` explicitly bakes in whatever `sys.stdout` happens
        # to be *right now* (e.g. a test runner's temporarily redirected
        # buffer), which then keeps writing to that stale object forever
        # since `cache_logger_on_first_use=True` caches the logger instance.
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return cast(structlog.stdlib.BoundLogger, structlog.get_logger(name))
