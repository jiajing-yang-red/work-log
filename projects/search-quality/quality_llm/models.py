"""Read provider model catalogs without generating completions."""

import time
import urllib.error
import urllib.request
from urllib.parse import urlencode, urlsplit

from quality_input.common import parse_json
from .client import LLMError, NoRedirect, validate_profile


def models_endpoint(profile):
    if profile.get("models_url"):
        return profile["models_url"]
    base = profile["base_url"].rstrip("/")
    # Both root and versioned OpenAI-compatible base URLs are supported.
    if profile["protocol"] in ("openai_chat", "openai_responses", "anthropic") and not urlsplit(base).path:
        base += "/v1"
    return base + "/models"


def get_json(url, headers, timeout):
    request = urllib.request.Request(url, headers=headers, method="GET")
    opener = urllib.request.build_opener(NoRedirect)
    with opener.open(request, timeout=timeout) as response:
        body = response.read(4 * 1024 * 1024 + 1)
        if len(body) > 4 * 1024 * 1024:
            raise ValueError("Model catalog exceeds size limit")
        return parse_json(body.decode("utf-8"))


def list_models(profile, api_key="", transport=None):
    p = validate_profile(profile)
    protocol = p["protocol"]
    if protocol == "mock":
        return {"endpoint": None, "source": "simulation", "models": [
            {"id": p["model"], "name": p["model"], "selectable": True, "reason": ""}
        ]}
    if p.get("api_key_env") and not api_key:
        raise LLMError("missing_credentials", f"请先填写 {p['api_key_env']}。")
    if p.get("catalog_models"):
        # An explicit local catalog is not evidence of account access or quota.
        # Used by providers such as Qianwen that do not expose GET /models.
        return {"endpoint": None, "source": "configured", "models": [
            {"id": model, "name": model, "selectable": True, "reason": ""}
            for model in p["catalog_models"]
        ]}
    endpoint = models_endpoint(p)
    if "REPLACE_" in endpoint:
        raise LLMError("configuration_placeholder", "请先配置模型接口地址。")
    headers = {"Accept": "application/json"}
    if protocol == "anthropic":
        headers.update({"x-api-key": api_key, "anthropic-version": "2023-06-01"})
    elif protocol == "gemini":
        headers["x-goog-api-key"] = api_key
    elif api_key:
        headers["Authorization"] = "Bearer " + api_key
    fetch = transport or get_json
    found, seen_cursors, cursor = {}, set(), None
    deadline = time.monotonic() + 30
    try:
        for _ in range(100):
            params = {}
            if protocol == "anthropic":
                params = {"limit": 1000}
                if cursor:
                    params["after_id"] = cursor
            elif protocol == "gemini":
                params = {"pageSize": 1000}
                if cursor:
                    params["pageToken"] = cursor
            elif cursor:
                params["after"] = cursor
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError()
            data = fetch(endpoint + ("?" + urlencode(params) if params else ""), headers,
                         min(15, remaining))
            if not isinstance(data, dict):
                raise ValueError()
            items = data.get("models", []) if protocol == "gemini" else data.get("data")
            if not isinstance(items, list):
                raise ValueError()
            for item in items:
                if not isinstance(item, dict):
                    raise ValueError()
                model_id = item.get("name") if protocol == "gemini" else item.get("id")
                if not isinstance(model_id, str) or not model_id.strip() or len(model_id) > 512:
                    raise ValueError()
                if protocol == "typesafe" and "typesafe:systemone" not in item.get("supported_protocols", []):
                    continue
                if protocol == "gemini" and model_id.startswith("models/"):
                    model_id = model_id[len("models/"):]
                name = item.get("displayName") or item.get("display_name") or item.get("name") or model_id
                name = name if isinstance(name, str) else model_id
                reason = ""
                if protocol == "gemini" and "generateContent" not in item.get("supportedGenerationMethods", []):
                    reason = "该模型未声明支持文本生成"
                found[model_id] = {"id": model_id, "name": name[:256],
                                   "selectable": not reason, "reason": reason}
            if protocol == "gemini":
                cursor = data.get("nextPageToken")
            elif data.get("has_more"):
                cursor = data.get("last_id") or (items[-1].get("id") if items else None)
                if not cursor:
                    raise ValueError()
            else:
                cursor = None
            if not cursor:
                return {"endpoint": endpoint, "source": "api", "models": list(found.values())}
            if not isinstance(cursor, str) or len(cursor) > 4096 or cursor in seen_cursors:
                raise ValueError()
            seen_cursors.add(cursor)
        raise ValueError()
    except urllib.error.HTTPError as exc:
        status = exc.code
        exc.close()
        raise LLMError("models_http_error", f"获取模型列表失败（HTTP {status}）。") from None
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
        raise LLMError("models_transport_error", "获取模型列表失败：网络连接异常或超时。") from None
    except (ValueError, TypeError, KeyError, IndexError):
        raise LLMError("models_invalid_response", "模型列表响应不完整或格式不符合接口约定。") from None
