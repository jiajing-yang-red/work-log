import itertools
from collections import Counter, defaultdict

from quality_input.common import InputError, canonical, digest, write_json, write_jsonl
from quality_input.pipeline import load_dataset, utc_now
from quality_llm import LLMClient
from . import __version__
from .execution import execute_tasks
from .query_specs import load_specs, select_queries
from .schemes import DIMENSIONS, SCHEMES, make_tasks, prompt_for
from .storage import checked_write, run_directory
from . import block_needs_met as block
from .recovery import accounting, initial_status, max_rounds, recovery_summary


def execution_rows(dataset, config, ids):
    return [
        {"experiment_id": experiment["id"], "query_id": response["query_id"],
         "search_run_id": response["search_run_id"], "response_id": response["response_id"],
         "collection_status": response["status"], "result_count": response["actual_result_count"]}
        for experiment in config["experiments"] for response in dataset["responses"]
        if response["query_id"] in ids
    ]


def configured_specs(config, specs_path, dataset):
    if config.get("query_understanding", "frozen") == "direct":
        if specs_path is not None:
            raise InputError("Direct query judging does not use frozen specs; omit --specs")
        return {}
    return load_specs(specs_path, dataset)


def plan(dataset_path, config, specs_path=None, limit=None):
    dataset = load_dataset(dataset_path)
    ids = select_queries(dataset, limit)
    specs = configured_specs(config, specs_path, dataset)
    tasks = make_tasks(dataset, config, specs, ids)
    by_experiment = {}
    for experiment in config["experiments"]:
        subset = [task for task in tasks if task["experiment_id"] == experiment["id"]]
        blocks = Counter(task["blocked_reason"] for task in subset if task["blocked_reason"])
        too_large = sum(not task["blocked_reason"] and
                        len(task["system"]) + len(canonical(task["payload"])) > config["runtime"]["max_input_chars"]
                        for task in subset)
        available = len(subset) - sum(blocks.values()) - too_large
        by_experiment[experiment["id"]] = {
            "tasks": len(subset), "by_kind": dict(Counter(task["kind"] for task in subset)),
            "unavailable": dict(blocks), "input_too_large": too_large,
            "planned_model_invocations": available,
            "maximum_model_invocations": available * max_rounds(config),
            "maximum_recovery_rounds_per_task": max_rounds(config),
            "maximum_http_attempts": available * max_rounds(config) * config["models"][experiment["model"]]["max_attempts"]
            if config["models"][experiment["model"]]["protocol"] != "mock" else 0,
            "simulated": config["models"][experiment["model"]]["protocol"] == "mock",
            "input_too_large_records": [{"input_index": t.get("input_index"), "source_ref": t.get("source_ref"),
                                         "task_id": t["task_id"]} for t in subset if not t["blocked_reason"] and
                                         len(t["system"]) + len(canonical(t["payload"])) > config["runtime"]["max_input_chars"]],
        }
    return {
        "dataset_version": dataset["manifest"]["dataset_version"],
        "query_understanding": config.get("query_understanding", "frozen"),
        "selected_queries": len(ids), "excluded_queries": len(dataset["queries"]) - len(ids),
        "query_specs_available": sum(qid in specs and specs[qid]["status"] == "ready" for qid in ids),
        "experiments": by_experiment,
        "query_executions": dict(Counter(row["collection_status"] for row in execution_rows(dataset, config, ids))),
        "network_calls_made": 0,
    }


def flatten(outcomes):
    rows = []
    for outcome in outcomes:
        base = {key: outcome[key] for key in (
            "task_id", "experiment_id", "scheme", "scheme_version", "model_profile",
            "mode", "repeat", "kind", "query_id", "search_run_id", "response_id",
            "dataset_version", "query_spec_hash", "prompt_hash", "input_hash", "rating_target",
            "simulated",
        )}
        base["task_status"] = outcome["status"]
        base["model_id"] = outcome.get("model_id")
        if "prompt_kind" in outcome:
            base["prompt_kind"] = outcome["prompt_kind"]
        base["query_understanding"] = outcome.get("query_understanding", "frozen")
        base["query_assessment"] = (outcome.get("judgment") or {}).get("query_assessment")
        base["sampling_attempt"] = outcome.get("sampling_attempt", 1)
        base["initial_task_status"] = (outcome.get("history") or [{"status": initial_status(outcome)}])[0]["status"]
        if outcome.get("auto_recovery"):
            base["auto_recovery"] = {k: outcome["auto_recovery"][k] for k in (
                "version", "initial_status", "attempt_count", "recovered", "stop_reason")}
            base["effective_prompt_hash"] = outcome.get("effective_prompt_hash", outcome["prompt_hash"])
        base["validation_version"] = outcome.get("validation_version")
        base["raw_output_status"] = outcome.get("raw_output_status", outcome["status"])
        if outcome["kind"] == block.KIND:
            native = (outcome.get("skill_output") or {}).get("results", [None])[0]
            base.update(input_index=outcome.get("input_index"), source_ref=outcome.get("source_ref"),
                        skill_result=native, skill_version=block.VERSION)

        def diagnostics(item):
            return {
                "error_code": item.get("error_code", outcome.get("error_code")),
                "validation_issues": item.get("validation_issues", []),
                "evidence_repairs": item.get("evidence_repairs", []),
                "model_reason": item.get("model_reason"),
                **({"decision": item["decision"]} if "decision" in item else {}),
            }

        if outcome["kind"] == "duplicates" and outcome["judgment"] is not None:
            for entry in outcome["judgment"]["rows"]:
                rows.append({**base, "result_id": entry["result_id"], "dimension": "duplicate",
                             "judgment_status": entry["status"],
                             "value": entry["duplicate_of"] is not None if entry["status"] == "rated" else None,
                             "duplicate_of": entry["duplicate_of"], "reason": entry["reason"],
                             "evidence": entry["evidence"], **diagnostics(entry)})
            continue
        for result_id in outcome["result_ids"]:
            for dimension in DIMENSIONS[outcome["kind"]]:
                judgment = (outcome.get("judgment") or {}).get("dimensions", {}).get(dimension)
                rows.append({
                    **base, "result_id": result_id, "dimension": dimension,
                    "judgment_status": judgment["status"] if judgment else
                    ("error" if outcome["kind"] in ("legacy_url", "duplicates") and outcome["status"] == "input_too_large"
                     else "unjudgeable" if outcome["status"] in ("unjudgeable", "input_too_large") else "error"),
                    "value": judgment["value"] if judgment else None,
                    "reason": judgment["reason"] if judgment else outcome.get("reason", "No judgment"),
                    "evidence": judgment["evidence"] if judgment else [],
                    **diagnostics(judgment or {}),
                })
    return rows


def compare_rows(rows):
    """Matched model agreement is diagnostic, never an accuracy ranking."""
    experiments = sorted({row["experiment_id"] for row in rows})
    pairs = []
    index = defaultdict(dict)
    for row in rows:
        key = (row.get("query_understanding", "frozen"), row.get("validation_version")) + tuple(row[k] for k in ("scheme", "scheme_version", "mode", "repeat", "kind",
                                    "query_id", "result_id", "dimension", "query_spec_hash",
                                    "prompt_hash", "input_hash"))
        index[row["experiment_id"]][key] = row
    for left, right in itertools.combinations(experiments, 2):
        common = set(index[left]) & set(index[right])
        if not common:
            continue
        grouped = defaultdict(list)
        for key in common:
            a, b = index[left][key], index[right][key]
            grouped[a["dimension"]].append((a, b))
        dimensions = {}
        differences = []
        for dimension, matches in grouped.items():
            rated = [(a, b) for a, b in matches
                     if a["judgment_status"] == b["judgment_status"] == "rated"
                     and not a["simulated"] and not b["simulated"]
                     and a.get("sampling_attempt", 1) == b.get("sampling_attempt", 1) == 1]
            agreed = sum(type(a["value"]) is type(b["value"]) and a["value"] == b["value"] for a, b in rated)
            dimensions[dimension] = {
                "matched_records": len(matches), "both_rated_real": len(rated),
                "explicitly_resampled_records": sum(a.get("sampling_attempt", 1) > 1 or
                                                    b.get("sampling_attempt", 1) > 1 for a, b in matches),
                "automatically_retried_records": sum((a.get("auto_recovery") or {}).get("attempt_count", 1) > 1 or
                                                     (b.get("auto_recovery") or {}).get("attempt_count", 1) > 1 for a, b in matches),
                "exact_agreement": agreed / len(rated) if rated else None,
                "left_status": dict(Counter(a["judgment_status"] for a, _ in matches)),
                "right_status": dict(Counter(b["judgment_status"] for _, b in matches)),
                "left_evidence_repaired_records": sum(bool(a.get("evidence_repairs")) for a, _ in matches),
                "right_evidence_repaired_records": sum(bool(b.get("evidence_repairs")) for _, b in matches),
            }
            for a, b in rated:
                if type(a["value"]) is not type(b["value"]) or a["value"] != b["value"]:
                    differences.append({"query_id": a["query_id"], "result_id": a["result_id"],
                                        "dimension": dimension, "repeat": a["repeat"],
                                        "left_value": a["value"], "right_value": b["value"],
                                        "left_task_id": a["task_id"], "right_task_id": b["task_id"]})
        pairs.append({"left": left, "right": right, "dimensions": dimensions,
                      "disagreements": sorted(differences, key=canonical)})
    return {"human_accuracy_computed": False,
            "note": "Agreement is not correctness. Simulated and explicitly resampled judgments are excluded. Automatic recoveries are included and counted separately; effective retry prompts can differ. Different base schemes/modes/prompts are not merged.",
            "pairs": pairs}


def run(dataset_path, config, specs_path, output, *, limit=None, resume=False,
        retry_failed=False, progress=None, client_factory=LLMClient):
    if not config["experiments"]:
        raise InputError("At least one experiment is required")
    dataset = load_dataset(dataset_path)
    ids = select_queries(dataset, limit)
    specs = configured_specs(config, specs_path, dataset)
    active_profiles = {experiment["model"] for experiment in config["experiments"]}
    if any(config["models"][profile]["protocol"] != "mock" for profile in active_profiles):
        if any(specs.get(qid, {}).get("simulated") for qid in ids):
            raise InputError("Real judges cannot consume simulated query specs; prepare with a real model")
    tasks = make_tasks(dataset, config, specs, ids)
    identity = {
        "kind": "evaluation", "implementation_version": __version__,
        "dataset_version": dataset["manifest"]["dataset_version"],
        "query_ids": ids, "config": config,
        "task_ids": [task["task_id"] for task in tasks],
        "prompt_hashes": {task.get("prompt_kind", task["kind"]): task["prompt_hash"] for task in tasks},
    }
    clients = {name: client_factory(config["models"][name]) for name in active_profiles}
    # Missing API credentials fail before creating a partial paid run.
    for name, client in clients.items():
        if any(task["model_profile"] == name and not task["blocked_reason"] for task in tasks):
            client.check_credentials()
    with run_directory(output, identity, resume) as root:
        started = utc_now()
        outcomes = execute_tasks(root, tasks, clients, config, retry_failed, progress)
        rows = flatten(outcomes)
        write_jsonl(root / "judgments.jsonl", rows)
        if any(task["kind"] == block.KIND for task in tasks):
            write_jsonl(root / "block_content_needs_met.jsonl", block.native_records(outcomes))
        write_jsonl(root / "query_executions.jsonl", execution_rows(dataset, config, ids))
        write_json(root / "model_comparison.json", compare_rows(rows))
        details = {}
        for experiment in config["experiments"]:
            subset = [outcome for outcome in outcomes if outcome["experiment_id"] == experiment["id"]]
            selected = [row for row in rows if row["experiment_id"] == experiment["id"]]
            calls = [record for outcome in subset if not outcome["cache_hit"]
                     for record in outcome.get("new_call_records", [accounting(outcome)])]
            details[experiment["id"]] = {
                "model_profile": experiment["model"],
                "model_id": config["models"][experiment["model"]]["model"],
                "task_status": dict(Counter(task["status"] for task in subset)),
                "initial_task_status": dict(Counter((task.get("history") or [{"status": initial_status(task)}])[0]["status"] for task in subset)),
                "auto_recovery": recovery_summary(subset),
                "explicitly_resampled_tasks": sum(task.get("sampling_attempt", 1) > 1 for task in subset),
                "tasks_with_evidence_repairs": sum(bool(task.get("evidence_repairs")) for task in subset),
                "evidence_repair_count": sum(len(task.get("evidence_repairs", [])) for task in subset),
                "raw_output_status": dict(Counter(task.get("raw_output_status", task["status"]) for task in subset)),
                "dimension_status": {
                    dimension: dict(Counter(row["judgment_status"] for row in selected if row["dimension"] == dimension))
                    for dimension in sorted({row["dimension"] for row in selected})
                },
                "cache_hits": sum(task["cache_hit"] for task in subset),
                "new_completed_model_calls": sum(call["completed"] for call in calls),
                "new_http_attempts": sum(call["http_attempts"] for call in calls),
                "new_call_latency_seconds": sum(call["latency_seconds"] for call in calls),
                "reported_new_input_tokens": sum(call["input_tokens"] for call in calls),
                "reported_new_output_tokens": sum(call["output_tokens"] for call in calls),
                "usage_note": "Token sums include only returned usage; failed calls may have unknown charges.",
            }
        statuses = Counter(outcome["status"] for outcome in outcomes)
        summary = {
            "status": "partial" if any(statuses[s] for s in ("error", "invalid_output", "partial_output", "input_too_large")) else "completed",
            "query_understanding": config.get("query_understanding", "frozen"),
            "started_at": started, "completed_at": utc_now(), "selected_query_count": len(ids),
            "excluded_query_count": len(dataset["queries"]) - len(ids),
            "task_count": len(tasks), "judgment_record_count": len(rows),
            "all_models_simulated": all(client.profile["protocol"] == "mock" for client in clients.values()),
            "experiments": details,
            "auto_recovery": recovery_summary(outcomes),
            "quality_accuracy_claimed": False,
            "note": "completed means execution finished; unjudgeable records remain explicit, not zero.",
        }
        checked_write(root / "summary.json", summary)
        return summary
