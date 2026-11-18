"""Bounded retry timing; never retry earlier than the provider's Retry-After."""
import math
import random
from datetime import timezone
from email.utils import parsedate_to_datetime


def retry_after_seconds(value, now):
    """Parse HTTP delay-seconds or an HTTP-date; invalid headers use backoff."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    try:
        if value.isascii() and value.isdigit():
            delay = float(value)
        else:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            delay = max(0.0, date.timestamp() - now)
        # A huge numeric header must not overflow or cause an early retry.
        return delay if math.isfinite(delay) else 1e100
    except (ValueError, TypeError, OverflowError):
        return None


def backoff_seconds(attempt, base, cap):
    """Equal jitter preserves a minimum rest while spreading retry arrivals."""
    ceiling = min(cap, base * 2 ** (attempt - 1))
    return random.uniform(ceiling / 2, ceiling)


class RetryWaitExceeded(Exception):
    def __init__(self, waited, required):
        self.waited, self.required = waited, required
