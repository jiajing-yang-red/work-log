"""Pure metric functions. Ranks are one-based; input order is authoritative."""

import math


def evaluate_query(ranking, judgments, ks=(5, 10, 20), threshold=2, gain="exponential"):
    """Compute binary metrics and graded NDCG against all supplied judgments.

    Missing results occupy nonrelevant positions. Unjudged documents have grade 0.
    AP@K uses the total number of judged relevant documents, not min(R, K).
    Zero binary relevance gives P/R/F1/AP=0; zero ideal gain gives NDCG=0.
    NDCG uses grades independently of the binary relevance threshold.
    """
    if len(set(ranking)) != len(ranking):
        raise ValueError("同一查询的搜索结果不能包含重复 doc_id")
    if gain not in ("linear", "exponential"):
        raise ValueError("gain 必须是 linear 或 exponential")
    if not math.isfinite(threshold) or threshold <= 0:
        raise ValueError("相关性阈值必须是有限正数")
    if not ks or any(isinstance(k, bool) or not isinstance(k, int) or k <= 0 for k in ks):
        raise ValueError("K 必须是正整数")
    if any(not math.isfinite(g) or g < 0 or g > 30 for g in judgments.values()):
        raise ValueError("相关性等级必须是 0 到 30 之间的有限数值")
    relevant = {doc for doc, grade in judgments.items() if grade >= threshold}
    total = len(relevant)
    hits = 0
    ap_sum = 0.0
    cumulative = []
    cumulative_ap = []
    for rank, doc in enumerate(ranking, 1):
        if doc in relevant:
            hits += 1
            ap_sum += hits / rank
        cumulative.append(hits)
        cumulative_ap.append(ap_sum)

    def graded_gain(grade):
        return grade if gain == "linear" else math.expm1(grade * math.log(2))

    ideal = sorted(judgments.values(), reverse=True)
    metrics = {"AP": ap_sum / total if total else 0.0}
    details = {}
    for k in sorted(set(ks)):
        n = min(k, len(ranking))
        found = cumulative[n - 1] if n else 0
        precision = found / k
        recall = found / total if total else 0.0
        dcg = sum(graded_gain(judgments.get(doc, 0)) / math.log2(i + 2)
                  for i, doc in enumerate(ranking[:k]))
        idcg = sum(graded_gain(grade) / math.log2(i + 2)
                   for i, grade in enumerate(ideal[:k]))
        metrics.update({
            f"Precision@{k}": precision,
            f"Recall@{k}": recall,
            f"F1@{k}": 2 * precision * recall / (precision + recall) if precision + recall else 0.0,
            f"AP@{k}": (cumulative_ap[n - 1] / total) if n and total else 0.0,
            f"NDCG@{k}": dcg / idcg if idcg else 0.0,
        })
        details[str(k)] = {
            "returned": n,
            "relevant_hits": found,
            "judged": sum(doc in judgments for doc in ranking[:k]),
            "unjudged": sum(doc not in judgments for doc in ranking[:k]),
            # Missing results are not counted as judged or unjudged.
            "judged_fraction_of_returned": sum(doc in judgments for doc in ranking[:k]) / n if n else None,
        }
    return {"metrics": metrics, "cutoffs": details, "relevant_total": total,
            "judged_total": len(judgments), "returned_total": len(ranking)}
