"""Structured JSON logging.

Answers, evidence bytes, tokens and signing keys must never reach a log. The
redaction filter below is a backstop, not permission to be careless — but a
backstop is worth having, because the cost of an access token in a log
aggregator is measured in incidents.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from typing import Any

_REDACT_PATTERNS = [
    re.compile(r"(?i)(authorization|cookie|password|secret|token|private_key|mfa_secret)"),
]

_SENSITIVE_KEYS = frozenset(
    {
        "password",
        "password_hash",
        "token",
        "access_token",
        "refresh_token",
        "authorization",
        "cookie",
        "set-cookie",
        "signing_key",
        "private_key",
        "mfa_secret",
        "mfa_secret_enc",
        "answer",
        "answers",
        "value",
        "source",
    }
)


def redact(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {
            k: ("[redacted]" if k.lower() in _SENSITIVE_KEYS else redact(v)) for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    return obj


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S.%03dZ"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for field in (
            "request_id",
            "org_id",
            "user_id",
            "session_id",
            "route",
            "status",
            "duration_ms",
        ):
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        extra = getattr(record, "context", None)
        if isinstance(extra, dict):
            payload["context"] = redact(extra)

        line = json.dumps(payload, separators=(",", ":"))
        for pattern in _REDACT_PATTERNS:
            if pattern.search(line) and "[redacted]" not in line:
                # Something sensitive-looking slipped through as a raw message.
                # Better a useless log line than a leaked credential.
                payload["message"] = "[redacted: message matched a sensitive pattern]"
                line = json.dumps(payload, separators=(",", ":"))
                break
        return line


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())
    logging.getLogger("uvicorn.access").handlers.clear()
