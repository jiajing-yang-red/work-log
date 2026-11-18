"""Auditable citation repair and isolation; never change model scores or quotes."""

import copy
import re

from quality_input.common import InputError
from .contracts import EvidenceError, resolve_pointer, validate_answer, validate_citations


VALIDATION_VERSION = "evidence-v1"


def evidence_catalog(payload):
    """List existing text paths without repeating the evidence or adding sources."""
    entries = []

    def visit(value, path, owner):
        if isinstance(value, str) and value:
            entries.append({"path": path, **owner})
        elif isinstance(value, dict):
            for key, child in value.items():
                if key not in ("result_id", "rank"):
                    escaped = key.replace("~", "~0").replace("/", "~1")
                    visit(child, path + "/" + escaped, owner)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, f"{path}/{index}", owner)

    for root in ("result", "page", "reference_answer"):
        visit(payload.get(root), "/" + root, {})
    for index, result in enumerate(payload.get("results", [])):
        visit(result, f"/results/{index}", {"result_id": result["result_id"]})
    return entries


def exact_match(payload, path, quote):
    try:
        source = resolve_pointer(payload, path)
        return isinstance(source, str) and quote in source
    except InputError:
        return False


def repair_path(citation, payload, item):
    """Allow only the known URL alias or an unambiguous rank/index mix-up."""
    path, quote = citation["path"], citation["quote"]
    if exact_match(payload, path, quote):
        return None
    if path == "/result/metadata/url" and exact_match(payload, "/result/url", quote):
        return "/result/url", "url_field_alias"
    match = re.fullmatch(r"/results/([1-9][0-9]*)/(url|title|snippet|text)", path)
    if not match:
        return None
    rank, field = int(match[1]), match[2]
    # Never search another field, another query, or a reference answer for a quote.
    matches = [(index, result) for index, result in enumerate(payload.get("results", []))
               if exact_match(payload, f"/results/{index}/{field}", quote)]
    if len(matches) != 1:
        return None
    index, source = matches[0]
    allowed_ids = {item.get("result_id"), item.get("duplicate_of")}
    if source.get("rank") == rank and source.get("result_id") in allowed_ids:
        return f"/results/{index}/{field}", "rank_used_as_array_index"
    return None


def validate_output(kind, answer, payload, authority_domains=()):
    """Validate shared rules first, then isolate errors in individual citations."""
    # Schema, intent, score consistency, duplicate coverage and cluster invariants
    # remain strict. Citation isolation cannot bypass any of these shared rules.
    validate_answer(kind, answer, payload, authority_domains, check_citations=False)
    judgment = copy.deepcopy(answer)
    if kind == "query_spec":
        return {"status": "success", "judgment": judgment,
                "validation_version": VALIDATION_VERSION, "raw_output_status": "success",
                "evidence_repairs": [], "validation_issues": []}
    repairs, issues = [], []
    if kind == "duplicates":
        items = [({"result_id": item["result_id"], "dimension": "duplicate"}, item)
                 for item in judgment["rows"]]
    else:
        items = [({"dimension": name}, item) for name, item in judgment["dimensions"].items()]

    for scope, item in items:
        item_repairs, item_issues = [], []
        for index, citation in enumerate(item["evidence"]):
            fixed = repair_path(citation, payload, item)
            if fixed:
                replacement, rule = fixed
                item_repairs.append({**scope, "citation_index": index, "rule": rule,
                                     "original_path": citation["path"], "corrected_path": replacement,
                                     "quote": citation["quote"]})
                citation["path"] = replacement
            try:
                validate_citations([citation], payload)
            except EvidenceError as exc:
                item_issues.append({**scope, "citation_index": index, "code": exc.code,
                                    "message": str(exc), **exc.citation})
        if item_repairs:
            item["evidence_repairs"] = item_repairs
            repairs.extend(item_repairs)
        if item_issues:
            reject_item(item, item_issues, kind)
            issues.extend(item_issues)

    if kind == "duplicates":
        by_id = {item["result_id"]: item for item in judgment["rows"]}
        for scope, item in items:
            earlier = item.get("duplicate_of")
            if item["status"] != "error" and earlier and by_id[earlier]["status"] == "error":
                dependent = {**scope, "code": "duplicate_dependency_invalid",
                             "message": "The referenced duplicate cluster member failed validation",
                             "depends_on": earlier}
                reject_item(item, [dependent], kind)
                issues.append(dependent)

    valid = sum(item["status"] != "error" for _, item in items)
    return {
        "status": "success" if not issues else "partial_output" if valid else "invalid_output",
        "judgment": judgment, "validation_version": VALIDATION_VERSION,
        "raw_output_status": "invalid_output" if repairs or issues else "success",
        "evidence_repairs": repairs, "validation_issues": issues,
    }


def reject_item(item, issues, kind):
    item.update(status="error", model_reason=item["reason"], reason=issues[0]["message"],
                evidence=[], error_code=issues[0]["code"], validation_issues=issues)
    if kind == "duplicates":
        item["duplicate_of"] = None
    else:
        item["value"] = None
