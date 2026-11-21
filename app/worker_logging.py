"""Worker diagnostics that omit upstream messages, request data and credentials."""

import json
import traceback
from collections import deque
from datetime import datetime, timezone

from app.errors import AppError


def log_worker_issue(logger, event, *, exc=None, **details):
    payload = {"event": event, "at": datetime.now(timezone.utc).isoformat(), **details}
    causes = []
    while exc is not None and len(causes) < 4:
        frames = deque(traceback.walk_tb(exc.__traceback__), maxlen=4)
        cause = {
            "type": type(exc).__name__,
            "frames": [
                f"{frame.f_globals.get('__name__')}:{frame.f_code.co_name}:{line}"
                for frame, line in frames
            ],
        }
        if isinstance(exc, AppError):
            cause["code"] = exc.code
        causes.append(cause)
        exc = exc.__cause__ or (None if exc.__suppress_context__ else exc.__context__)
    if causes:
        payload["exceptions"] = causes
    logger.warning("%s", json.dumps(payload))
