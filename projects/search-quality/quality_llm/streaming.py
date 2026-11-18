"""Collect OpenAI-compatible SSE into the same visible response as non-streaming.

Reasoning deltas are discarded. A broken stream is returned as incomplete so the
caller can save its partial answer, without silently resampling a scored task.
"""
import http.client
import time

from quality_input.common import parse_json


def read_chat_stream(response, timeout):
    answer = {"choices": [{"index": 0, "message": {"content": "", "refusal": None}, "finish_reason": None}],
              "stream_info": {"completed": False, "events": 0, "answer_chunks": 0, "reasoning_chunks": 0,
                              "timeout_seconds": timeout}}
    choice, info = answer["choices"][0], answer["stream_info"]
    texts, refusals, event = [], [], []
    consumed = 0
    deadline = time.monotonic() + timeout

    def consume_event(lines):
        if not lines:
            return False
        payload = "\n".join(lines)
        if payload.strip() == "[DONE]":
            info["completed"] = True
            return True
        data = parse_json(payload)
        if not isinstance(data, dict):
            raise ValueError()
        info["events"] += 1
        for key in ("id", "model"):
            if isinstance(data.get(key), str):
                answer[key] = data[key]
        if isinstance(data.get("usage"), dict):
            answer["usage"] = data["usage"]
        if data.get("error"):
            answer["error"] = data["error"]
            info["error"] = "Provider returned a stream error"
            info["error_code"] = "stream_provider_error"
            return True
        choices = data.get("choices", [])
        if not isinstance(choices, list):
            raise ValueError()
        for part in choices:
            if not isinstance(part, dict):
                raise ValueError()
            if part.get("index", 0) != 0:
                continue
            delta = part.get("delta", {})
            if not isinstance(delta, dict):
                raise ValueError()
            if isinstance(delta.get("reasoning_content"), str) and delta["reasoning_content"]:
                info["reasoning_chunks"] += 1  # Count only; never persist reasoning text.
            for key, target in (("content", texts), ("refusal", refusals)):
                value = delta.get(key)
                if value is not None:
                    if not isinstance(value, str):
                        raise ValueError()
                    target.append(value)
                    if key == "content" and value:
                        info["answer_chunks"] += 1
            if part.get("finish_reason") is not None:
                choice["finish_reason"] = part["finish_reason"]
        return False

    try:
        while True:
            if time.monotonic() > deadline:
                info.update(error="Stream exceeded the total generation time limit", error_code="stream_total_timeout")
                break
            raw = response.readline(1024 * 1024 + 1)
            if not raw:
                if event:
                    consume_event(event)
                break
            consumed += len(raw)
            if len(raw) > 1024 * 1024 or consumed > 16 * 1024 * 1024:
                info["error"] = "Stream exceeds the transport size limit"
                info["error_code"] = "response_too_large"
                break
            line = raw.decode("utf-8").rstrip("\r\n")
            if not line:
                if consume_event(event):
                    break
                event = []
            elif line.startswith("data:"):
                event.append(line[5:].removeprefix(" "))
    except TimeoutError:
        info.update(error="Stream received no data before the socket timeout", error_code="stream_idle_timeout")
    except (OSError, http.client.HTTPException):
        info.update(error="Stream connection was interrupted", error_code="stream_interrupted")
    except (ValueError, TypeError, KeyError):
        info.update(error="Stream contained a malformed event", error_code="stream_malformed")
    if not info["completed"] and "error" not in info:
        info.update(error="Stream ended without [DONE]", error_code="stream_incomplete")
    choice["message"] = {"content": "".join(texts), "refusal": "".join(refusals) or None}
    return answer
