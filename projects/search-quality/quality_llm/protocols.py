"""Wire adapters. No scoring rules or model names belong in this module."""

from urllib.parse import quote

from quality_input.common import canonical
from .systemone import choice_request, extract_choice_response, visible_response


PROTOCOLS = {"mock", "openai_responses", "openai_chat", "anthropic", "gemini", "typesafe"}
DEFAULT_URLS = {
    "openai_responses": "https://api.openai.com/v1",
    "openai_chat": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta",
}


def build_request(profile, system, payload, schema, api_key):
    protocol = profile["protocol"]
    base = profile["base_url"].rstrip("/")
    model = profile["model"]
    headers = {"Content-Type": "application/json"}
    user = canonical(payload)
    parameters = profile["parameters"].copy()
    limit = profile["max_output_tokens"]
    output_mode = profile["output_mode"]
    if protocol == "typesafe":
        headers["Authorization"] = "Bearer " + api_key
        return base + "/systemone", headers, choice_request(model, system, payload, schema)
    if protocol == "openai_responses":
        headers["Authorization"] = "Bearer " + api_key
        body = {"model": model, "instructions": system, "input": user,
                "max_output_tokens": limit, "store": False, "truncation": "disabled", **parameters}
        if output_mode == "json_schema":
            body["text"] = {"format": {"type": "json_schema", "name": "evaluation",
                                      "schema": schema, "strict": True}}
        elif output_mode == "json_object":
            body["text"] = {"format": {"type": "json_object"}}
        return base + "/responses", headers, body
    if protocol == "openai_chat":
        if api_key:
            headers["Authorization"] = "Bearer " + api_key
        body = {"model": model, "messages": [{"role": "system", "content": system},
                                             {"role": "user", "content": user}],
                profile["token_limit_field"]: limit, "stream": profile.get("stream", False), **parameters}
        if body["stream"] and profile.get("stream_include_usage", True):
            body["stream_options"] = {"include_usage": True}
        if output_mode == "json_schema":
            body["response_format"] = {"type": "json_schema", "json_schema": {
                "name": "evaluation", "schema": schema, "strict": True}}
        elif output_mode == "json_object":
            body["response_format"] = {"type": "json_object"}
        return base + "/chat/completions", headers, body
    if protocol == "anthropic":
        headers.update({"x-api-key": api_key, "anthropic-version": "2023-06-01"})
        body = {"model": model, "system": system, "max_tokens": limit,
                "messages": [{"role": "user", "content": user}], **parameters}
        return base + "/messages", headers, body
    if protocol == "gemini":
        headers["x-goog-api-key"] = api_key
        body = {"systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": user}]}],
                "generationConfig": {"maxOutputTokens": limit, **parameters}}
        return base + "/models/" + quote(model, safe="") + ":generateContent", headers, body
    raise ValueError("Mock requests do not use HTTP")


def extract_response(protocol, data):
    """Return visible answer text only, usage and actual returned model.

    Reasoning/thinking blocks are deliberately not copied into artifacts.
    """
    if protocol == "typesafe":
        return extract_choice_response(data)
    if protocol == "openai_responses":
        if data.get("status") != "completed":
            raise ValueError("Response did not complete")
        text = []
        for item in data.get("output", []):
            if item.get("type") == "message":
                for part in item.get("content", []):
                    if part.get("type") == "refusal":
                        raise ValueError("Model refused the request")
                    if part.get("type") == "output_text":
                        text.append(part["text"])
        usage = data.get("usage", {})
    elif protocol == "openai_chat":
        if "stream_info" in data and not data["stream_info"].get("completed"):
            raise ValueError("Chat response stream did not complete")
        choice = data["choices"][0]
        if choice.get("finish_reason") != "stop" or choice["message"].get("refusal"):
            raise ValueError("Chat response refused or did not finish normally")
        text = [choice["message"]["content"]]
        raw_usage = data.get("usage", {})
        usage = {"input_tokens": raw_usage.get("prompt_tokens"),
                 "output_tokens": raw_usage.get("completion_tokens"),
                 "total_tokens": raw_usage.get("total_tokens")}
    elif protocol == "anthropic":
        if data.get("stop_reason") != "end_turn":
            raise ValueError("Message did not finish normally")
        text = [part["text"] for part in data.get("content", []) if part.get("type") == "text"]
        usage = data.get("usage", {})
    elif protocol == "gemini":
        candidate = data["candidates"][0]
        if candidate.get("finishReason") != "STOP":
            raise ValueError("Gemini response blocked or incomplete")
        text = [part["text"] for part in candidate["content"]["parts"]
                if "text" in part and not part.get("thought", False)]
        raw_usage = data.get("usageMetadata", {})
        usage = {"input_tokens": raw_usage.get("promptTokenCount"),
                 "output_tokens": raw_usage.get("candidatesTokenCount"),
                 "reasoning_tokens": raw_usage.get("thoughtsTokenCount"),
                 "total_tokens": raw_usage.get("totalTokenCount")}
    else:
        raise ValueError("Unknown response protocol")
    if not text or any(not isinstance(part, str) for part in text) or not "".join(text).strip():
        raise ValueError("No visible text in response")
    return {
        "text": "".join(text), "usage": usage,
        "returned_model": data.get("model", data.get("modelVersion")),
        "response_id": data.get("id", data.get("responseId")),
    }


def response_diagnostics(protocol, data):
    """Project visible output and diagnostic metadata, even for rejected responses.

    This is not the wire body: reasoning/thinking, request echoes, credentials and
    arbitrary provider extensions are excluded. Never use this projection as a score.
    """
    def obj(value):
        return value if isinstance(value, dict) else {}

    def items(value):
        return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []

    def scalar(value):
        return value if isinstance(value, (str, int, float, bool)) else None

    def counts(value):
        # Usage extensions can contain nested token counts, but never free text.
        return {key: counts(val) if isinstance(val, dict) else val
                for key, val in obj(value).items()
                if isinstance(val, dict) or type(val) in (int, float)}

    data = obj(data)
    texts, refusals = [], []
    result = {"text": "", "refusal": None, "finish_reason": None,
              "provider_status": scalar(data.get("status")), "incomplete_reason": None,
              "returned_model": scalar(data.get("model", data.get("modelVersion"))),
              "response_id": scalar(data.get("id", data.get("responseId"))),
              "usage": counts(data.get("usage", data.get("usageMetadata")))}
    if protocol == "typesafe":
        visible = visible_response(data)
        texts.append(canonical(visible))
        result["finish_reason"] = "structured_response" if isinstance(data.get("answers"), dict) else None
        result["response_id"] = scalar(data.get("id", data.get("request_id")))
    elif protocol == "openai_responses":
        result["incomplete_reason"] = scalar(obj(data.get("incomplete_details")).get("reason"))
        for item in items(data.get("output")):
            if item.get("type") == "message":
                for part in items(item.get("content")):
                    if part.get("type") == "output_text":
                        texts.append(part.get("text"))
                    elif part.get("type") == "refusal":
                        refusals.append(part.get("refusal"))
    elif protocol == "openai_chat":
        choice = next(iter(items(data.get("choices"))), {})
        result["finish_reason"] = scalar(choice.get("finish_reason"))
        message = obj(choice.get("message"))
        content = message.get("content")
        texts.extend([content] if isinstance(content, str) else
                     [part.get("text") for part in items(content) if part.get("type") == "text"])
        refusals.append(message.get("refusal"))
    elif protocol == "anthropic":
        result["finish_reason"] = scalar(data.get("stop_reason"))
        texts.extend(part.get("text") for part in items(data.get("content")) if part.get("type") == "text")
    elif protocol == "gemini":
        candidate = next(iter(items(data.get("candidates"))), {})
        result["finish_reason"] = scalar(candidate.get("finishReason"))
        result["incomplete_reason"] = scalar(obj(data.get("promptFeedback")).get("blockReason"))
        texts.extend(part.get("text") for part in items(obj(candidate.get("content")).get("parts"))
                     if not part.get("thought", False))
    result["text"] = "".join(value for value in texts if isinstance(value, str))
    result["refusal"] = "\n".join(value for value in refusals if isinstance(value, str)) or None
    error = obj(data.get("error"))
    if error:
        result["error"] = {key: scalar(error[key]) for key in ("code", "type", "message", "status")
                           if key in error and scalar(error[key]) is not None}
    if isinstance(data.get("stream_info"), dict):
        result["stream_info"] = {key: scalar(data["stream_info"].get(key))
                                 for key in ("completed", "events", "error", "error_code", "timeout_seconds",
                                             "answer_chunks", "reasoning_chunks")}
    return result


def response_failure_code(response):
    """Classify observable response failures; old ambiguous errors stay ambiguous."""
    if not isinstance(response, dict):
        return "incomplete_response"
    stream = response.get("stream_info") or {}
    code = stream.get("error_code")
    if code in {"stream_total_timeout", "stream_idle_timeout", "stream_interrupted", "stream_malformed",
                "stream_incomplete", "stream_provider_error", "response_too_large"}:
        return code
    if stream.get("error") == "Stream interrupted or timed out":
        return "stream_timeout_or_interrupted"
    if stream and not stream.get("completed"):
        return "stream_incomplete"
    finish = response.get("finish_reason")
    if finish in ("length", "max_tokens", "MAX_TOKENS") or response.get("incomplete_reason") == "max_output_tokens":
        return "output_truncated"
    if response.get("refusal") or finish in ("content_filter", "SAFETY", "RECITATION", "refusal"):
        return "model_refused"
    if not response.get("text"):
        return "empty_response"
    return "incomplete_response"
