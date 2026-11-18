"""Human answerability labels stay outside judge inputs and are joined after scoring."""

from collections import Counter, defaultdict

from quality_input.common import InputError


LABEL_SCHEMA = "answerability_human_v1"
GRADES = (-1, 0, 1, 2, 3)


def valid_grade(value):
    return type(value) in (int, float) and value in GRADES


def human_references(dataset):
    sources = {r["result_id"]: (response["query_id"], r)
               for response in dataset["responses"] for r in response["results"]}
    refs = {}
    for label in dataset.get("reference_labels", []):
        if (label.get("label_schema_version") != LABEL_SCHEMA or label.get("source") != "human"
                or label.get("evidence_mode") != "api_only" or not valid_grade(label.get("grade"))):
            continue
        rid = label.get("result_id")
        qid, result = sources.get(rid, (None, {}))
        if qid != label["query_id"] or result.get("document_key") != label["document_key"]:
            raise InputError("人工校准标签未绑定到相同 Query 的原始结果。")
        if rid in refs:
            raise InputError("同一结果有多份人工可回答性标签，请明确版本后再校准。")
        refs[rid] = {"query_id": qid, "grade": int(label["grade"]), "source": "human",
                     "reason": label.get("reason") or "", "version": label["version"],
                     "rubric_version": label.get("rubric_version") or "not_recorded",
                     "evidence_mode": "api_only"}
    return refs


def compare_with_human(rows, references, experiments=(), repeats=1):
    groups = {(eid, repeat): {} for eid in experiments for repeat in range(repeats)}
    for row in rows:
        if row.get("dimension") != "answerability":
            continue
        ref = references.get(row["result_id"])
        if not ref or row.get("query_id") != ref["query_id"] or row.get("mode") != "api_only":
            continue
        groups.setdefault((row["experiment_id"], row["repeat"]), {})[row["result_id"]] = row
    summaries = []
    for (experiment, repeat), found in sorted(groups.items()):
        paired = [r for r in found.values() if r.get("judgment_status") == "rated"
                  and not r.get("simulated") and valid_grade(r.get("value"))]
        matrix = {str(h): {str(m): 0 for m in GRADES} for h in GRADES}
        for row in paired:
            matrix[str(references[row["result_id"]]["grade"])][str(int(row["value"]))] += 1
        exact = sum(matrix[str(g)][str(g)] for g in GRADES)
        summaries.append({
            "experiment_id": experiment, "repeat": repeat, "reference_count": len(references),
            "compared": len(paired), "agreed": exact, "disagreed": len(paired) - exact,
            "uncompared": len(references) - len(paired),
            "agreement": exact / len(paired) if paired else None,
            "exact_over_all": exact / len(references) if references else None,
            "target_agreement": .95,
            "target_met": bool(references) and len(paired) == len(references) and exact / len(references) >= .95,
            "coverage": len(paired) / len(references) if references else None,
            "confusion": matrix,
            "by_human_grade": {str(g): {"compared": sum(matrix[str(g)].values()),
                                         "agreed": matrix[str(g)][str(g)]} for g in GRADES},
        })
    return {"dimension": "answerability", "reference_count": len(references),
            "grade_distribution": dict(Counter(str(r["grade"]) for r in references.values())),
            "experiments": summaries}
