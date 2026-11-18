"""Validated row-based CSV, JSON and JSONL input."""

import csv
import json
import math
from pathlib import Path


def read_rows(path, required=()):
    path = Path(path)
    with path.open(encoding="utf-8-sig", newline="") as source:
        suffix = path.suffix.lower()
        if suffix == ".csv":
            reader = csv.DictReader(source)
            if not reader.fieldnames or len(set(reader.fieldnames)) != len(reader.fieldnames):
                raise ValueError(f"{path}: 缺少表头或表头重复")
            if not set(required).issubset(reader.fieldnames):
                raise ValueError(f"{path}: 缺少必需字段 {', '.join(required)}")
            rows = list(reader)
        elif suffix == ".json":
            rows = json.load(source)
        elif suffix in (".jsonl", ".ndjson"):
            rows = [json.loads(line) for line in source if line.strip()]
        else:
            raise ValueError("输入仅支持 .csv、.json（对象数组）、.jsonl、.ndjson")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError(f"{path}: 数据必须是记录对象数组")
    return rows


def identifier(row, key):
    value = row.get(key)
    if not isinstance(value, (str, int)) or isinstance(value, bool) or not str(value).strip():
        raise ValueError(f"字段 {key} 必须是非空字符串或整数")
    return str(value).strip()


def load_qrels(path):
    qrels = {}
    metadata = {}
    for number, row in enumerate(read_rows(path, ("query_id", "doc_id")), 1):
        try:
            query = identifier(row, "query_id")
            doc = identifier(row, "doc_id")
            raw_grade = row.get("grade", row.get("relevance"))
            if isinstance(raw_grade, bool):
                raise ValueError("grade 不能是布尔值")
            grade = float(raw_grade)
            if not math.isfinite(grade) or not 0 <= grade <= 30:
                raise ValueError("grade 必须是 0 到 30 之间的有限数值")
            if doc in qrels.setdefault(query, {}):
                raise ValueError(f"重复标注 ({query}, {doc})")
            qrels[query][doc] = grade
            meta = metadata.setdefault(query, {"query": "", "category": "未分类"})
            for field in ("query", "category"):
                value = row.get(field)
                if value is not None and str(value).strip():
                    value = str(value).strip()
                    if meta[field] not in ("", "未分类", value):
                        raise ValueError(f"同一 query_id 的 {field} 不一致")
                    meta[field] = value
        except (ValueError, TypeError) as exc:
            raise ValueError(f"{path} 记录 {number}: {exc}") from exc
    if not qrels:
        raise ValueError("标注文件不能为空；测试集的查询范围由标注文件决定")
    return qrels, metadata


def load_results(path, allowed_queries):
    grouped = {}
    seen_docs = set()
    for number, row in enumerate(read_rows(path, ("query_id", "doc_id", "rank")), 1):
        try:
            query = identifier(row, "query_id")
            doc = identifier(row, "doc_id")
            raw_rank = row.get("rank")
            rank = int(str(raw_rank))
            if rank <= 0:
                raise ValueError("rank 必须是从 1 开始的正整数")
            if query not in allowed_queries:
                raise ValueError(f"查询 {query} 没有标注，请补充标注或移除该结果")
            if (query, doc) in seen_docs:
                raise ValueError(f"查询 {query} 中 doc_id {doc} 重复")
            seen_docs.add((query, doc))
            grouped.setdefault(query, []).append((rank, doc))
        except (ValueError, TypeError) as exc:
            raise ValueError(f"{path} 记录 {number}: {exc}") from exc
    result = {}
    for query, entries in grouped.items():
        entries.sort()
        if [rank for rank, _ in entries] != list(range(1, len(entries) + 1)):
            raise ValueError(f"查询 {query}: rank 必须唯一且从 1 连续递增")
        result[query] = [doc for _, doc in entries]
    return result
