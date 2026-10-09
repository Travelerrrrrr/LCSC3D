"""Bounded request diagnostics without exception messages or account data."""
from __future__ import annotations

from app_logging import MAX_LOG_BYTES, record_error


def record_request_error(error, operation, *, level='ERROR'):
    return record_error(error, 'request.failed', level=level, operation=operation)
