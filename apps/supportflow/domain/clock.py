"""Business reference time is injectable only in an explicit evaluation profile."""
from datetime import datetime, timezone
import os
import re


def business_now(*, real_now=None):
    if real_now is not None and (not isinstance(real_now, datetime) or real_now.tzinfo is None or real_now.utcoffset() is None):
        raise ValueError("A supplied database clock must be timezone-aware")
    reference = os.environ.get("SF_EVAL_REFERENCE_TIME")
    if reference is None:
        return real_now.astimezone(timezone.utc) if real_now is not None else datetime.now(timezone.utc)
    if (os.environ.get("SF_RUNTIME_PROFILE") != "eval" or
            not re.fullmatch(r"sf_eval_[a-z0-9_]+_(?:app|sandbox)", os.environ.get("PGDATABASE", ""))):
        raise ValueError("A reference clock requires the explicit disposable evaluation profile")
    value = datetime.fromisoformat(reference.replace("Z", "+00:00"))
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Reference time must include a timezone")
    return value.astimezone(timezone.utc)
