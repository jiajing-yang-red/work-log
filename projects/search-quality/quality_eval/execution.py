from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED

from quality_input.common import InputError, canonical, parse_json
from quality_llm import LLMError
from .contracts import schema_for
from .evidence import validate_output
from .schemes import simulated_response
from .storage import checked_read, checked_write
from .jev import validate_jev_answer
from . import block_needs_met as block
from .recovery import execute_with_recovery, freeze_request


class BatchPaused(InputError):
    """An account failure stops new tasks after in-flight results are saved."""


def account_failure(outcome):
    if outcome.get("status") != "error":
        return None
    attempts = (outcome.get("response_diagnostics") or {}).get("attempts", [])
    for attempt in reversed(attempts):
        status = attempt.get("http_status")
        if status in (401, 402, 403):
            return {"http_status": status, "reason": {
                401: "API Key 认证失败", 402: "账户余额不足", 403: "接口访问被拒绝",
            }[status]}
    return None


def execute_task(task, client, max_input_chars, authority_domains, *, max_output_tokens=None):
    public = {key: value for key, value in task.items() if key not in ("system", "payload")}
    result = {**public, "status": None, "judgment": None, "call": None,
              "simulated": client.profile["protocol"] == "mock",
              "request": {"system": task["system"], "payload": task["payload"]}}
    if task.get("blocked_reason"):
        legacy = task["kind"] in ("legacy_url", "duplicates")
        result.update(status="error" if legacy else "unjudgeable", reason=task["blocked_reason"])
        if legacy:
            result["error_code"] = "input_unavailable"
        return result
    if len(task["system"]) + len(canonical(task["payload"])) > max_input_chars:
        result.update(status="input_too_large", reason="Input exceeds explicit limit; evidence was not truncated")
        return result
    try:
        contract = task.get("block_output_contract", block.LEGACY_CONTRACT)
        schema = (block.output_schema(contract) if task["kind"] == block.KIND else
                  schema_for(task["kind"], task.get("query_understanding", "frozen")))
        call = client.complete(task["system"], task["payload"],
                               schema,
                               simulated_response(task["kind"], task["payload"]),
                               **({"max_output_tokens": max_output_tokens} if max_output_tokens is not None else {}))
        result["call"] = call
        try:
            answer = parse_json(call["text"])
        except ValueError as exc:
            result.update(status="invalid_output", reason=str(exc), error_code="invalid_json")
            return result
        if task["kind"] == block.KIND:
            if result["simulated"]:
                result.update(status="success", judgment=block.simulation())
            else:
                result.update(block.validate(answer, task["payload"], contract))
        elif client.profile["protocol"] == "typesafe":
            result.update(validate_jev_answer(answer, task["kind"]))
        elif result["simulated"] and task["kind"] in ("legacy_url", "duplicates"):
            # Mock records describe skipped semantic judgment, outside the real-rating schema.
            result.update(status="success", judgment=answer)
        else:
            result.update(validate_output(task["kind"], answer, task["payload"], authority_domains))
    except LLMError as exc:
        result.update(status="error", reason=str(exc), error_code=exc.code, attempts=exc.attempts)
        if exc.diagnostics is not None:
            result["response_diagnostics"] = exc.diagnostics
    except (ValueError, TypeError, KeyError, IndexError) as exc:
        result.update(status="invalid_output", reason=str(exc), error_code="output_contract_invalid")
    return result


def execute_tasks(root, tasks, clients, config, retry_failed=False, progress=None):
    """Coordinator writes final checkpoints; workers own separate recovery logs."""
    outcomes, pending, histories = [], [], {}
    for task in tasks:
        path = root / "tasks" / (task["task_id"] + ".json")
        if not path.exists():
            # An explicit retry may have archived the old checkpoint before an
            # interruption. Resume that same cycle rather than returning cycle 1.
            archived_paths = list((root / "previous_attempts").glob(task["task_id"] + ".*.json"))
            if archived_paths:
                archived = max(archived_paths, key=lambda p: int(p.name.split(".")[-2]))
                saved = checked_read(archived)
                if saved.get("task_id") != task["task_id"]:
                    raise InputError("Archived task checkpoint identity mismatch")
                histories[task["task_id"]] = saved.get("history", []) + [{
                    "path": str(archived.relative_to(root)), "status": saved["status"],
                    "sampling_attempt": saved.get("sampling_attempt", 1),
                }]
        if path.exists():
            saved = checked_read(path)
            if saved.get("task_id") != task["task_id"]:
                raise InputError("Task checkpoint identity mismatch")
            # Resuming is not permission to silently resample failed/invalid judgments.
            if not retry_failed or saved["status"] not in ("error", "invalid_output", "partial_output"):
                outcomes.append({**saved, "cache_hit": True})
                continue
            archive = root / "previous_attempts"
            archive.mkdir(exist_ok=True)
            index = 1
            archived = archive / f"{task['task_id']}.{index}.json"
            while archived.exists():
                index += 1
                archived = archive / f"{task['task_id']}.{index}.json"
            path.rename(archived)
            histories[task["task_id"]] = saved.get("history", []) + [{
                "path": str(archived.relative_to(root)), "status": saved["status"],
                "sampling_attempt": saved.get("sampling_attempt", 1),
            }]
        pending.append(task)
    if progress:
        progress(len(outcomes), len(tasks))
    # One immutable prompt snapshot per hash, not 39,940 copies of the skill.
    prompts = {task["prompt_hash"]: task["system"] for task in tasks if task["kind"] == block.KIND}
    for key, system in prompts.items():
        path = root / "prompts" / (key + ".json")
        if not path.exists():
            path.parent.mkdir(exist_ok=True)
            checked_write(path, {"system": system})
        elif checked_read(path) != {"system": system}:
            raise InputError("Frozen prompt snapshot mismatch")
    workers = config["runtime"]["max_workers"]
    stopped = None
    with ThreadPoolExecutor(max_workers=workers) as pool:
        queue, futures = iter(pending), {}

        def submit_next():
            if stopped is not None:
                return
            task = next(queue, None)
            if task is not None:
                future = pool.submit(execute_with_recovery, root, task, clients[task["model_profile"]],
                                     config, len(histories.get(task["task_id"], [])) + 1, execute_task)
                futures[future] = task

        for _ in range(workers):
            submit_next()
        while futures:
            ready, _ = wait(futures, return_when=FIRST_COMPLETED)
            for future in ready:
                task = futures.pop(future)
                outcome = future.result()
                failure = account_failure(outcome)
                if failure and stopped is None:
                    stopped = {**failure, "task_id": task["task_id"], "model_profile": task["model_profile"]}
                if task["kind"] == block.KIND:
                    freeze_request(root, outcome)
                outcome["history"] = histories.get(task["task_id"], [])
                outcome["sampling_attempt"] = len(outcome["history"]) + 1
                checked_write(root / "tasks" / (task["task_id"] + ".json"), outcome)
                outcomes.append({**outcome, "cache_hit": False})
                if progress:
                    progress(len(outcomes), len(tasks))
            # Inspect all completed results before replenishing the queue, so a
            # simultaneous account failure cannot be hidden by another success.
            for _ in ready:
                submit_next()
    if stopped is not None:
        checked_write(root / "batch_stop.json", {**stopped, "done": len(outcomes), "total": len(tasks)})
        raise BatchPaused(f"{stopped['reason']}（HTTP {stopped['http_status']}），已暂停后续任务并保存在途结果。修复账户后可继续并重试失败项。")
    return sorted(outcomes, key=lambda row: row["task_id"])
