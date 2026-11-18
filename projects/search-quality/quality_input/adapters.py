"""Adapters only map source facts. They do not assign quality labels."""

import csv
import sqlite3
from pathlib import Path

from .common import InputError, canonical, empty, issue, parse_json, read_json, read_jsonl
from .reference_export import is_reference_export, reference_export_adapter


QUERY_FIELDS = (
    "query_id", "query_text", "raw_query", "language", "locale", "category",
    "query_issued_at", "request_frequency",
)
RUN_FIELDS = (
    "search_run_id", "provider", "system_version", "requested_top_k",
    "collection_started_at", "collection_completed_at",
)
RESPONSE_FIELDS = (
    "status", "http_status", "attempt_count", "retrieved_at",
    "reported_result_count", "error_kind", "error_message",
)
RESULT_FIELDS = (
    "rank", "url", "title", "snippet", "text", "published_at", "source_score",
)
FLAT_FIELDS = set(QUERY_FIELDS + RUN_FIELDS + RESPONSE_FIELDS + RESULT_FIELDS)


def sqlite_rows(connection, table):
    return [dict(row) for row in connection.execute(f'SELECT * FROM "{table}"')]


def sqlite_adapter(path, issues):
    source = sqlite3.connect(Path(path).as_uri() + "?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    source.execute("PRAGMA query_only=ON")
    raw_path = "raw/source.sqlite"
    try:
        tables = {row[0] for row in source.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"sampling_run", "queries", "query_urls"} <= tables:
            raise InputError("SQLite requires sampling_run, queries and query_urls tables")
        required = {
            "sampling_run": {"run_id", "top_k", "api_endpoint"},
            "queries": {"query_id", "run_id", "query_text", "collect_status", "result_count"},
            "query_urls": {"url_id", "query_id", "rank", "url", "title",
                           "snippet", "published_at", "result_json"},
        }
        for table, fields in required.items():
            columns = {row[1] for row in source.execute(f'PRAGMA table_info("{table}")')}
            if not fields <= columns:
                raise InputError(f"{table} missing columns: {sorted(fields - columns)}")
        check = [row[0] for row in source.execute("PRAGMA quick_check")]
        if check != ["ok"]:
            raise InputError("SQLite integrity check failed")
        for row in source.execute("PRAGMA foreign_key_check"):
            issue(issues, "FOREIGN_KEY_ERROR", "SQLite foreign key violation",
                  table=row[0], row_id=row[1])
        raw_runs = sqlite_rows(source, "sampling_run")
        raw_queries = sqlite_rows(source, "queries")
        raw_results = sqlite_rows(source, "query_urls")
    finally:
        source.close()
    queries, runs, responses = [], [], []
    response_map = {}
    for row in raw_runs:
        # This is a named producer adapter, not a general SQLite/API detector.
        from urllib.parse import urlparse
        if urlparse(row.get("api_endpoint") or "").hostname != "api.ateve.ai":
            raise InputError("This SQLite adapter requires an api.ateve.ai source")
        runs.append({
            "search_run_id": row["run_id"], "provider": "ateve",
            # tool_version is the collector version, not the search-engine version.
            "system_version": None, "collector_version": row.get("tool_version"),
            "requested_top_k": row["top_k"],
            "collection_started_at": row.get("created_at"),
            "collection_completed_at": row.get("completed_at"),
            "query_scope": [q["query_id"] for q in raw_queries if q["run_id"] == row["run_id"]],
            "source_query_window": {
                "start": row.get("start_time"), "end": row.get("end_time"),
                "source_timezone": row.get("timezone"),
            },
            "request_parameters": {"top_k": row["top_k"]},
            "source_ref": {"path": raw_path, "table": "sampling_run", "key": row["run_id"]},
        })
    for row in raw_queries:
        ref = {"path": raw_path, "table": "queries", "key": row["query_id"]}
        queries.append({
            "query_id": row["query_id"], "query_text": row["query_text"],
            "raw_query": row.get("raw_query"), "language": row.get("language"),
            "locale": None, "category": row.get("query_category"),
            "query_issued_at": row.get("entry_event_time"),
            "request_frequency": row.get("request_frequency"), "source_ref": ref,
        })
        response = {
            "query_id": row["query_id"], "search_run_id": row["run_id"],
            "status": row["collect_status"], "http_status": row.get("http_status"),
            "attempt_count": row.get("attempt_count"),
            "retrieved_at": row.get("collected_at"),
            "reported_result_count": row["result_count"],
            "error_kind": row.get("error_kind"), "error_message": row.get("error_message"),
            "source_ref": ref, "results": [],
        }
        responses.append(response)
        response_map[row["query_id"]] = response
    for row in raw_results:
        ref = {"path": raw_path, "table": "query_urls", "key": row["url_id"]}
        response = response_map.get(row["query_id"])
        if response is None:
            issue(issues, "ORPHAN_RESULT", "Result references an unknown query",
                  source_ref=ref)
            continue
        original_valid = False
        try:
            original = parse_json(row["result_json"])
            if not isinstance(original, dict):
                raise InputError("result_json must be an object")
            original_valid = True
        except (ValueError, TypeError) as exc:
            issue(issues, "INVALID_RESULT_JSON", str(exc), source_ref=ref)
            original = {}
        for field in ("url", "title", "snippet", "published_at"):
            if original_valid and original.get(field) != row[field]:
                issue(issues, "FIELD_CONFLICT",
                      f"Column and result_json disagree: {field}", source_ref=ref)
        response["results"].append({
            **{field: row[field] for field in ("rank", "url", "title", "snippet", "published_at")},
            "text": original.get("text"), "source_score": original.get("score"),
            "source_metadata": {key: original[key] for key in
                                ("id", "display_url", "images", "is_safe") if key in original},
            "raw_ref": {**ref, "field": "result_json"},
        })
    return {"queries": queries, "search_runs": runs, "responses": responses,
            "page_snapshots": [], "reference_labels": []}


def object_adapter(path):
    data = read_json(path)
    if isinstance(data, list):
        return nested_results_adapter(data)
    if not isinstance(data, dict):
        raise InputError("JSON input must be an object with queries/search_runs/responses arrays")
    for key in ("queries", "search_runs", "responses"):
        if not isinstance(data.get(key), list):
            raise InputError(f"JSON input requires an array: {key}")
    for key in ("queries", "search_runs", "responses", "page_snapshots", "reference_labels"):
        values = data.setdefault(key, [])
        if not isinstance(values, list) or any(not isinstance(row, dict) for row in values):
            raise InputError(f"{key} must contain objects")
        for index, row in enumerate(values):
            row["source_ref"] = {"path": "raw/source.json", "pointer": f"/{key}/{index}"}
            if key == "responses" and isinstance(row.get("results"), list):
                for result_index, result in enumerate(row["results"]):
                    if isinstance(result, dict):
                        result["raw_ref"] = {"path": "raw/source.json",
                                             "pointer": f"/{key}/{index}/results/{result_index}"}
    return data


def nested_results_adapter(data):
    """Position-preserving query/results export; original metadata stays in raw JSON."""
    queries, responses = [], []
    input_index = 0
    run_id = "provided_results"
    for qi, row in enumerate(data):
        if (not isinstance(row, dict) or not isinstance(row.get("query"), str)
                or not isinstance(row.get("results"), list)):
            raise InputError(f"/{qi}: expected query string and results array")
        qid = f"Q{qi:08d}"
        ref = {"path": "raw/source.json", "pointer": f"/{qi}"}
        queries.append({"query_id": qid, "query_text": row["query"], "source_ref": ref})
        results = []
        for ri, result in enumerate(row["results"]):
            if not isinstance(result, dict) or any(
                    result.get(k) is not None and not isinstance(result[k], str)
                    for k in ("url", "title", "snippet")):
                raise InputError(f"/{qi}/results/{ri}: result text fields must be strings or null")
            results.append({
                **{k: result.get(k) for k in ("url", "title", "snippet")}, "rank": ri + 1,
                "raw_ref": {"path": "raw/source.json", "pointer": f"/{qi}/results/{ri}"},
                "source_metadata": {"input_index": input_index, "source_query_index": qi,
                                    "source_result_index": ri},
            })
            input_index += 1
        # This adapter describes the supplied collection, not an inferred HTTP response.
        responses.append({"query_id": qid, "search_run_id": run_id,
                          "status": "ok" if results else "empty", "results": results, "source_ref": ref})
    return {"_adapter": "nested_query_results_v1", "queries": queries, "responses": responses,
            "search_runs": [{"search_run_id": run_id, "provider": "provided_results",
                             "query_scope": [q["query_id"] for q in queries]}]}


def load_mapping(path):
    mapping = read_json(path) if path else {}
    if not isinstance(mapping, dict) or set(mapping) - {"fields", "defaults"}:
        raise InputError("Mapping supports only fields and defaults objects")
    fields, defaults = mapping.get("fields", {}), mapping.get("defaults", {})
    if not isinstance(fields, dict) or not isinstance(defaults, dict):
        raise InputError("Mapping fields/defaults must be objects")
    if set(fields) - FLAT_FIELDS or set(defaults) - set(RUN_FIELDS):
        raise InputError("Unknown mapping field, or default outside SearchRun fields")
    if any(not isinstance(value, str) or not value for value in fields.values()):
        raise InputError("Mapping source keys must be nonempty strings")
    return {"fields": fields, "defaults": defaults}


def flat_adapter(path, kind, mapping, issues):
    if kind == "jsonl":
        rows = read_jsonl(path)
        if not mapping["fields"] and not mapping["defaults"] and is_reference_export(rows):
            return reference_export_adapter(rows, issues)
    else:
        with Path(path).open(encoding="utf-8-sig", newline="") as source:
            reader = csv.DictReader(source)
            if not reader.fieldnames or len(set(reader.fieldnames)) != len(reader.fieldnames):
                raise InputError("CSV requires a unique header row")
            rows = list(enumerate(reader, 2))
    if not rows:
        raise InputError("Flat input contains no records")
    available_keys = {key for _, row in rows if isinstance(row, dict) for key in row}
    for source_key in mapping["fields"].values():
        if source_key not in available_keys:
            raise InputError(f"Mapped key {source_key!r} does not occur in the input")
    queries, runs, responses = {}, {}, {}

    def merge(target, key, value, ref):
        if key not in target:
            target[key] = {**value, "source_ref": ref}
            return
        existing = target[key]
        for field, incoming in value.items():
            if empty(incoming):
                continue
            if empty(existing.get(field)):
                existing[field] = incoming
            elif canonical(existing[field]) != canonical(incoming):
                issue(issues, "FIELD_CONFLICT", f"Inconsistent repeated field: {field}",
                      source_ref=ref, field=field)

    for row_number, original in rows:
        ref = {"path": f"raw/source.{kind}", "record": row_number}
        if not isinstance(original, dict) or None in original:
            raise InputError(f"Invalid flat record at {row_number}")
        row = {}
        for field in FLAT_FIELDS:
            source_key = mapping["fields"].get(field, field)
            if source_key in original:
                row[field] = original[source_key]
            elif field in mapping["defaults"]:
                row[field] = mapping["defaults"][field]
        # Required keys must be present, but content validation is centralized.
        for field in ("query_id", "query_text", "search_run_id", "provider"):
            if field not in row:
                raise InputError(f"Flat input requires {field}, record {row_number}")
        qkey, rkey = canonical(row["query_id"]), canonical(row["search_run_id"])
        pair = (qkey, rkey)
        merge(queries, qkey, {f: row.get(f) for f in QUERY_FIELDS}, ref)
        merge(runs, rkey, {f: row.get(f) for f in RUN_FIELDS}, ref)
        response_fields = {
            "query_id": row["query_id"], "search_run_id": row["search_run_id"],
            **{f: row.get(f) for f in RESPONSE_FIELDS},
        }
        merge(responses, pair, response_fields, ref)
        response = responses[pair]
        response.setdefault("results", [])
        has_result = any(not empty(row.get(f)) for f in RESULT_FIELDS)
        if has_result:
            response["results"].append({**{f: row.get(f) for f in RESULT_FIELDS},
                                         "raw_ref": ref})
        else:
            response.setdefault("_empty_row_refs", []).append(ref)
    for response in responses.values():
        if response["results"] and response.get("_empty_row_refs"):
            issue(issues, "MIXED_EMPTY_ROW", "Response has both empty-marker and result rows",
                  source_ref=response["_empty_row_refs"][0])
    for run in runs.values():
        run["query_scope"] = [
            q["query_id"] for q in queries.values()
            if (canonical(q["query_id"]), canonical(run["search_run_id"])) in responses
        ]
    issue(issues, "FLAT_QUERY_SCOPE",
          "Flat input can only preserve queries explicitly represented by rows; "
          "include an empty/status row for every query without results.", "info")
    return {"queries": list(queries.values()), "search_runs": list(runs.values()),
            "responses": list(responses.values()), "page_snapshots": [], "reference_labels": []}
