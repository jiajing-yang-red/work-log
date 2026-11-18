"""TypeSafe Choice wire shape. No generated explanation or citation is invented."""

import json

from quality_input.common import canonical


def parse_choice_prompt(text, *, labels=None):
    """Validate the editable native question; labels stay compatible with metrics."""
    expected = set(labels) if labels is not None else {"-1", "0", "1", "2", "3"}
    def unique_keys(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("Jev Prompt JSON 不能包含重复字段。")
            value[key] = item
        return value

    try:
        question = json.loads(text, object_pairs_hook=unique_keys)
    except (ValueError, TypeError) as exc:
        raise ValueError("Jev Prompt 必须是有效 JSON，且不能包含重复字段。") from exc
    if (not isinstance(question, dict) or set(question) != {"type", "instructions", "criteria"}
            or question["type"] != "choice"):
        raise ValueError("Jev Prompt 必须包含 type=choice、instructions 和 criteria。")
    instructions = question["instructions"]
    structured = (labels is not None and isinstance(instructions, list) and instructions
                  and all(isinstance(x, str) and x.strip() for x in instructions))
    if not structured and (not isinstance(instructions, str) or not instructions.strip()):
        raise ValueError("Jev instructions 必须是非空文本。")
    criteria = question["criteria"]
    if (not isinstance(criteria, dict) or set(criteria) != expected
            or any(not isinstance(v, str) or not v.strip() for v in criteria.values())):
        raise ValueError("Jev criteria 必须为指定的每个选项提供非空定义：" + ", ".join(sorted(expected)))
    return question


def choice_request(model, system, payload, schema):
    try:
        dimensions = schema["properties"]["dimensions"]["properties"]
        if set(dimensions) == {"block_content_needs_met"}:
            values = dimensions["block_content_needs_met"]["properties"]["value"]["enum"]
            if values != [0, 1, 2, 3, 4, None]:
                raise ValueError("Jev Needs Met requires grades 0..4 and an insufficient-evidence state")
            question = parse_choice_prompt(system, labels=["0", "1", "2", "3", "4", "insufficient_evidence"])
            return {"model": model, "state": payload,
                    "questions": {"block_content_needs_met": question}}
        values = dimensions["answerability"]["properties"]["value"]["enum"]
    except (KeyError, TypeError):
        raise ValueError("Jev only supports answerability scoring") from None
    if set(dimensions) != {"answerability"} or values != [-1, 0, 1, 2, 3]:
        raise ValueError("Jev requires the five answerability grades -1..3")
    if system.lstrip().startswith("{"):
        return {"model": model, "state": payload,
                "questions": {"answerability": parse_choice_prompt(system)}}
    # Compatibility for historical captured prompts and older direct API callers.
    # New SearchBench tasks use answerability_jev, never this generative fallback.
    rules = system.partition("\nOutput JSON schema:\n")[0]
    instructions = rules + (
        "\n\n【本次原生 Choice 调用约定】\n"
        "按上述评分规则判断 state.result 对 state.query 的可回答性，选择唯一分档。"
        "评分规则中的 JSON、reason 和 evidence 是生成式接口的输出要求，本次不适用；"
        "本次只选择 criteria 中的分数，不生成理由、引用或 JSON 文本。"
        "criteria 的分档语义以以上评分规则为准。"
    )
    return {"model": model, "state": payload, "questions": {"answerability": {
        "type": "choice", "instructions": instructions,
        "criteria": {str(value): f"可回答性 {value} 分；按评分规则中该分数的定义判断。" for value in values},
    }}}


def visible_response(data):
    """Project only decision fields, usage and identifiers, excluding private extensions."""
    if not isinstance(data, dict):
        return {}
    result = {k: data[k] for k in ("model", "id", "request_id") if isinstance(data.get(k), str)}
    if "answers" in data:
        answers = data["answers"]
        result["answers"] = {
            key: {k: value[k] for k in ("type", "choice", "confidence", "probabilities") if k in value}
            if isinstance(value, dict) else value for key, value in answers.items()
        } if isinstance(answers, dict) else answers
    if isinstance(data.get("usage"), dict):
        result["usage"] = {k: v for k, v in data["usage"].items() if type(v) in (int, float)}
    if isinstance(data.get("error"), dict):
        result["error"] = {k: v for k, v in data["error"].items()
                           if k in ("code", "type", "message") and isinstance(v, (str, int))}
    return result


def extract_choice_response(data):
    visible = visible_response(data)
    if not isinstance(visible.get("answers"), dict) or data.get("error"):
        raise ValueError("Jev response is missing answers")
    return {"text": canonical(visible), "usage": visible.get("usage", {}),
            "returned_model": visible.get("model"),
            "response_id": visible.get("id", visible.get("request_id"))}
