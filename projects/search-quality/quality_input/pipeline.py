import shutil
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from . import SCHEMA_VERSION, __version__
from .adapters import flat_adapter, load_mapping, object_adapter, sqlite_adapter
from .common import (
    InputError, digest, file_digest, issue, read_json, read_jsonl,
    timestamp, write_json, write_jsonl,
)
from .normalize import Normalizer


ADAPTER_VERSIONS = {
    "sqlite": "ateve_sampling_sqlite_v1",
    "json": "canonical_object_v1",
    "jsonl": "flat_jsonl_v1",
    "csv": "flat_csv_v1",
}


def utc_now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def snapshot(source, target, kind):
    if kind != "sqlite":
        shutil.copyfile(source, target)
        return
    # SQLite backup includes committed WAL data in one consistent snapshot.
    # Copying only the .sqlite file can silently miss WAL transactions.
    incoming = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)
    outgoing = sqlite3.connect(target)
    try:
        incoming.execute("PRAGMA query_only=ON")
        incoming.backup(outgoing)
        # A backup may inherit WAL mode. Freeze the destination as a standalone
        # database so a read-only consumer never needs -wal/-shm sidecars.
        outgoing.execute("PRAGMA journal_mode=DELETE")
    finally:
        outgoing.close()
        incoming.close()


def make_report(data, issues, time_available):
    queries, responses = data["queries"], data["responses"]
    results = [result for response in responses for result in response["results"]]
    status_counts = Counter(response["status"] for response in responses)
    lengths = Counter(response["actual_result_count"] for response in responses)
    issue_counts = Counter(item["code"] for item in issues)
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "blocked" if any(item["severity"] == "error" for item in issues) else "ready",
        "counts": {
            "queries": len(queries), "search_runs": len(data["search_runs"]),
            "responses": len(responses), "results": len(results),
            "distinct_url_strings": len({r["url"] for r in results if r["url"]}),
            "response_status": dict(sorted(status_counts.items())),
            "results_per_response": {str(k): v for k, v in sorted(lengths.items())},
            "missing_result_fields": {
                key: sum(not result.get(key) for result in results)
                for key in ("url", "title", "snippet", "text", "published_at")
            },
            "missing_query_fields": {
                key: sum(not query.get(key) for query in queries)
                for key in ("language", "locale", "category", "query_issued_at")
            },
            "page_snapshots": len(data["page_snapshots"]),
            "reference_labels": len(data["reference_labels"]),
            "reference_labels_by_source": dict(Counter(label["source"] for label in data["reference_labels"])),
        },
        "capabilities": {
            # Evidence presence, not a claim that every result is judgeable.
            "api_records_with_any_visible_evidence": sum(
                any(r.get(f) and r[f].strip() for f in ("url", "title", "snippet", "text"))
                for r in results),
            "page_snapshots_with_body": sum(bool(p["body_text"]) for p in data["page_snapshots"]),
            "evaluation_time_available": time_available,
            "reference_scopes_declared": sorted({label["reference_scope"]
                                                for label in data["reference_labels"]
                                                if isinstance(label["reference_scope"], str)}),
            "reference_completeness_verified": False,
        },
        "issue_counts": dict(sorted(issue_counts.items())),
        "issues": issues,
    }


def import_dataset(input_path, output_path, *, format="auto", mapping_path=None,
                   evaluation_as_of=None, use_collection_time=False, dataset_id=None):
    """Freeze input and return a report. Blocking issues never drop queries silently.

    The output must not exist. A blocked import keeps raw evidence and diagnostics;
    consumers must use load_dataset(), which rejects blocked or tampered bundles.
    """
    source, output = Path(input_path).resolve(), Path(output_path).resolve()
    if not source.is_file():
        raise InputError(f"Input file does not exist: {source}")
    if evaluation_as_of is not None and use_collection_time:
        raise InputError("Choose explicit evaluation time OR collection time")
    if evaluation_as_of is not None:
        evaluation_as_of = timestamp(evaluation_as_of)
    kind = format
    if kind == "auto":
        kind = {".sqlite": "sqlite", ".db": "sqlite", ".sqlite3": "sqlite",
                ".json": "json", ".jsonl": "jsonl", ".ndjson": "jsonl",
                ".csv": "csv"}.get(source.suffix.lower())
    if kind not in ADAPTER_VERSIONS:
        raise InputError("Supported formats: sqlite, json, jsonl, csv")
    if mapping_path and kind not in ("csv", "jsonl"):
        raise InputError("--mapping is supported only for flat CSV/JSONL")
    mapping = load_mapping(mapping_path)
    # Never overwrite an existing import, even if empty.
    output.mkdir(parents=True, exist_ok=False)
    (output / "raw").mkdir()
    frozen = output / "raw" / f"source.{kind}"
    issues = []
    data = {"queries": [], "search_runs": [], "responses": [],
            "page_snapshots": [], "reference_labels": []}
    source_hash = None
    adapter_version = ADAPTER_VERSIONS[kind]
    try:
        snapshot(source, frozen, kind)
        source_hash = file_digest(frozen)
        if kind == "sqlite":
            raw_data = sqlite_adapter(frozen, issues)
        elif kind == "json":
            raw_data = object_adapter(frozen)
        else:
            raw_data = flat_adapter(frozen, kind, mapping, issues)
        adapter_version = raw_data.pop("_adapter", adapter_version)
        data = Normalizer(issues).normalize(raw_data)
    except (InputError, ValueError, TypeError, KeyError, sqlite3.Error, OSError) as exc:
        issue(issues, "IMPORT_FAILED", f"{type(exc).__name__}: {exc}")
    time_basis = "explicit" if evaluation_as_of else "unspecified"
    if use_collection_time:
        times = [run["collection_completed_at"] for run in data["search_runs"]
                 if run["collection_completed_at"]]
        times.extend(response["retrieved_at"] for response in data["responses"]
                     if response["retrieved_at"])
        if times:
            evaluation_as_of = max(times, key=lambda value: datetime.fromisoformat(
                value.replace("Z", "+00:00")))
            time_basis = "recollection_latest_completion"
        else:
            issue(issues, "EVALUATION_TIME_UNAVAILABLE",
                  "Collection-time policy requested but no valid collection timestamp exists")
    if evaluation_as_of is None:
        issue(issues, "EVALUATION_TIME_UNSPECIFIED",
              "Relative-time judgments require an explicit evaluation time policy", "warning")
    report = make_report(data, issues, evaluation_as_of is not None)
    if source_hash is None and frozen.is_file():
        source_hash = file_digest(frozen)
    config = {
        "adapter": adapter_version, "schema_version": SCHEMA_VERSION,
        "implementation_version": __version__, "source_sha256": source_hash,
        "mapping": mapping, "evaluation_as_of": evaluation_as_of, "time_basis": time_basis,
        "dataset_id": dataset_id,
    }
    version = digest(config)
    manifest = {
        "schema_version": SCHEMA_VERSION, "implementation_version": __version__,
        "dataset_id": dataset_id or "dataset_" + version[:24],
        "dataset_version": version, "created_at": utc_now(), "status": report["status"],
        "sources": [{
            "original_path": str(source), "snapshot_path": f"raw/source.{kind}",
            "snapshot_sha256": source_hash,
            "snapshot_method": "sqlite_backup" if kind == "sqlite" else "byte_copy",
            "adapter": adapter_version,
        }],
        "mapping_version": "flat_mapping_v1", "mapping": mapping,
        "evaluation_as_of": evaluation_as_of, "time_basis": time_basis,
        "query_scope": [query["query_id"] for query in data["queries"]],
        "search_runs": data["search_runs"],
        "document_identity_policy": "url_exact_v1",
        "source_query_windows": [run["source_query_window"] for run in data["search_runs"]
                                 if run["source_query_window"] is not None],
        "files": {},
    }
    for name in ("queries", "responses", "page_snapshots", "reference_labels"):
        if name in ("queries", "responses") or data[name]:
            path = output / f"{name}.jsonl"
            write_jsonl(path, data[name])
            manifest["files"][path.name] = file_digest(path)
    write_json(output / "input_report.json", report)
    manifest["files"]["input_report.json"] = file_digest(output / "input_report.json")
    if frozen.is_file():
        manifest["files"][f"raw/source.{kind}"] = source_hash
    manifest["manifest_hash"] = digest(manifest)
    write_json(output / "manifest.json", manifest)
    return report


def load_dataset(path):
    """Read a validated bundle; refuse blocked data and file-hash mismatches."""
    root = Path(path).resolve()
    manifest = read_json(root / "manifest.json")
    expected_manifest_hash = manifest.get("manifest_hash")
    if expected_manifest_hash != digest({k: v for k, v in manifest.items() if k != "manifest_hash"}):
        raise InputError("Manifest checksum mismatch")
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise InputError("Unsupported bundle schema version")
    if manifest.get("status") != "ready":
        raise InputError("Dataset is blocked; inspect input_report.json")
    for required in ("queries.jsonl", "responses.jsonl", "input_report.json"):
        if required not in manifest.get("files", {}):
            raise InputError(f"Manifest missing required file: {required}")
    for filename, expected in manifest["files"].items():
        candidate = (root / filename).resolve()
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise InputError("Manifest file escapes dataset directory") from exc
        if not candidate.is_file() or file_digest(candidate) != expected:
            raise InputError(f"Dataset file checksum mismatch: {filename}")
    report = read_json(root / "input_report.json")
    if report.get("status") != "ready":
        raise InputError("Input report is blocked")
    return {
        "manifest": manifest, "input_report": report,
        "search_runs": manifest["search_runs"],
        **{key: [row for _, row in read_jsonl(root / f"{key}.jsonl")]
           if f"{key}.jsonl" in manifest["files"] else []
           for key in ("queries", "responses", "page_snapshots", "reference_labels")},
    }


def iter_api_inputs(dataset):
    """Produce API judge envelopes, not a query-level metric denominator.

    Empty and failed queries remain in dataset['queries']/['responses'].
    Provider, source_score, rank and source-only request metadata are omitted
    from judge_input; collection_status stays in the orchestration envelope.
    """
    queries = {query["query_id"]: query for query in dataset["queries"]}
    for response in dataset["responses"]:
        query = queries[response["query_id"]]
        for result in response["results"]:
            yield {
                "result_id": result["result_id"], "response_id": response["response_id"],
                "collection_status": response["status"],
                "evidence_hash": result["evidence_hash"],
                "judge_input": {
                    "evidence_mode": "api_only",
                    "evaluation_as_of": dataset["manifest"]["evaluation_as_of"],
                    "query": {key: query[key] for key in
                              ("query_id", "query_text", "language", "locale", "query_issued_at")},
                    "evidence": {
                        **{key: result[key] for key in
                           ("url", "title", "snippet", "text", "published_at", "published_at_raw")},
                        "metadata": {key: result["source_metadata"][key] for key in
                                     ("display_url", "images") if key in result["source_metadata"]},
                    },
                },
            }
