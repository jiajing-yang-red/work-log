import re

from quality_input.common import InputError, read_json
from quality_llm import validate_profile
from .schemes import SCHEMES, prompt_kind, validate_prompt_overrides
from . import block_needs_met as block
from .recovery import validate_policy


def load_config(path):
    return validate_config(read_json(path))


def validate_config(raw):
    if not isinstance(raw, dict) or set(raw) - {
        "models", "query_model", "query_understanding", "experiments", "runtime", "approved_authority_domains", "prompt_overrides", "block_output_contract", "failure_recovery",
    }:
        raise InputError("Unknown evaluation configuration fields")
    if not isinstance(raw.get("models"), dict) or not raw["models"]:
        raise InputError("Configuration requires named model profiles")
    models = {}
    for name, profile in raw["models"].items():
        if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
            raise InputError("Model profile IDs must use letters, digits, underscore or hyphen")
        models[name] = validate_profile(profile)
    understanding = raw.get("query_understanding", "frozen")
    if understanding not in ("frozen", "direct"):
        raise InputError("query_understanding must be frozen or direct")
    query_model = raw.get("query_model") if understanding == "frozen" else None
    if query_model is not None and query_model not in models:
        raise InputError("Unknown query_model profile")
    if query_model is not None and models[query_model]["protocol"] == "typesafe":
        raise InputError("Jev cannot generate query specifications; choose a generative model")
    experiments = []
    seen = set()
    for item in raw.get("experiments", []):
        if not isinstance(item, dict) or set(item) - {"id", "scheme", "model", "mode", "repeats"}:
            raise InputError("Unknown experiment fields")
        experiment = {"mode": "api_only", "repeats": 1, **item}
        if not isinstance(experiment.get("id"), str) or not re.fullmatch(r"[A-Za-z0-9_-]+", experiment["id"]):
            raise InputError("Experiment requires a simple unique id")
        if experiment["id"] in seen:
            raise InputError("Duplicate experiment id")
        seen.add(experiment["id"])
        if experiment.get("scheme") not in SCHEMES or experiment.get("model") not in models:
            raise InputError("Unknown evaluation scheme or model profile")
        if experiment["scheme"] == "block_content_needs_met_v1" and (
                understanding != "direct" or experiment["mode"] != "api_only"):
            raise InputError("Block Content Needs Met requires direct query judging and api_only evidence")
        if models[experiment["model"]]["protocol"] == "typesafe" and experiment["scheme"] != "answerability_only_v1":
            raise InputError("Jev only supports the answerability_only_v1 scheme")
        if experiment["mode"] not in ("api_only", "page_content"):
            raise InputError("Unsupported evidence mode")
        if type(experiment["repeats"]) is not int or not 1 <= experiment["repeats"] <= 10:
            raise InputError("repeats must be an integer in 1..10")
        experiments.append(experiment)
    runtime = {"max_workers": 4, "max_input_chars": 180000, **raw.get("runtime", {})}
    if set(runtime) != {"max_workers", "max_input_chars"}:
        raise InputError("Unknown runtime options")
    for key, upper in (("max_workers", 32), ("max_input_chars", 2000000)):
        if type(runtime[key]) is not int or not 1 <= runtime[key] <= upper:
            raise InputError(f"Invalid {key}")
    domains = raw.get("approved_authority_domains", [])
    if not isinstance(domains, list) or any(not isinstance(d, str) or
                                           not re.fullmatch(r"[a-z0-9.-]+", d) for d in domains):
        raise InputError("Authority domains must be explicit lowercase domain names")
    overrides = validate_prompt_overrides(raw.get("prompt_overrides", {}))
    if "block_output_contract" in raw:
        if raw["block_output_contract"] is None:
            raise InputError("Block output contract must be explicit")
        block.resolve_contract(raw["block_output_contract"])
    active_kinds = {prompt_kind(kind, models[e["model"]]["protocol"])
                    for e in experiments for kind in SCHEMES[e["scheme"]].parts}
    if "answerability_jev" in overrides and "answerability_jev" not in active_kinds:
        raise InputError("Jev 专用 Prompt 只能用于原生 Jev 可回答性评分。")
    if ("answerability_jev" in active_kinds and "answerability" not in active_kinds
            and "answerability" in overrides):
        raise InputError("Jev 请使用 answerability_jev 专用 Prompt，不能复用生成式 answerability 规则。")
    return {"models": models, "query_model": query_model, "query_understanding": understanding,
            "experiments": experiments,
            "runtime": runtime, "approved_authority_domains": sorted(set(domains)),
            **({"prompt_overrides": overrides}
               if "prompt_overrides" in raw else {}),
            **({"block_output_contract": raw["block_output_contract"]}
               if "block_output_contract" in raw else {}),
            **({"failure_recovery": validate_policy(raw["failure_recovery"])}
               if "failure_recovery" in raw else {})}
