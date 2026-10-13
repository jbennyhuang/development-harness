"""Recognize explicit provider quota errors and diagnostics."""
import json

USAGE_MARKERS = (
    "usage limit", "usage_limit", "quota exceeded", "quota_exceeded",
    "insufficient_quota", "credits exhausted", "credit balance", "resource_exhausted",
    "rate limit", "rate_limit", "rate_limit_exceeded", "usage_limit_reached",
)


def usage_exhausted(value: object) -> bool:
    text = json.dumps(value, default=str).lower()
    return any(marker in text for marker in USAGE_MARKERS)
