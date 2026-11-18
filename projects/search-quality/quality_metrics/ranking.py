"""Rank-aware answerability metrics and paired, pooled-reference comparisons."""

from collections import Counter, defaultdict
from math import log2

from quality_eval.calibration import valid_grade
from quality_input.common import InputError, digest


VERSION = "answerability-ranking-v1"
METRICS = ("ndcg_at_10", "mrr_at_10", "success_at_1", "success_at_3", "success_at_10")
POLICY = {
    "cutoff": 10, "success_grade": 3,
    "gains": {"-1": 0, "0": 0, "1": 1, "2": 3, "3": 7},
    "discount": "1 / log2(rank + 1)", "zero_idcg": 0,
    "missing_judgment": "exclude_entire_query", "empty_results": "zero",
    "duplicate_url": "first_occurrence_only_keep_rank",
    "comparison_labels": "baseline_first_for_identical_evidence",
}


def metrics(scores, ideal_scores=None):
    """Slots retain their rank; zero-pad short lists, never compress missing scores."""
    def gain(s):
        return 2 ** max(0, s) - 1

    def dcg(values):
        return sum(gain(s) / log2(i + 2) for i, s in enumerate(values[:10]))

    scores = list(scores[:10])
    ideal = sorted(scores if ideal_scores is None else ideal_scores, reverse=True)[:10]
    denominator = dcg(ideal)
    first = next((i + 1 for i, s in enumerate(scores) if s == 3), None)
    return {"ndcg_at_10": dcg(scores) / denominator if denominator else 0.0,
            "mrr_at_10": 1.0 / first if first else 0.0,
            **{f"success_at_{k}": float(first is not None and first <= k) for k in (1, 3, 10)}}


def mean_metrics(items):
    return {k: sum(item[k] for item in items) / len(items) if items else None for k in METRICS}


def _profile_signature(config, experiment):
    profile = config.get("models", {}).get(experiment.get("model"), {})
    # A hash describes comparability; no credentials or endpoints reach the browser.
    fields = ("protocol", "base_url", "model", "parameters", "max_output_tokens")
    query_profile = config.get("models", {}).get(config.get("query_model"), {})
    return digest({"scorer": {k: profile.get(k) for k in fields},
                   "parser": {k: query_profile.get(k) for k in fields},
                   "max_input_chars": config.get("runtime", {}).get("max_input_chars"),
                   "query_understanding": config.get("query_understanding"),
                   **({"failure_recovery": config["failure_recovery"]} if "failure_recovery" in config else {})})


def build_rankings(rows, context, job, config):
    relevant = [r for r in rows if r.get("dimension") == "answerability"]
    by_group = defaultdict(dict)
    source_runs = defaultdict(set)
    for row in relevant:
        by_group[(row["experiment_id"], row["repeat"])][row["result_id"]] = row
        if row.get("search_run_id"):
            source_runs[row["result_id"]].add(row["search_run_id"])
    all_runs = {s for values in source_runs.values() for s in values}
    all_runs.update(r["search_run_id"] for r in context["responses"] if r.get("search_run_id"))
    fallback = next(iter(all_runs)) if len(all_runs) == 1 else "unattributed"
    responses = defaultdict(list)
    for response in context["responses"]:
        inferred = {s for r in response["results"] for s in source_runs[r["result_id"]]}
        sid = response.get("search_run_id") or (next(iter(inferred)) if len(inferred) == 1 else fallback)
        responses[sid].append(response)
    texts = {q["query_id"]: q for q in context["queries"]}
    experiments = {e["id"]: e for e in config.get("experiments", [])
                   if e["scheme"] in ("answerability_only_v1", "legacy_five_v1")}
    groups = []
    for eid, experiment in experiments.items():
        for repeat in range(experiment.get("repeats", 1)):
            found = by_group[(eid, repeat)]
            for sid, subset in sorted(responses.items()):
                ids = {r["result_id"] for response in subset for r in response["results"]}
                stamps = {tuple(found[rid].get(k) for k in
                                ("model_id", "prompt_hash", "scheme_version", "mode", "query_understanding"))
                          for rid in ids if rid in found}
                simulated = job.get("simulated") or config.get("models", {}).get(experiment["model"], {}).get("protocol") == "mock"
                group = {"id": digest([eid, repeat, sid])[:20], "experiment_id": eid,
                         "repeat": repeat, "search_run_id": sid, "scheme": experiment["scheme"],
                         "model_id": config.get("models", {}).get(experiment["model"], {}).get("model"),
                         "signature": digest([_profile_signature(config, experiment), list(next(iter(stamps)))]) if len(stamps) == 1 else None,
                         "query_total": len(subset), "queries": [], "simulated": bool(simulated)}
                query_counts = Counter(r["query_id"] for r in subset)
                for response in subset:
                    qid = response["query_id"]
                    item = {"query_id": qid, "query_text": texts[qid]["query_text"],
                            "query_context_hash": digest(texts[qid]),
                            "status": "excluded", "reason": None, "results": []}
                    group["queries"].append(item)
                    if simulated:
                        item["reason"] = "simulated"
                        continue
                    if query_counts[qid] > 1:
                        item["reason"] = "ambiguous_response"
                        continue
                    if response["status"] not in ("ok", "empty"):
                        item["reason"] = "collection_failed"
                        continue
                    source = response["results"]
                    if any(type(r.get("rank")) is not int for r in source):
                        item["reason"] = "invalid_ranks"
                        continue
                    ordered = sorted(source, key=lambda r: r["rank"])[:10]
                    if [r["rank"] for r in ordered] != list(range(1, len(ordered) + 1)):
                        item["reason"] = "invalid_ranks"
                        continue
                    if not ordered:
                        item.update(status="eligible", empty=True, duplicate_slots=0, scores=[], metrics=metrics([]))
                        continue
                    seen = set()
                    failures = set()
                    for result in ordered:
                        row = found.get(result["result_id"])
                        if not row:
                            failures.add("pending_scores")
                            continue
                        if row.get("simulated") or row.get("judgment_status") == "simulated":
                            failures.add("simulated")
                            continue
                        if row.get("query_id") != qid or row.get("judgment_status") != "rated" or not valid_grade(row.get("value")):
                            failures.add("invalid_scores")
                            continue
                        url = result.get("url")
                        if not url:
                            failures.add("missing_url")
                            continue
                        evidence = {k: result.get(k) for k in ("url", "title", "snippet", "text", "published_at")}
                        if experiment.get("mode") == "page_content":
                            evidence["page_text"] = context.get("pages", {}).get(result.get("page_snapshot_id"))
                        item["results"].append({"rank": result["rank"], "url": url,
                                                "grade": int(row["value"]), "duplicate": url in seen,
                                                "evidence_hash": digest(evidence),
                                                "query_spec_hash": row.get("query_spec_hash")})
                        seen.add(url)
                    if failures:
                        item["reason"] = "+".join(sorted(failures))
                        continue
                    scores = [0 if r["duplicate"] else r["grade"] for r in item["results"]]
                    item.update(status="eligible", empty=False,
                                duplicate_slots=sum(r["duplicate"] for r in item["results"]),
                                scores=scores, metrics=metrics(scores))
                valid = [q for q in group["queries"] if q["status"] == "eligible"]
                group.update(eligible_queries=len(valid), empty_queries=sum(q["empty"] for q in valid),
                             zero_gain_queries=sum(not any(s > 0 for s in q["scores"]) for q in valid),
                             duplicate_slots=sum(q["duplicate_slots"] for q in valid),
                             excluded=dict(Counter(q["reason"] for q in group["queries"] if q["status"] != "eligible")),
                             metrics=mean_metrics([q["metrics"] for q in valid]))
                groups.append(group)
    return {"version": VERSION, "policy": POLICY, "basis": "within_returned_top10",
            "job_id": job["id"], "dataset_path": job["dataset_path"], "groups": groups}


def summary(report):
    return {**report, "groups": [{k: v for k, v in group.items() if k not in ("queries", "signature")}
                                  for group in report["groups"]]}


def compare_groups(current, baseline):
    if not current["signature"] or current["signature"] != baseline["signature"] or current["scheme"] != baseline["scheme"]:
        raise InputError("评分模型、参数、Prompt、评测方案或证据模式不同，不能作为同条件排序对比。")
    left = {q["query_id"]: q for q in current["queries"]}
    right = {q["query_id"]: q for q in baseline["queries"]}
    common = sorted(left.keys() & right.keys())
    excluded = Counter()
    pairs = []
    conflicts = 0
    shared_labels = 0
    changed_scores = 0
    for qid in common:
        a, b = left[qid], right[qid]
        if a["query_context_hash"] != b["query_context_hash"]:
            excluded["query_changed"] += 1
            continue
        if a["status"] != "eligible" or b["status"] != "eligible":
            excluded["incomplete_or_invalid"] += 1
            continue
        evidence = defaultdict(set)
        specs = set()
        for r in a["results"] + b["results"]:
            evidence[r["url"]].add(r["evidence_hash"])
            specs.add(r["query_spec_hash"])
        if len(specs) > 1:
            excluded["query_spec_changed"] += 1
            continue
        if any(len(values) > 1 for values in evidence.values()):
            excluded["evidence_changed"] += 1
            continue
        # Fixed baseline wins conflicting labels; all original judgments remain intact.
        pool = {}
        for r in b["results"] + a["results"]:
            pool.setdefault(r["url"], r["grade"])
        first_a = {r["url"]: r["grade"] for r in a["results"] if not r["duplicate"]}
        first_b = {r["url"]: r["grade"] for r in b["results"] if not r["duplicate"]}
        overlap = first_a.keys() & first_b.keys()
        disagreements = sum(first_a[url] != first_b[url] for url in overlap)
        conflicts += disagreements
        shared_labels += len(overlap)
        scored = {}
        for name, item in (("current", a), ("baseline", b)):
            scores = [0 if r["duplicate"] else pool[r["url"]] for r in item["results"]]
            changed_scores += sum(not r["duplicate"] and r["grade"] != pool[r["url"]] for r in item["results"])
            scored[name] = metrics(scores, list(pool.values()))
        pairs.append({"query_id": qid, "query_text": a["query_text"], **scored,
                      "pool_size": len(pool), "label_conflicts": disagreements,
                      "zero_gain": not any(v > 0 for v in pool.values())})
    means = {name: mean_metrics([p[name] for p in pairs]) for name in ("current", "baseline")}
    return {"version": VERSION, "policy": POLICY, "basis": "shared_top10_pool_baseline_labels",
            "common_queries": len(common), "compared_queries": len(pairs),
            "unmatched_current": len(left.keys() - right.keys()),
            "unmatched_baseline": len(right.keys() - left.keys()), "excluded": dict(excluded),
            "shared_labels": shared_labels, "label_conflicts": conflicts,
            "changed_score_slots": changed_scores, "zero_gain_queries": sum(p["zero_gain"] for p in pairs),
            **means, "delta": {k: means["current"][k] - means["baseline"][k] if pairs else None for k in METRICS},
            "queries": pairs}
