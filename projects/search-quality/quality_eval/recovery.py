"""Bounded output recovery, with durable attempts and unchanged valid judgments."""
import copy
import threading
import time
from collections import Counter

from quality_input.common import InputError, canonical, digest, parse_json
from quality_llm.retry import backoff_seconds
from .evidence import validate_output
from .storage import checked_read, checked_write

VERSION = "failure-recovery-v1"
DEFAULT_POLICY = {"enabled": True, "max_attempts": 3, "max_output_tokens": 32768}
_PROMPT_LOCK = threading.Lock()
FAILED = {"error", "invalid_output", "partial_output"}


def validate_policy(value):
    if not isinstance(value, dict) or set(value) - set(DEFAULT_POLICY):
        raise InputError("Unknown failure_recovery options")
    policy = {**DEFAULT_POLICY, **value}
    if type(policy["enabled"]) is not bool:
        raise InputError("failure_recovery.enabled must be boolean")
    if type(policy["max_attempts"]) is not int or not 1 <= policy["max_attempts"] <= 5:
        raise InputError("failure_recovery.max_attempts must be in 1..5, including the initial call")
    if type(policy["max_output_tokens"]) is not int or not 1 <= policy["max_output_tokens"] <= 100000:
        raise InputError("Invalid failure_recovery.max_output_tokens")
    return policy


def policy_for(config):
    # Old frozen jobs must keep their historical execution policy and identity.
    return validate_policy(config["failure_recovery"]) if "failure_recovery" in config else {**DEFAULT_POLICY, "enabled": False}


def max_rounds(config):
    policy = policy_for(config)
    return policy["max_attempts"] if policy["enabled"] else 1


def freeze_request(root, outcome):
    request = outcome.get("request") or {}
    if "system" not in request:
        return
    system = request["system"]
    key = digest(system)
    with _PROMPT_LOCK:
        path = root / "prompts" / (key + ".json")
        path.parent.mkdir(exist_ok=True)
        if not path.exists():
            checked_write(path, {"system": system})
        elif checked_read(path) != {"system": system}:
            raise InputError("Frozen recovery prompt mismatch")
    outcome["request"] = {**{k: v for k, v in request.items() if k != "system"}, "system_ref": key}
    if key != outcome.get("prompt_hash"):
        outcome["effective_prompt_hash"] = key


def diagnose(outcome, budget, policy, protocol):
    """Classify locally; no extra judge call and no untrusted text in feedback."""
    if outcome["status"] == "input_too_large":
        return {"cause": "input_too_large", "action": "stop"}
    if outcome.get("simulated") or outcome["status"] not in FAILED:
        return {"cause": "no_recovery_needed", "action": "stop"}
    code = outcome.get("error_code", "")
    diagnostics = outcome.get("response_diagnostics") or (outcome.get("call") or {}).get("diagnostics") or {}
    attempts = diagnostics.get("attempts") or []
    status = attempts[-1].get("http_status") if attempts else None
    if code == "http_error":
        cause = "transport_retries_exhausted" if status in (408, 429, 500, 502, 503, 504) else "permanent_http_error"
        return {"cause": cause, "action": "stop", "http_status": status}
    if code in ("transport_error", "retry_wait_exhausted"):
        return {"cause": "transport_retries_exhausted", "action": "stop"}
    if code == "output_truncated":
        cap = max(budget, policy["max_output_tokens"])
        if budget >= cap or protocol == "typesafe":
            return {"cause": code, "action": "stop", "stop_reason": "output_budget_cap"}
        return {"cause": code, "action": "increase_output_budget", "next_max_output_tokens": min(budget * 2, cap)}
    if code in ("empty_response", "incomplete_response", "stream_total_timeout", "stream_idle_timeout",
                "stream_interrupted", "stream_malformed", "stream_incomplete", "stream_provider_error",
                "stream_timeout_or_interrupted", "recovery_interrupted"):
        # Embedded provider refusals / permanent API errors must also stop.
        response = (attempts[-1].get("response") or {}) if attempts else {}
        error = response.get("error") or {}
        permanent = {"400", "401", "402", "403", "404", "422", "invalid_api_key", "insufficient_quota", "insufficient_balance",
                     "content_filter", "authentication_error", "authorization_error", "invalid_request_error", "permission_denied", "model_not_found"}
        if response.get("refusal") or any(str(error.get(k, "")).lower() in permanent for k in ("code", "type", "status")):
            return {"cause": "permanent_provider_error", "action": "stop"}
        return {"cause": code, "action": "retry_response"}
    if code == "invalid_json":
        return {"cause": code, "action": "correct_json"}
    if outcome["status"] in ("invalid_output", "partial_output"):
        evidence = bool(outcome.get("validation_issues")) or any(
            word in outcome.get("reason", "").lower() for word in ("evidence", "citation", "quote", "substring"))
        return {"cause": "evidence_invalid" if evidence else "output_contract_invalid",
                "action": "correct_evidence" if evidence else "correct_schema"}
    return {"cause": code or "unknown_failure", "action": "stop"}


FEEDBACK = {
    "correct_json": "上次输出无法解析为 JSON。请只返回一个符合文末 Schema 的 JSON 对象，不添加 Markdown、解释前缀或尾随文字。",
    "correct_schema": "上次输出未通过结构或字段一致性校验。请严格遵守文末 Schema 的必填字段、类型、枚举和关联要求，不增加字段。",
    "correct_evidence": "上次输出的证据未通过校验。请仅逐字复制本条输入中实际存在的非空文字，并使用对应字段或路径；不得改写、翻译、拼接或虚构引文。证据不足按原规则处理。",
}


def recovery_system(system, action, protocol):
    if action not in FEEDBACK or protocol == "typesafe":
        return system
    feedback = "\n\n输出校验反馈：\n" + FEEDBACK[action] + "\n评分定义、证据范围与输入不变；不得为了通过校验而改变评分标准。\n"
    marker = "\nOutput JSON schema:\n"
    # Keep the schema as the last parseable JSON segment for request inspection.
    if marker in system:
        before, after = system.rsplit(marker, 1)
        return before + feedback + marker + after
    return system + feedback


def preserve_valid(previous, latest, task, domains):
    if not previous or previous["status"] != "partial_output" or not previous.get("judgment"):
        return latest
    old = previous["judgment"]
    key = "rows" if task["kind"] == "duplicates" else "dimensions"
    valid = ({item["result_id"]: item for item in old[key] if item["status"] != "error"}
             if key == "rows" else {name: item for name, item in old[key].items() if item["status"] != "error"})
    # Keep the original query assessment / requirements as well as valid scores.
    try:
        raw = parse_json(latest["call"]["text"])
        combined = copy.deepcopy(old)
        combined[key] = copy.deepcopy(raw[key])
        clean = lambda item: {k: v for k, v in item.items() if k != "evidence_repairs"}
        if key == "rows":
            combined[key] = [clean(valid[item["result_id"]]) if item["result_id"] in valid else item for item in combined[key]]
        else:
            combined[key].update({name: clean(item) for name, item in valid.items()})
        merged = validate_output(task["kind"], combined, task["payload"], domains)
        found = ({item["result_id"]: item for item in merged["judgment"][key]} if key == "rows" else merged["judgment"][key])
        if any(found.get(name) != clean(item) for name, item in valid.items()):
            raise ValueError("Recovery conflicts with already validated scopes")
        for name, item in valid.items():
            found[name].update(copy.deepcopy(item))
        result = {**latest, **merged}
        result.pop("error_code", None)
        result.pop("reason", None)
        result["evidence_repairs"] = [r for item in found.values() for r in item.get("evidence_repairs", [])]
    except (ValueError, KeyError, TypeError, IndexError):
        result = {**latest, **{k: previous[k] for k in (
            "status", "judgment", "validation_version", "raw_output_status", "evidence_repairs", "validation_issues") if k in previous}}
        result.update(error_code="partial_recovery_conflict", reason="Recovery did not repair failed scopes while preserving valid judgments")
    result["retained_valid_scopes"] = sorted(valid)
    return result


def accounting(outcome):
    call = outcome.get("call") or {}
    diag = outcome.get("response_diagnostics") or call.get("diagnostics") or {}
    attempts = diag.get("attempts") or []
    usages = [(item.get("response") or {}).get("usage") or {} for item in attempts]
    if not any(usages) and call.get("usage"):
        usages = [call["usage"]]
    def tokens(keys):
        return sum(next((u[k] for k in keys if isinstance(u.get(k), (int, float))), 0) for u in usages)
    return {
        "completed": bool(call) and not outcome.get("simulated"),
        "http_attempts": len(attempts) or call.get("attempts", outcome.get("attempts", 0)),
        "input_tokens": tokens(("input_tokens", "prompt_tokens", "promptTokenCount")),
        "output_tokens": tokens(("output_tokens", "completion_tokens", "candidatesTokenCount")),
        "latency_seconds": call.get("latency_seconds", sum(a.get("latency_seconds", 0) for a in attempts)),
        "usage_unknown": not outcome.get("simulated") and bool(attempts or call or outcome.get("error_code") == "recovery_interrupted") and (not usages or any(not u for u in usages)),
    }


def initial_status(outcome):
    return (outcome.get("auto_recovery") or {}).get("initial_status", outcome["status"])


def recovery_summary(outcomes):
    records = [o["auto_recovery"] for o in outcomes if o.get("auto_recovery")]
    return {
        "retried_tasks": sum(r["attempt_count"] > 1 for r in records),
        "recovered_tasks": sum(r["recovered"] for r in records),
        "initial_failures": sum(r["initial_status"] in FAILED for r in records),
        "remaining_failed_tasks": sum(o["status"] in FAILED for o in outcomes),
        "stop_reasons": dict(Counter(r["stop_reason"] for r in records if r["initial_status"] in FAILED)),
    }


def execute_with_recovery(root, task, client, config, cycle, execute):
    policy = policy_for(config)
    args = (config["runtime"]["max_input_chars"], config["approved_authority_domains"])
    if not policy["enabled"] or client.profile["protocol"] == "mock":
        return execute(task, client, *args)
    folder = root / "recovery" / task["task_id"] / ("cycle-" + str(cycle))
    folder.mkdir(parents=True, exist_ok=True)
    final_path = folder / "final.json"
    if final_path.exists():
        return {**checked_read(final_path), "new_call_records": []}
    budget = client.profile["max_output_tokens"]
    system, action = task["system"], "initial"
    records, new_calls, best, stop, waited = [], [], None, None, 0.0
    for number in range(1, policy["max_attempts"] + 1):
        path = folder / f"attempt-{number}.json"
        reservation = folder / f"attempt-{number}.reserved.json"
        request = {"system": system, "payload": task["payload"], "max_output_tokens": budget}
        if reservation.exists():
            reserved = checked_read(reservation)
            if reserved["request_hash"] != digest(request):
                raise InputError("Recovery reservation mismatch")
            waited = max(waited, reserved.get("cumulative_wait_seconds", 0))
        if path.exists():
            latest = checked_read(path)
        else:
            if reservation.exists():
                # A dispatched request may have been charged even if its reply was lost.
                latest = {**{k: v for k, v in task.items() if k not in ("system", "payload")},
                          "status": "error", "judgment": None, "call": None, "simulated": False,
                          "request": request, "error_code": "recovery_interrupted",
                          "reason": "Process stopped after reserving an attempt; response and usage unknown"}
            else:
                if number > 1:
                    delay = backoff_seconds(number - 1, client.profile.get("retry_base_seconds", 4), client.profile.get("retry_max_seconds", 60))
                    if waited + delay > client.profile.get("retry_wait_budget_seconds", 120):
                        stop = "recovery_wait_budget_exhausted"
                        break
                    time.sleep(delay)
                    waited += delay
                # Persist before dispatch, so process restarts cannot reset the budget.
                checked_write(reservation, {"request_hash": digest(request), "action": action, "cumulative_wait_seconds": waited})
                effective = {**task, "system": system}
                latest = execute(effective, client, *args, **({"max_output_tokens": budget} if budget != client.profile["max_output_tokens"] else {}))
                latest["request"]["max_output_tokens"] = budget
                new_calls.append(accounting(latest))
            freeze_request(root, latest)
            checked_write(path, latest)
        best = preserve_valid(best, latest, task, args[1])
        decision = diagnose(latest, budget, policy, client.profile["protocol"])
        if latest["status"] == "success" and best["status"] == "partial_output":
            decision = {"cause": "partial_recovery_conflict", "action": "correct_evidence"}
        records.append({"round": number, "status": latest["status"], "effective_status": best["status"],
                        **decision, "request_action": action, "max_output_tokens": budget,
                        "effective_prompt_hash": digest(system), "path": str(path.relative_to(root)),
                        "accounting": accounting(latest)})
        if best["status"] not in FAILED:
            stop = ("recovered" if best["status"] in ("success", "unjudgeable") else best["status"]) if records[0]["status"] in FAILED else "no_recovery_needed"
            break
        if decision["action"] == "stop":
            stop = decision.get("stop_reason", decision["cause"])
            break
        if number == policy["max_attempts"]:
            stop = "max_attempts"
            break
        budget = decision.get("next_max_output_tokens", budget)
        action = decision["action"]
        system = recovery_system(task["system"], action, client.profile["protocol"])
    best["auto_recovery"] = {
        "version": VERSION, "policy": policy, "initial_status": records[0]["status"],
        "attempt_count": len(records), "recovered": stop == "recovered", "stop_reason": stop,
        "recovery_wait_seconds": waited, "attempts": records,
    }
    best["new_call_records"] = new_calls
    checked_write(final_path, best)
    return best
