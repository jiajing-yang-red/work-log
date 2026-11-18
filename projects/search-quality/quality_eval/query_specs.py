from pathlib import Path

from quality_input.common import InputError, digest, file_digest, read_json, read_jsonl, timestamp, write_jsonl
from quality_input.pipeline import load_dataset
from quality_llm import LLMClient
from .contracts import validate_spec
from .execution import execute_tasks
from .schemes import prompt_for
from .storage import checked_read, checked_write, run_directory
from .recovery import recovery_summary


def select_queries(dataset, limit=None):
    if limit is not None and (type(limit) is not int or limit < 1):
        raise InputError("limit_queries must be a positive integer")
    all_ids = sorted(q["query_id"] for q in dataset["queries"])
    return all_ids[:limit] if limit else all_ids


def prepare_specs(dataset_path, config, output, *, limit=None, resume=False,
                  retry_failed=False, references_path=None, progress=None, client_factory=LLMClient):
    dataset = load_dataset(dataset_path)
    if config.get("query_understanding", "frozen") == "direct":
        raise InputError("Query preparation is disabled; use plan/run without --specs")
    profile = config["query_model"]
    if profile is None:
        raise InputError("prepare requires an explicit query_model")
    ids = select_queries(dataset, limit)
    references = read_json(references_path) if references_path else {}
    if not isinstance(references, dict):
        raise InputError("Reference answers must be an object keyed by query_id")
    known_ids = {q["query_id"] for q in dataset["queries"]}
    for key, value in references.items():
        if key not in known_ids or not isinstance(value, dict) or set(value) != {"answer", "source", "as_of"}:
            raise InputError("Independent references require known query_id and answer/source/as_of")
        if not all(isinstance(value[k], str) and value[k].strip() for k in value):
            raise InputError("Reference answer fields must be nonempty strings")
        timestamp(value["as_of"])
    system = prompt_for("query_spec", "frozen", config.get("prompt_overrides"))
    identity = {
        "kind": "query_specs", "version": "1.0", "dataset_version": dataset["manifest"]["dataset_version"],
        "query_ids": ids, "model_profile": config["models"][profile], "profile_name": profile,
        "prompt_hash": digest(system), "references_hash": digest(references),
        "runtime": config["runtime"],
        **({"failure_recovery": config["failure_recovery"]} if "failure_recovery" in config else {}),
    }
    client = client_factory(config["models"][profile])
    client.check_credentials()
    tasks = []
    for query in dataset["queries"]:
        if query["query_id"] not in ids:
            continue
        payload = {
            "query": {key: query.get(key) for key in ("query_text", "language", "locale", "query_issued_at")},
            "evaluation_as_of": dataset["manifest"]["evaluation_as_of"],
        }
        metadata = {"kind": "query_spec", "query_id": query["query_id"],
                    "model_profile": profile, "input_hash": digest(payload),
                    "prompt_hash": digest(system), "preparation_hash": digest(identity)}
        tasks.append({**metadata, "task_id": digest(metadata), "system": system,
                      "payload": payload, "blocked_reason": None})
    with run_directory(output, identity, resume) as root:
        outcomes = execute_tasks(root, tasks, {profile: client}, config, retry_failed, progress)
        rows = []
        query_map = {q["query_id"]: q for q in dataset["queries"]}
        for outcome in outcomes:
            qid = outcome["query_id"]
            row = {
                "query_id": qid, "query_text": query_map[qid]["query_text"],
                "dataset_version": dataset["manifest"]["dataset_version"],
                "status": "ready" if outcome["status"] == "success" else outcome["status"],
                "simulated": outcome["simulated"], "spec": outcome["judgment"],
                "reference_answer": references.get(qid),
                "provenance": {"task_id": outcome["task_id"], "prompt_hash": digest(system),
                               "model_profile": profile, "call": outcome.get("call"),
                               **({"auto_recovery": outcome["auto_recovery"]} if outcome.get("auto_recovery") else {})},
            }
            row["spec_hash"] = digest({key: row[key] for key in
                                       ("query_id", "query_text", "dataset_version", "status",
                                        "simulated", "spec", "reference_answer")})
            rows.append(row)
        rows.sort(key=lambda row: row["query_id"])
        write_jsonl(root / "query_specs.jsonl", rows)
        summary = {
            "status": "ready" if all(row["status"] == "ready" for row in rows) else "partial",
            "dataset_version": dataset["manifest"]["dataset_version"],
            "query_count": len(rows), "ready_count": sum(row["status"] == "ready" for row in rows),
            "simulated": client.profile["protocol"] == "mock",
            "auto_recovery": recovery_summary(outcomes),
            "file_sha256": file_digest(root / "query_specs.jsonl"),
        }
        checked_write(root / "summary.json", summary)
        return summary


def load_specs(path, dataset):
    if path is None:
        return {}
    root = Path(path)
    summary = checked_read(root / "summary.json")
    if summary["dataset_version"] != dataset["manifest"]["dataset_version"]:
        raise InputError("Query specs belong to another dataset version")
    if summary["file_sha256"] != file_digest(root / "query_specs.jsonl"):
        raise InputError("Query specs file checksum mismatch")
    queries = {q["query_id"]: q for q in dataset["queries"]}
    records = {}
    for _, row in read_jsonl(root / "query_specs.jsonl"):
        qid = row["query_id"]
        if qid not in queries or qid in records or row["query_text"] != queries[qid]["query_text"]:
            raise InputError("Invalid query specification binding")
        expected = digest({key: row[key] for key in
                           ("query_id", "query_text", "dataset_version", "status",
                            "simulated", "spec", "reference_answer")})
        if row["spec_hash"] != expected or row["dataset_version"] != summary["dataset_version"]:
            raise InputError("Query specification checksum/version mismatch")
        if row["status"] == "ready":
            validate_spec(row["spec"])
        records[qid] = row
    return records
