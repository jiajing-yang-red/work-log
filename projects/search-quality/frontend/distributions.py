"""Score shares for each experiment, independent of human reference coverage."""

from collections import Counter

from quality_eval.calibration import GRADES, valid_grade


def block_distributions(rows, job, result_count):
    from quality_eval.block_needs_met import SCHEME, DIMENSION
    groups = {(f"{SCHEME}-{m}", repeat): {} for m in job["models"]
              for repeat in range(job["repeats"]) if SCHEME in job["schemes"]}
    for row in rows:
        if row["dimension"] == DIMENSION:
            groups.setdefault((row["experiment_id"], row["repeat"]), {})[row["result_id"]] = row
    summaries = []
    for (experiment, repeat), found in sorted(groups.items()):
        counts, excluded = {str(i): 0 for i in range(5)}, Counter()
        for row in found.values():
            if row.get("simulated"):
                excluded["simulated"] += 1
            elif row.get("task_status") == "input_too_large":
                excluded["input_too_large"] += 1
            elif row["judgment_status"] == "rated" and type(row["value"]) is int and 0 <= row["value"] <= 4:
                counts[str(row["value"])] += 1
            else:
                excluded[row["judgment_status"]] += 1
        summaries.append({"experiment_id": experiment, "repeat": repeat, "counts": counts,
                          "dimension": DIMENSION, "rated": sum(counts.values()), "expected": result_count,
                          "pending": max(0, result_count - len(found)), "excluded": dict(excluded)})
    return summaries


def answerability_distributions(rows, job, result_count):
    groups = {(f"{scheme}-{model}", repeat): {}
              for scheme in job["schemes"]
              if scheme in ("answerability_only_v1", "legacy_five_v1")
              for model in job["models"] for repeat in range(job["repeats"])}
    for row in rows:
        if row.get("dimension") == "answerability":
            # Retries of one result must not increase its weight in the chart.
            groups.setdefault((row["experiment_id"], row["repeat"]), {})[row["result_id"]] = row
    summaries = []
    for (experiment, repeat), found in sorted(groups.items()):
        counts = {str(grade): 0 for grade in GRADES}
        excluded = Counter()
        for row in found.values():
            status = row.get("judgment_status")
            if row.get("simulated") or status == "simulated":
                excluded["simulated"] += 1
            elif status == "rated" and valid_grade(row.get("value")):
                counts[str(int(row["value"]))] += 1
            else:
                excluded[status if status in ("error", "unjudgeable", "not_applicable")
                         else "invalid"] += 1
        summaries.append({
            "experiment_id": experiment, "repeat": repeat, "counts": counts,
            "rated": sum(counts.values()), "expected": result_count,
            "pending": max(0, result_count - len(found)), "excluded": dict(excluded),
        })
    return summaries
