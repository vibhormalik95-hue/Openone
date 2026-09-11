"""Redacted operational telemetry shared by API and worker."""

import json
import logging
from datetime import UTC, datetime

import sentry_sdk

from hivemind.config import Settings


class RedactedJsonFormatter(logging.Formatter):
    def format(self, record):
        event = record.getMessage() if record.name.startswith("hivemind.") else "library_event"
        payload = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": event,
        }
        for name in ("request_id", "route", "status", "duration_ms"):
            value = getattr(record, name, None)
            if value is not None:
                payload[name] = value
        # Never include record.args, traceback locals, request body, headers, tokens, or SQL.
        return json.dumps(payload, separators=(",", ":"))


def scrub_sentry(event, hint):
    event.pop("request", None)
    event.pop("breadcrumbs", None)
    event.pop("user", None)
    event.pop("extra", None)
    for exception in event.get("exception", {}).get("values", []):
        exception["value"] = "redacted"
        for frame in exception.get("stacktrace", {}).get("frames", []):
            frame.pop("vars", None)
    return event


def configure_logging(settings: Settings):
    handler = logging.StreamHandler()
    handler.setFormatter(RedactedJsonFormatter())
    logging.basicConfig(level=logging.WARNING, handlers=[handler], force=True)
    logging.getLogger("hivemind").setLevel(logging.INFO)
    logging.getLogger("uvicorn.access").disabled = True
    if settings.sentry_dsn:
        sentry_sdk.init(
            dsn=settings.sentry_dsn,
            send_default_pii=False,
            include_local_variables=False,
            traces_sample_rate=0.0,
            before_send=scrub_sentry,
        )
