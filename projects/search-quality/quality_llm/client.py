import http.client
import ipaddress
import math
import os
import threading
import time
import urllib.error
import urllib.request
from functools import partial
from urllib.parse import urlsplit

from quality_input.common import InputError, canonical, parse_json
from .protocols import DEFAULT_URLS, PROTOCOLS, build_request, extract_response, response_diagnostics, response_failure_code
from .streaming import read_chat_stream
from .retry import RetryWaitExceeded, backoff_seconds, retry_after_seconds


class LLMError(Exception):
    def __init__(self, code, message, attempts=0, diagnostics=None):
        super().__init__(message)
        self.code, self.attempts = code, attempts
        self.diagnostics = diagnostics


class ResponseDecodeError(ValueError):
    """A received HTTP body could not be decoded into a provider response."""


def redact_secret(value, secret):
    if isinstance(value, str):
        return value.replace(secret, "[REDACTED]") if secret else value
    if isinstance(value, dict):
        return {key: redact_secret(val, secret) for key, val in value.items()}
    if isinstance(value, list):
        return [redact_secret(val, secret) for val in value]
    return value


def validate_profile(profile):
    if not isinstance(profile, dict):
        raise InputError("Model profile must be an object")
    allowed = {"protocol", "model", "base_url", "api_key_env", "max_output_tokens",
               "max_concurrency", "requests_per_second", "timeout_seconds", "max_attempts",
               "parameters", "output_mode", "token_limit_field", "stream", "catalog_models", "stream_timeout_seconds",
               "stream_include_usage", "models_url", "retry_base_seconds", "retry_max_seconds",
               "retry_wait_budget_seconds"}
    if set(profile) - allowed:
        raise InputError(f"Unknown model profile fields: {sorted(set(profile) - allowed)}")
    p = {"max_output_tokens": 3000, "max_concurrency": 2,
         "requests_per_second": 1.0, "timeout_seconds": 60, "max_attempts": 2,
         "retry_base_seconds": 4.0, "retry_max_seconds": 60.0, "retry_wait_budget_seconds": 120.0,
         "parameters": {}, "output_mode": "prompt",
         "token_limit_field": "max_completion_tokens", **profile}
    if p.get("protocol") not in PROTOCOLS:
        raise InputError("Unknown LLM protocol")
    if not isinstance(p.get("model"), str) or not p["model"].strip():
        raise InputError("Model profile requires an explicit model identifier")
    for field, upper in (("max_output_tokens", 100000), ("max_concurrency", 64),
                         ("max_attempts", 5)):
        if type(p[field]) is not int or not 1 <= p[field] <= upper:
            raise InputError(f"Invalid {field}")
    for field, upper in (("requests_per_second", 1000), ("timeout_seconds", 120),
                         ("retry_base_seconds", 60), ("retry_max_seconds", 300),
                         ("retry_wait_budget_seconds", 1800)):
        if type(p[field]) not in (int, float) or not math.isfinite(p[field]) or not 0 < p[field] <= upper:
            raise InputError(f"Invalid {field}")
    if p["retry_base_seconds"] > p["retry_max_seconds"]:
        raise InputError("retry_base_seconds must not exceed retry_max_seconds")
    if p["output_mode"] not in ("prompt", "json_schema", "json_object"):
        raise InputError("Unsupported output_mode")
    if p["protocol"] in ("anthropic", "gemini", "typesafe") and p["output_mode"] != "prompt":
        raise InputError("This native adapter uses prompt JSON plus local validation")
    if p["token_limit_field"] not in ("max_completion_tokens", "max_tokens"):
        raise InputError("Unsupported chat token-limit field")
    if "stream" in p and (type(p["stream"]) is not bool or p["protocol"] != "openai_chat"):
        raise InputError("stream must be a boolean for the openai_chat protocol")
    if "stream_include_usage" in p and (type(p["stream_include_usage"]) is not bool or not p.get("stream")):
        raise InputError("stream_include_usage must be a boolean and requires streaming")
    if "stream_timeout_seconds" in p:
        limit = p["stream_timeout_seconds"]
        if not p.get("stream") or type(limit) not in (int, float) or not 1 <= limit <= 1800:
            raise InputError("stream_timeout_seconds requires streaming and must be in 1..1800")
    if "catalog_models" in p:
        catalog = p["catalog_models"]
        if (not isinstance(catalog, list) or not catalog or len(catalog) > 500 or
                any(not isinstance(m, str) or not m.strip() or len(m) > 512 for m in catalog) or
                len(set(catalog)) != len(catalog) or p["model"] not in catalog):
            raise InputError("catalog_models must contain unique model IDs including the selected model")
    # Explicit allowlist prevents overriding model, messages, tools, endpoint or secrets.
    parameter_keys = {
        "mock": set(),
        "openai_responses": {"temperature", "top_p", "reasoning"},
        "openai_chat": {"temperature", "top_p", "seed", "reasoning_effort", "thinking"},
        "anthropic": {"temperature", "top_p", "top_k"},
        "gemini": {"temperature", "topP", "topK", "seed", "thinkingConfig"},
        "typesafe": set(),
    }
    if not isinstance(p["parameters"], dict) or set(p["parameters"]) - parameter_keys[p["protocol"]]:
        raise InputError("Unsupported model parameters; request control fields cannot be overridden")
    if "thinking" in p["parameters"]:
        thinking = p["parameters"]["thinking"]
        if (not isinstance(thinking, dict) or set(thinking) != {"type"} or
                thinking["type"] not in ("enabled", "disabled")):
            raise InputError("thinking must specify type enabled or disabled")
    p["base_url"] = p.get("base_url", DEFAULT_URLS.get(p["protocol"]))
    if p["protocol"] != "mock":
        parsed = urlsplit(p["base_url"] or "")
        try:
            local = parsed.hostname == "localhost" or ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError:
            local = False
        if parsed.scheme != "https" and not (parsed.scheme == "http" and local):
            raise InputError("Use HTTPS, or HTTP for a loopback model endpoint")
        if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise InputError("base_url must not contain credentials, query parameters or fragments")
        env = p.get("api_key_env")
        if env is not None and (not isinstance(env, str) or not env.isidentifier()):
            raise InputError("api_key_env must be an environment variable name")
        if not env and not (local and p["protocol"] == "openai_chat"):
            raise InputError("Remote profiles require api_key_env; never put keys in config")
        if "models_url" in p:
            if not isinstance(p["models_url"], str) or not p["models_url"].strip():
                raise InputError("models_url must be a URL")
            catalog = urlsplit(p["models_url"])
            if (catalog.scheme, catalog.netloc) != (parsed.scheme, parsed.netloc) or catalog.query or catalog.fragment:
                raise InputError("Model catalog must use the same origin as the inference endpoint")
    return p


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def http_transport(url, headers, body, timeout, stream_timeout=None):
    request = urllib.request.Request(url, data=canonical(body).encode("utf-8"), headers=headers)
    opener = urllib.request.build_opener(NoRedirect)
    with opener.open(request, timeout=timeout) as response:
        if body.get("stream") and "text/event-stream" in response.headers.get("Content-Type", "").lower():
            return read_chat_stream(response, stream_timeout if stream_timeout is not None else timeout)
        raw = response.read(16 * 1024 * 1024 + 1)
        if len(raw) > 16 * 1024 * 1024:
            raise ResponseDecodeError("Provider response exceeds the 16 MiB transport limit")
        try:
            return parse_json(raw.decode("utf-8"))
        except (ValueError, UnicodeError):
            # Unstructured wire bodies may contain private reasoning or request echoes.
            raise ResponseDecodeError("HTTP response body is not valid UTF-8 JSON") from None


class LLMClient:
    def __init__(self, profile, transport=None):
        self.profile = validate_profile(profile)
        self.transport = transport or partial(http_transport, stream_timeout=self.profile.get("stream_timeout_seconds"))
        self.slots = threading.BoundedSemaphore(self.profile["max_concurrency"])
        self.rate_lock = threading.Lock()
        self.next_request = 0.0
        self.cooldown_until = 0.0

    def _wait_for_turn(self, not_before, budget):
        """Recheck shared cooldown after waking; reserve only actual dispatches."""
        started = time.monotonic()
        while True:
            with self.rate_lock:
                now = time.monotonic()
                waited = now - started
                delay = max(self.next_request, self.cooldown_until, not_before) - now
                if waited > budget or waited + max(0, delay) > budget:
                    raise RetryWaitExceeded(waited, max(0, delay))
                if delay <= 0:
                    self.next_request = now + 1 / self.profile["requests_per_second"]
                    return waited
            # Do not hold the lock while waiting: another response may extend cooldown.
            time.sleep(delay)

    def _cool_down(self, until):
        with self.rate_lock:
            self.cooldown_until = max(self.cooldown_until, until)

    def check_credentials(self):
        name = self.profile.get("api_key_env")
        if self.profile["protocol"] != "mock" and self.profile["model"].startswith("REPLACE_"):
            raise LLMError("configuration_placeholder", "Replace the template model identifier before running")
        if self.profile["protocol"] != "mock" and name and not os.environ.get(name):
            raise LLMError("missing_credentials", f"Environment variable {name} is not set")

    def complete(self, system, payload, schema, mock_response=None, *, max_output_tokens=None):
        self.check_credentials()
        # A recovery call can increase its own budget without changing the shared
        # client's semaphore, rate limiter, cooldown or model configuration.
        profile = self.profile
        if max_output_tokens is not None:
            profile = validate_profile({**profile, "max_output_tokens": max_output_tokens})
        if self.profile["protocol"] == "mock":
            if mock_response is None:
                raise LLMError("mock_missing", "No simulated response supplied")
            return {"text": canonical(mock_response), "usage": {}, "returned_model": self.profile["model"],
                    "response_id": None, "attempts": 0, "latency_seconds": 0.0, "simulated": True}
        api_key = os.environ.get(self.profile.get("api_key_env") or "", "")
        url, headers, body = build_request(profile, system, payload, schema, api_key)
        started = time.monotonic()
        diagnostics = {"protocol": self.profile["protocol"], "requested_model": self.profile["model"],
                       "max_output_tokens": profile["max_output_tokens"],
                       "timeout_seconds": self.profile["timeout_seconds"],
                       "stream_timeout_seconds": self.profile.get("stream_timeout_seconds", self.profile["timeout_seconds"])
                       if self.profile.get("stream") else None, "attempts": []}
        diagnostics["retry_policy"] = {
            "strategy": "exponential_equal_jitter_v1", "respect_retry_after": True,
            "shared_cooldown_statuses": [429, 503],
            **{key: self.profile[key] for key in (
                "max_attempts", "retry_base_seconds", "retry_max_seconds", "retry_wait_budget_seconds")},
        }
        diagnostics["wait_seconds"] = 0.0
        if self.profile["protocol"] == "typesafe":
            diagnostics["max_output_tokens"] = None  # Not a supported System One request parameter.
            diagnostics["wire_request"] = {"endpoint": url, "protocol": "typesafe", "body": body}
        with self.slots:
            not_before, error = 0.0, None
            for attempt in range(1, self.profile["max_attempts"] + 1):
                try:
                    waited = self._wait_for_turn(not_before, self.profile["retry_wait_budget_seconds"] - diagnostics["wait_seconds"])
                except RetryWaitExceeded as exc:
                    diagnostics["wait_seconds"] += exc.waited
                    diagnostics.update(retry_stop_reason="wait_budget_exhausted", required_wait_seconds=exc.required)
                    if error is None:
                        error = LLMError("retry_wait_exhausted", "Model cooldown exceeds the request waiting budget", 0)
                    else:
                        diagnostics["attempts"][-1].update(will_retry=False, retry_stop_reason="wait_budget_exhausted")
                    error.diagnostics = diagnostics
                    raise error
                diagnostics["wait_seconds"] += waited
                attempt_started = time.monotonic()
                record = {"attempt": attempt, "http_status": None, "response": None, "wait_before_seconds": waited}
                retry_after, received_at = None, None
                try:
                    data = self.transport(url, headers, body, self.profile["timeout_seconds"])
                    record.update(http_status=200, response=redact_secret(
                        response_diagnostics(self.profile["protocol"], data), api_key))
                    answer = extract_response(self.profile["protocol"], data)
                    record.update(outcome="completed", latency_seconds=time.monotonic() - attempt_started)
                    diagnostics["attempts"].append(record)
                    return {**redact_secret(answer, api_key), "attempts": attempt, "diagnostics": diagnostics,
                            "latency_seconds": time.monotonic() - started, "simulated": False}
                except urllib.error.HTTPError as exc:
                    received_at = time.monotonic()
                    retryable = exc.code in (408, 429, 500, 502, 503, 504)
                    retry_after = retry_after_seconds(exc.headers.get("Retry-After"), time.time()) if exc.headers else None
                    error = LLMError("http_error", f"HTTP {exc.code}", attempt)
                    record["http_status"] = exc.code
                    try:
                        raw = exc.read(65537)
                        if len(raw) > 65536:
                            raise ValueError("Error body exceeds limit")
                        record["response"] = redact_secret(response_diagnostics(
                            self.profile["protocol"], parse_json(raw.decode("utf-8"))), api_key)
                    except (ValueError, UnicodeError, OSError, http.client.HTTPException):
                        record["body_note"] = "HTTP error body is not readable JSON or exceeds 64 KiB; raw body not retained"
                    finally:
                        exc.close()
                except (urllib.error.URLError, TimeoutError, ConnectionError, OSError, http.client.HTTPException) as exc:
                    retryable = True
                    record["transport_exception"] = type(exc).__name__
                    if isinstance(exc, http.client.IncompleteRead):
                        # Retain counts only: a partial wire body may contain private reasoning.
                        record["partial_response_bytes"] = len(exc.partial)
                    error = LLMError("transport_error", "Network request failed or timed out", attempt)
                except (ValueError, KeyError, IndexError, TypeError, AttributeError) as exc:
                    # Output recovery is bounded and audited by the evaluation layer.
                    retryable = False
                    if isinstance(exc, ResponseDecodeError):
                        record.update(http_status=200, body_note=str(exc))
                    code = response_failure_code(record.get("response"))
                    message = ((record.get("response") or {}).get("stream_info") or {}).get("error")
                    error = LLMError(code, message or "Provider response refused, incomplete or malformed", attempt)
                record.update(outcome=error.code, error_message=str(error),
                              latency_seconds=time.monotonic() - attempt_started,
                              retryable=retryable, will_retry=False)
                diagnostics["attempts"].append(record)
                error.diagnostics = diagnostics
                if retry_after is not None:
                    record["retry_after_seconds"] = retry_after
                if retryable:
                    backoff = backoff_seconds(attempt, self.profile["retry_base_seconds"], self.profile["retry_max_seconds"])
                    # Keep a provider's longer delay even if it exceeds our wait budget:
                    # stop this task instead of truncating Retry-After and sending early.
                    not_before = max(time.monotonic() + backoff, (received_at or 0) + (retry_after or 0))
                    record.update(backoff_seconds=backoff, retry_delay_seconds=max(0, not_before - time.monotonic()))
                    if record["http_status"] in (429, 503):
                        self._cool_down(not_before)
                        record["shared_cooldown_seconds"] = record["retry_delay_seconds"]
                if not retryable or attempt == self.profile["max_attempts"]:
                    record["retry_stop_reason"] = "non_retryable" if not retryable else "max_attempts"
                    raise error
                record["will_retry"] = True
