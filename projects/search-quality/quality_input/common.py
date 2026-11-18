import hashlib
import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path


class InputError(ValueError):
    pass


def reject_constant(value):
    raise InputError(f"Non-finite JSON number: {value}")


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise InputError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def parse_json(text):
    return json.loads(text, parse_constant=reject_constant,
                      object_pairs_hook=unique_object)


def read_json(path):
    return parse_json(Path(path).read_text(encoding="utf-8-sig"))


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def file_digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def write_json(path, value):
    Path(path).write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def write_jsonl(path, records):
    with Path(path).open("w", encoding="utf-8") as target:
        for record in records:
            target.write(canonical(record) + "\n")


def read_jsonl(path):
    records = []
    with Path(path).open(encoding="utf-8-sig") as source:
        for line, text in enumerate(source, 1):
            if text.strip():
                try:
                    records.append((line, parse_json(text)))
                except ValueError as exc:
                    raise InputError(f"JSONL line {line}: {exc}") from exc
    return records


def identifier(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise InputError("Expected a string or integer identifier")
    result = str(value)
    if not result.strip():
        raise InputError("Identifier is empty")
    return result


def integer(value, minimum=0):
    if isinstance(value, bool):
        raise InputError("Boolean is not an integer")
    if isinstance(value, int):
        result = value
    elif isinstance(value, str) and value.strip().isascii() and value.strip().isdigit():
        result = int(value.strip())
    else:
        raise InputError("Expected an integer")
    if result < minimum:
        raise InputError(f"Expected integer >= {minimum}")
    return result


def number(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise InputError("Expected a finite number")
    try:
        result = float(value)
    except (ValueError, OverflowError) as exc:
        raise InputError("Expected a finite number") from exc
    if not math.isfinite(result):
        raise InputError("Expected a finite number")
    return result


def timestamp(value):
    """Require an explicit offset. No host-timezone or date-only inference."""
    if not isinstance(value, str):
        raise InputError("Expected an ISO datetime string with timezone")
    try:
        # Python 3.9 accepts microseconds, while producer timestamps may have
        # nanoseconds. Originals are retained separately by the normalizer.
        compatible = re.sub(r"(\.\d{6})\d+(?=Z$|[+-]\d{2}:\d{2}$)", r"\1", value)
        dt = datetime.fromisoformat(compatible.replace("Z", "+00:00"))
    except ValueError as exc:
        raise InputError("Invalid ISO datetime") from exc
    if dt.tzinfo is None:
        raise InputError("Datetime has no timezone")
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def issue(issues, code, message, severity="error", **location):
    issues.append({"severity": severity, "code": code, "message": message,
                   **{key: value for key, value in location.items() if value is not None}})


def empty(value):
    return value is None or value == ""
