from dataclasses import dataclass
from pathlib import Path

from quality_input.common import InputError, canonical, digest
from quality_llm.systemone import parse_choice_prompt
from .contracts import schema_for
from .evidence import evidence_catalog
from . import block_needs_met as block


PROMPTS = Path(__file__).parent / "prompts"


@dataclass(frozen=True)
class Scheme:
    id: str
    version: str
    parts: tuple
    source: str


SCHEMES = {
    block.SCHEME: Scheme(block.SCHEME, block.VERSION, (block.KIND,), "needs-met-rater 2.1.1 frozen runtime"),
    "answerability_only_v1": Scheme(
        "answerability_only_v1", "1.1", ("answerability",),
        "Human-calibrated answerability rubric 2026-10-22; generic rules, labels excluded from judge input"),
    "legacy_five_v1": Scheme(
        "legacy_five_v1", "1.1", ("legacy_url", "duplicates"),
        "evaluation/context-search-quality-evaluation/SKILL.md; reconstructed candidate, not final historical GPT prompt"),
    "google_overview_v1": Scheme(
        "google_overview_v1", "1.0", ("google_nm", "google_pq"),
        "https://services.google.com/fh/files/misc/hsw-sqrg.pdf; project adaptation of Overview pp.19-31"),
}
DIMENSIONS = {"answerability": ("answerability",),
              block.KIND: (block.DIMENSION,),
              "legacy_url": ("answerability", "authoritative", "freshness", "noise"),
              "duplicates": ("duplicate",), "google_nm": ("needs_met",),
              "google_pq": ("page_quality",)}


MAX_PROMPT_RULES_CHARS = 50000
PROMPT_KINDS = {"query_spec", "answerability_jev", *DIMENSIONS}


def prompt_kind(kind, protocol):
    return "answerability_jev" if kind == "answerability" and protocol == "typesafe" else kind


def validate_prompt_overrides(value):
    if not isinstance(value, dict) or set(value) - PROMPT_KINDS:
        raise InputError("Prompt 覆盖配置包含不支持的任务类型。")
    for kind, rules in value.items():
        if not isinstance(rules, str) or not rules.strip() or len(rules) > MAX_PROMPT_RULES_CHARS:
            raise InputError(f"Prompt 规则不能为空，且不能超过 {MAX_PROMPT_RULES_CHARS} 个字符。")
        if "Output JSON schema:" in rules:
            raise InputError("请只编辑评分指令；Output JSON schema 由系统自动附加。")
        if kind == "answerability_jev":
            try:
                parse_choice_prompt(rules)
            except ValueError as exc:
                raise InputError(str(exc)) from None
    return dict(value)


def prompt_for(kind, query_understanding="frozen", overrides=None, *, block_output_contract=None):
    if kind == block.KIND:
        base = validate_prompt_overrides(overrides)[kind] if overrides and kind in overrides else block.rules()
        contract = block.resolve_contract(block_output_contract, has_override=bool(overrides and kind in overrides))
        return base + "\nOutput JSON schema:\n" + canonical(block.output_schema(contract))
    if kind == "answerability_jev":
        rules = (validate_prompt_overrides(overrides)[kind] if overrides and kind in overrides
                 else (PROMPTS / "answerability_jev.json").read_text(encoding="utf-8"))
        return canonical(parse_choice_prompt(rules))
    if overrides and kind in overrides:
        base = validate_prompt_overrides(overrides)[kind]
        return base + "\nOutput JSON schema:\n" + canonical(schema_for(kind, query_understanding))
    specific = (PROMPTS / f"{kind}.txt").read_text(encoding="utf-8")
    if kind in ("query_spec", "duplicates", "answerability"):
        base = specific
    else:
        common = "legacy_common.txt" if kind == "legacy_url" else "common.txt"
        base = (PROMPTS / common).read_text(encoding="utf-8") + "\n" + specific
    if kind in ("legacy_url", "google_nm"):
        prefix = "legacy_query" if kind == "legacy_url" else "query"
        base += "\n" + (PROMPTS / f"{prefix}_{query_understanding}.txt").read_text(encoding="utf-8")
    return base + "\nOutput JSON schema:\n" + canonical(schema_for(kind, query_understanding))


def unknown_measurement(reason):
    return {"status": "unjudgeable", "value": None, "reason": reason, "evidence": []}


def duplicate_fallback(payload, simulated=False):
    rows, seen = [], {}
    for index, result in enumerate(payload["results"]):
        url = result.get("url")
        previous = seen.get(url) if url else None
        definite = index == 0 or previous is not None
        rows.append({
            "result_id": result["result_id"], "status": "rated" if definite else "simulated" if simulated else "error",
            "duplicate_of": previous,
            "reason": ("SIMULATION: " if simulated else "") +
                      ("First occurrence or exact URL rule." if definite else "Semantic comparison unavailable."),
            "evidence": [{"path": f"/results/{index}/url", "quote": url[:200]}] if previous else [],
        })
        if url:
            seen.setdefault(url, result["result_id"])
    return {"rows": rows}


def simulated_response(kind, payload):
    if kind == block.KIND:
        return block.simulation()
    reason = "SIMULATION ONLY: no semantic quality judgment was performed."
    if kind == "query_spec":
        return {"intents": [{
            "id": "I1", "likelihood": "primary", "goal": payload["query"]["query_text"],
            "kind": "other", "requirements": [{"id": "R1", "text": "Resolve the supplied query."}],
            "hard_constraints": [], "fully_meets_eligible": False, "specific_target": None,
        }], "freshness_applicable": False, "freshness_type": "F5",
            "needs_clarification": False, "clarification_reason": "SIMULATED interpretation; not for real judging."}
    if kind == "duplicates":
        return duplicate_fallback(payload, simulated=True)
    dimensions = {name: unknown_measurement(reason) for name in DIMENSIONS[kind]}
    if kind == "answerability":
        dimensions["answerability"]["status"] = "simulated"
        return {"dimensions": dimensions}
    if kind == "legacy_url":
        # Simulation is an execution marker, never a fabricated quality label.
        for item in dimensions.values():
            item["status"] = "simulated"
    if kind == "google_pq":
        return {"page_purpose": "", "dimensions": dimensions}
    result = {"selected_intent_id": None, "requirements": [], "constraints": [],
              "temporal_fit": "unknown", "reliability": "unknown", "dimensions": dimensions}
    direct = payload.get("query_understanding") == "direct"
    if direct:
        result.pop("selected_intent_id")
        result.update(query_assessment={
            "interpretation": "SIMULATION ONLY: " + payload["query"]["query_text"],
            "kind": "other", "likelihood": "primary", "specific_target": None,
            "fully_meets_eligible": False, "needs_clarification": False, "freshness_applicable": False,
        }, requirements=[{"text": "SIMULATION: Resolve the supplied query.", "status": "unknown"}])
    if kind == "legacy_url":
        result["authority_tier"] = None
        if direct:
            result["query_assessment"] = {
                "interpretation": "SIMULATION ONLY: " + payload["query"]["query_text"],
                "freshness_applicable": False, "freshness_type": "F5",
            }
        if direct or not payload["query_spec"]["freshness_applicable"]:
            dimensions["freshness"]["status"] = "not_applicable"
    return result


def visible_api(result):
    return {
        **{key: result.get(key) for key in
           ("url", "title", "snippet", "text", "published_at", "published_at_raw")},
        "metadata": {key: result.get("source_metadata", {})[key] for key in
                     ("display_url", "images") if key in result.get("source_metadata", {})},
    }


def page_evidence(result, pages):
    page = pages.get(result.get("page_snapshot_id"))
    if not page:
        return None, "page_snapshot_missing"
    if (page.get("fetch_status") != "ok" or not page.get("body_text")
            or page.get("truncated") is not False or page.get("extraction_status") != "complete"):
        return page, "page_snapshot_incomplete_or_unverified"
    return page, None


def legacy_page_evidence(result, pages):
    page, missing = page_evidence(result, pages)
    # A verified empty/unavailable page is evidence of unusability, not a missing input.
    if page and (page.get("http_status") in (401, 403, 404, 410) or (
            page.get("fetch_status") == "ok" and page.get("extraction_status") == "complete"
            and page.get("truncated") is False and page.get("body_text") == "")):
        return page, None
    return page, missing


def make_tasks(dataset, config, specs, query_ids):
    understanding = config.get("query_understanding", "frozen")
    query_map = {q["query_id"]: q for q in dataset["queries"]}
    pages = {p["page_snapshot_id"]: p for p in dataset["page_snapshots"]}
    systems = {}
    for experiment in config["experiments"]:
        for kind in SCHEMES[experiment["scheme"]].parts:
            key = prompt_kind(kind, config["models"][experiment["model"]].get("protocol"))
            systems[key] = prompt_for(key, understanding, config.get("prompt_overrides"),
                                      block_output_contract=config.get("block_output_contract"))
    tasks = []
    indices = {r["result_id"]: i for i, r in enumerate(
        r for response in dataset["responses"] for r in response["results"])}
    skill_bundle = block.bundle_manifest()
    for experiment in config["experiments"]:
        scheme = SCHEMES[experiment["scheme"]]
        if scheme.id == block.SCHEME and (understanding != "direct" or experiment["mode"] != "api_only"):
            raise InputError("Block Content Needs Met requires direct query judging and api_only evidence")
        for response in dataset["responses"]:
            qid = response["query_id"]
            if qid not in query_ids:
                continue
            query = query_map[qid]
            wrapper = specs.get(qid) if understanding == "frozen" else None
            spec = wrapper.get("spec") if wrapper and wrapper.get("status") == "ready" else None
            base = {
                "query": {key: query.get(key) for key in
                          ("query_text", "language", "locale", "query_issued_at")},
                "query_spec": spec,
                "evaluation_as_of": dataset["manifest"]["evaluation_as_of"],
                "reference_answer": wrapper.get("reference_answer") if wrapper else None,
                "mode": experiment["mode"],
            }
            if understanding == "direct":
                base.pop("query_spec")
                base["query_understanding"] = "direct"
            for repeat in range(experiment["repeats"]):
                for kind in scheme.parts:
                    groups = [response["results"]] if kind == "duplicates" else [[r] for r in response["results"]]
                    for group in groups:
                        if not group:
                            continue
                        blocked = None
                        payload = dict(base)
                        if response["status"] != "ok":
                            blocked = "collection_not_successful"
                        if kind not in ("google_pq", "duplicates") and understanding == "frozen":
                            if spec is None:
                                blocked = blocked or "query_spec_missing_or_failed"
                            elif kind not in ("legacy_url", "answerability") and spec["needs_clarification"]:
                                blocked = blocked or "query_requires_clarification"
                            elif kind not in ("legacy_url", "answerability") and spec["freshness_applicable"] and not base["evaluation_as_of"]:
                                blocked = blocked or "evaluation_time_missing"
                        if kind == "duplicates":
                            payload = {"query": base["query"], "mode": experiment["mode"], "results": []}
                            for result in group:
                                if experiment["mode"] == "page_content":
                                    page, missing = legacy_page_evidence(result, pages)
                                    blocked = blocked or missing
                                    visible = {"url": result.get("url"),
                                               "text": page.get("body_text") if page else None}
                                else:
                                    visible = visible_api(result)
                                payload["results"].append({"result_id": result["result_id"],
                                                           "rank": result["rank"], **visible})
                        else:
                            result = group[0]
                            if kind == "google_pq" or experiment["mode"] == "page_content":
                                page_reader = legacy_page_evidence if kind == "legacy_url" else page_evidence
                                page, missing = page_reader(result, pages)
                                blocked = blocked or missing
                                payload["page"] = page
                                payload["result"] = {key: result.get(key) for key in ("url", "title")}
                            else:
                                payload["result"] = visible_api(result)
                            if kind == "google_pq":
                                # PQ has no query relevance input and never uses snippets as a page.
                                payload = {
                                    "page": payload["page"],
                                    "context": {key: base["query"].get(key) for key in ("locale", "language")} |
                                               {"evaluation_as_of": base["evaluation_as_of"]},
                                }
                            if kind == "legacy_url":
                                payload["approved_authority_domains"] = config["approved_authority_domains"]
                                if payload.get("reference_answer"):
                                    payload["evaluation_as_of"] = payload["reference_answer"]["as_of"]
                        # Source refs and extraction tool names are provenance, not judge evidence.
                        if isinstance(payload.get("page"), dict):
                            http_status = payload["page"].get("http_status")
                            payload["page"] = {key: payload["page"].get(key) for key in
                                               ("requested_url", "final_url", "body_text", "fetched_at",
                                                "content_type", "fetch_status", "extraction_status", "truncated")}
                            if kind == "legacy_url":
                                payload["page"]["http_status"] = http_status
                        key = prompt_kind(kind, config["models"][experiment["model"]].get("protocol"))
                        if kind == block.KIND:
                            payload = {"id": result["result_id"], "input_index": indices[result["result_id"]],
                                       "query": query["query_text"],
                                       **{k: result.get(k) for k in ("title", "snippet", "url")}}
                            if payload["url"] is None:
                                payload["url"] = ""
                        elif key != "answerability_jev":
                            payload["evidence_catalog"] = evidence_catalog(payload)
                        system = systems[key]
                        metadata = {
                            "experiment_id": experiment["id"], "scheme": scheme.id,
                            "scheme_version": scheme.version, "model_profile": experiment["model"],
                            "model_id": config["models"][experiment["model"]]["model"],
                            "mode": "page_content" if kind == "google_pq" else experiment["mode"],
                            "repeat": repeat, "kind": kind,
                            **({"prompt_kind": key} if key != kind else {}),
                            "query_id": qid, "search_run_id": response["search_run_id"],
                            "response_id": response["response_id"],
                            "result_ids": [r["result_id"] for r in group],
                            "dataset_version": dataset["manifest"]["dataset_version"],
                            "query_understanding": understanding,
                            "query_spec_hash": wrapper.get("spec_hash") if wrapper and kind != "google_pq" else None,
                            "prompt_hash": digest(system), "input_hash": digest(payload),
                            "rating_target": ("page_snapshot" if kind == "google_pq" or
                                              experiment["mode"] == "page_content" else "api_evidence"),
                        }
                        if kind == block.KIND:
                            metadata.update(input_index=payload["input_index"], source_ref=result.get("raw_ref"),
                                            skill_bundle=skill_bundle, rating_target="provided_text_fields")
                            contract = block.resolve_contract(config.get("block_output_contract"),
                                has_override=block.KIND in config.get("prompt_overrides", {}))
                            if contract == block.COMPACT_CONTRACT:
                                metadata["block_output_contract"] = contract
                        identity = {**metadata, "model": config["models"][experiment["model"]],
                                    "blocked_reason": blocked}
                        if config["models"][experiment["model"]].get("protocol") == "typesafe":
                            identity["native_adapter_version"] = "systemone-choice-v1"
                        tasks.append({**metadata, "task_id": digest(identity), "payload": payload,
                                      "system": system, "blocked_reason": blocked})
    return tasks
