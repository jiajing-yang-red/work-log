"""Frozen four-field skill adapter. No landing pages, feature scores or rank evidence."""
import copy
import hashlib
import json
from pathlib import Path

from quality_input.common import InputError, canonical
from .block_prompt import VERSION as PROMPT_VERSION, assemble

ROOT = Path(__file__).parent / "skills" / "needs-met-rater"
VERSION = "2.1.1"
SCHEME = "block_content_needs_met_v1"
KIND = "block_needs_met"
DIMENSION = "block_content_needs_met"
GRADES = {str(i): name for i, name in enumerate(("FailsM", "SM", "MM", "HM", "FullyM"))}
LEGACY_CONTRACT = "full-v1"
COMPACT_CONTRACT = "compact-v1"
FILES = ("SKILL.md", "references/rubric.md", "references/context-rules.md",
         "references/io-and-examples.md", "references/output.schema.json")


def bundle_manifest():
    return {"skill_version": VERSION, "files": {
        name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in FILES}}


def resolve_contract(contract=None, *, has_override=False):
    # Saved rules predate the compact contract unless their config opts in.
    value = contract if contract is not None else LEGACY_CONTRACT if has_override else COMPACT_CONTRACT
    if value not in (LEGACY_CONTRACT, COMPACT_CONTRACT):
        raise InputError("Unknown Block Needs Met output contract")
    return value


def output_schema(contract=LEGACY_CONTRACT):
    if contract == COMPACT_CONTRACT:
        return {"type": "object", "additionalProperties": False,
                "required": ["score", "reason", "evidence"],
                "properties": {
                    "score": {"enum": [0, 1, 2, 3, 4, None]},
                    "reason": {"type": "string", "minLength": 1},
                    "evidence": {"type": "array", "maxItems": 3,
                                 "items": {"type": "string", "minLength": 1}}}}
    resolve_contract(contract)
    schema = json.loads((ROOT / FILES[-1]).read_text())
    # The transport sends one item per call and always supplies an association ID.
    results = schema["properties"]["results"]
    results.update(minItems=1, maxItems=1)
    results["items"]["required"] = [*results["items"]["required"], "id"]
    return schema


def rules():
    return assemble(ROOT)["text"]


def _structure(value, schema, path="$"):
    """Validate structural keywords used by the frozen schema; conditions below."""
    if "const" in schema and (type(value) is not type(schema["const"]) or value != schema["const"]):
        raise InputError(path + ": const mismatch")
    if "enum" in schema and not any(type(value) is type(v) and value == v for v in schema["enum"]):
        raise InputError(path + ": invalid enum")
    types = schema.get("type", [])
    types = [types] if isinstance(types, str) else types
    mapping = {"object": dict, "array": list, "string": str, "integer": int, "boolean": bool, "null": type(None)}
    if types and type(value) not in [mapping[t] for t in types]:
        raise InputError(path + ": wrong type")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        if not set(schema.get("required", [])) <= set(value):
            raise InputError(path + ": missing fields")
        if schema.get("additionalProperties") is False and set(value) - set(props):
            raise InputError(path + ": unexpected fields")
        for key in value.keys() & props.keys():
            _structure(value[key], props[key], path + "/" + key)
    if isinstance(value, list):
        if not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", float("inf")):
            raise InputError(path + ": wrong array size")
        for i, item in enumerate(value):
            _structure(item, schema.get("items", {}), f"{path}/{i}")
    if isinstance(value, str) and (len(value) < schema.get("minLength", 0) or
                                  (schema.get("minLength", 0) and not value.strip())):
        raise InputError(path + ": empty text")
    if type(value) is int and not schema.get("minimum", float("-inf")) <= value <= schema.get("maximum", float("inf")):
        raise InputError(path + ": out of range")


def _validate_compact(answer, payload):
    _structure(answer, output_schema(COMPACT_CONTRACT))
    rated = answer["score"] is not None
    if rated and (not answer["evidence"] or not payload["query"].strip() or not payload["url"].strip()):
        raise InputError("Rated result requires query, URL and at least one evidence quote")
    citations = []
    for quote in answer["evidence"]:
        fields = [k for k in ("title", "snippet", "url")
                  if isinstance(payload.get(k), str) and quote in payload[k]]
        if not fields:
            raise InputError("Evidence must be an exact substring of an input title, snippet or URL")
        # If a quote occurs in several fields, use the first matching field.
        # This is a deterministic citation location, not a model judgment.
        citations.append({"field": fields[0], "text": quote})
    item = {**{k: payload[k] for k in ("id", "input_index", "query", "url")},
            "status": "rated" if rated else "insufficient_evidence",
            "score": answer["score"], "rating": GRADES[str(answer["score"])] if rated else None,
            "reason": answer["reason"], "evidence": citations}
    native = {"skill_version": VERSION, "rating_mode": DIMENSION, "evidence_scope": "provided_text_fields",
              "score_scale": GRADES, "results": [item]}
    dimension = {"status": "rated" if rated else "unjudgeable", "value": item["score"],
                 "reason": item["reason"],
                 "evidence": [{"path": "/"+x["field"], "quote": x["text"]} for x in citations]}
    return {"status": "success", "judgment": {"dimensions": {DIMENSION: dimension}},
            "skill_output": native, "validation_version": "block-needs-met-compact-v1"}


def validate(answer, payload, contract=LEGACY_CONTRACT):
    resolve_contract(contract)
    if contract == COMPACT_CONTRACT:
        return _validate_compact(answer, payload)
    _structure(answer, output_schema())
    item = answer["results"][0]
    for field in ("input_index", "id", "query", "url"):
        if type(item[field]) is not type(payload[field]) or item[field] != payload[field]:
            raise InputError("Result association mismatch: " + field)
    if item["status"] == "rated":
        if (type(item["score"]) is not int or GRADES.get(str(item["score"])) != item["rating"] or
                item["confidence"] is None or not item["evidence"] or
                not item["query"].strip() or not item["url"].strip()):
            raise InputError("Rated result requires a matching grade, confidence, query, URL and evidence")
    elif any(item[k] is not None for k in ("score", "rating", "confidence")) or item["needs_review"] is not True:
        raise InputError("Insufficient evidence must have null grades and needs_review=true")
    if item["confidence"] == "low" and not item["needs_review"]:
        raise InputError("Low confidence requires review")
    for citation in item["evidence"]:
        source = payload.get(citation["field"])
        if not isinstance(source, str) or citation["text"] not in source:
            raise InputError("Evidence must be an exact substring of the cited input field")
    dimension = {"status": "rated" if item["status"] == "rated" else "unjudgeable",
                 "value": item["score"], "reason": item["reason"],
                 "evidence": [{"path": "/" + e["field"], "quote": e["text"]} for e in item["evidence"]]}
    return {"status": "success", "judgment": {"dimensions": {DIMENSION: dimension}},
            "skill_output": copy.deepcopy(answer), "validation_version": "block-needs-met-v1"}


def simulation():
    return {"dimensions": {DIMENSION: {
        "status": "simulated", "value": None,
        "reason": "SIMULATION ONLY: no semantic quality judgment was performed.", "evidence": []}}}


def native_records(outcomes):
    for outcome in sorted(outcomes, key=lambda r: (r.get("input_index", 0), r["experiment_id"], r["repeat"])):
        if outcome["kind"] != KIND:
            continue
        record = {key: outcome.get(key) for key in (
            "input_index", "source_ref", "task_id", "query_id", "result_ids", "experiment_id", "repeat",
            "model_id", "status", "reason", "error_code", "simulated", "skill_output", "prompt_hash",
            "input_hash", "skill_bundle", "block_output_contract")}
        if outcome.get("auto_recovery"):
            record.update(auto_recovery=outcome["auto_recovery"],
                          effective_prompt_hash=outcome.get("effective_prompt_hash", outcome["prompt_hash"]))
        yield record
