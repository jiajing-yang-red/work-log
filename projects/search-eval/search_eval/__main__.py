"""Run with python3 -m search_eval --help."""

import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sys

from . import __version__
from .io import load_qrels, load_results
from .metrics import evaluate_query
from .report import aggregate, html_report


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def make_report(qrels_path, results_path, ks=(5, 10, 20), threshold=2,
                gain="exponential", run_name="offline-evaluation"):
    qrels, metadata = load_qrels(qrels_path)
    results = load_results(results_path, qrels)
    ks = sorted(set(ks))
    queries = {}
    for query in sorted(qrels):
        ranking = results.get(query, [])
        queries[query] = {
            **metadata[query],
            **evaluate_query(ranking, qrels[query], ks, threshold, gain),
            "ranking": [{"doc_id": doc, "grade": qrels[query].get(doc)} for doc in ranking],
        }
    coverage = {}
    for k in ks:
        key = str(k)
        counts = {field: sum(query["cutoffs"][key][field] for query in queries.values())
                  for field in ("returned", "judged", "unjudged")}
        counts["judged_fraction_of_returned"] = counts["judged"] / counts["returned"] if counts["returned"] else None
        coverage[key] = counts
    groups = {}
    for category in sorted({query["category"] for query in queries.values()}):
        subset = {key: query for key, query in queries.items() if query["category"] == category}
        groups[category] = {"query_count": len(subset), "metrics": aggregate(subset)}
    warnings = []
    missing = sum(query["returned_total"] == 0 for query in queries.values())
    no_relevant = sum(query["relevant_total"] == 0 for query in queries.values())
    if missing:
        warnings.append(f"{missing} 个查询没有搜索结果，仍保留在分母中，其搜索质量指标为 0。无法从离线文件区分空结果与执行失败，请核查结果采集日志。")
    if no_relevant:
        warnings.append(f"{no_relevant} 个查询没有达到阈值的相关标注，Recall/AP/F1 记 0；NDCG 独立使用原始等级。")
    if any(item["unjudged"] for item in coverage.values()):
        warnings.append("存在未标注的搜索结果，暂按 0 分处理；建议补充标注后再作版本优劣判断。")
    return {
        "schema_version": 1, "evaluator_version": __version__, "run_name": run_name,
        "created_at": datetime.now(timezone.utc).isoformat(), "query_count": len(queries),
        "config": {"ks": ks, "threshold": threshold, "gain": gain, "aggregation": "macro",
                   "ap_denominator": "all_judged_relevant", "unjudged": "zero", "undefined": "zero"},
        "inputs": {"qrels_sha256": digest(qrels_path), "results_sha256": digest(results_path)},
        "summary": aggregate(queries), "groups": groups, "coverage": coverage,
        "queries": queries, "warnings": warnings,
    }


def compare(report, baseline):
    if baseline.get("schema_version") != report["schema_version"] or baseline.get("evaluator_version") != report["evaluator_version"]:
        raise ValueError("基线报告版本不匹配，请用当前评测器重跑基线")
    if baseline.get("config") != report["config"]:
        raise ValueError("基线配置不一致：K、阈值、增益与计算口径必须一致")
    if baseline.get("inputs", {}).get("qrels_sha256") != report["inputs"]["qrels_sha256"]:
        raise ValueError("基线标注文件不同：请使用相同测试集和标注版本")
    if set(baseline.get("queries", {})) != set(report["queries"]):
        raise ValueError("基线查询集合不一致")
    expected = set(report["summary"])
    if set(baseline.get("summary", {})) != expected:
        raise ValueError("基线指标不完整")
    for key, number in baseline["summary"].items():
        if not isinstance(number, (int, float)) or not math.isfinite(number):
            raise ValueError(f"基线指标 {key} 无效")
    metric = f'NDCG@{report["config"]["ks"][0]}'
    deltas = {}
    for query_id, query in report["queries"].items():
        old = baseline["queries"][query_id].get("metrics", {})
        if set(old) != set(query["metrics"]) or any(not isinstance(v, (int, float)) or not math.isfinite(v) for v in old.values()):
            raise ValueError(f"基线查询 {query_id} 的指标不完整或无效")
        deltas[query_id] = {key: value - old[key] for key, value in query["metrics"].items()}
    regressions = [{"query_id": query_id, "delta": values[metric]}
                   for query_id, values in deltas.items() if values[metric] < 0]
    regressions.sort(key=lambda row: (row["delta"], row["query_id"]))
    report["comparison"] = {
        "baseline_name": baseline.get("run_name", "baseline"),
        "summary_delta": {key: value - baseline["summary"][key] for key, value in report["summary"].items()},
        "query_deltas": deltas, "regression_metric": metric, "regressions": regressions,
    }


def csv_safe(value):
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def write_report(report, output):
    output = Path(output)
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise ValueError(f"输出路径已存在且非空：{output}。请换一个目录以保留历史记录")
    rendered = html_report(report)
    payload = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)
    output.mkdir(parents=True, exist_ok=True)
    (output / "report.json").write_text(payload + "\n", encoding="utf-8")
    (output / "report.html").write_text(rendered, encoding="utf-8")
    with (output / "per_query.csv").open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.writer(file)
        keys = list(next(iter(report["queries"].values()))["metrics"])
        writer.writerow(["query_id", "query", "category", "relevant_total", "returned_total", *keys])
        for query_id, query in report["queries"].items():
            writer.writerow([csv_safe(query_id), csv_safe(query["query"]), csv_safe(query["category"]),
                             query["relevant_total"], query["returned_total"], *[query["metrics"][key] for key in keys]])
    with (output / "summary.csv").open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(["metric", "value", "delta_vs_baseline"])
        for name, number in report["summary"].items():
            writer.writerow([name, number, report.get("comparison", {}).get("summary_delta", {}).get(name, "")])


def main(argv=None):
    parser = argparse.ArgumentParser(description="搜索离线自动评测：Precision / Recall / F1 / MAP / NDCG")
    parser.add_argument("--qrels", required=True, help="标注文件 CSV / JSON / JSONL")
    parser.add_argument("--results", required=True, help="搜索结果 CSV / JSON / JSONL")
    parser.add_argument("--ks", nargs="+", type=int, default=[5, 10, 20])
    parser.add_argument("--threshold", type=float, default=2, help="二值相关阈值，默认 2；0/1 标注请设为 1")
    parser.add_argument("--gain", choices=["linear", "exponential"], default="exponential")
    parser.add_argument("--name", default="offline-evaluation", help="搜索版本或本次评测名称")
    parser.add_argument("--baseline", help="以前生成的 report.json")
    parser.add_argument("--output", required=True, help="新的输出目录，不覆盖已有非空目录")
    args = parser.parse_args(argv)
    try:
        report = make_report(args.qrels, args.results, args.ks, args.threshold, args.gain, args.name)
        if args.baseline:
            with Path(args.baseline).open(encoding="utf-8") as file:
                compare(report, json.load(file))
        write_report(report, args.output)
    except (ValueError, OSError, TypeError) as exc:
        parser.exit(2, f"评测失败：{exc}\n")
    print(f'完成：{report["query_count"]} 个查询；报告 {Path(args.output).resolve() / "report.html"}')
    for key, value in report["summary"].items():
        print(f"{key:16s} {value:.6f}")
    for warning in report["warnings"]:
        print(f"提示：{warning}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
