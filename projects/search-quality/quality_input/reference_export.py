"""Import SearchBench's Top10 human/Jev exports without exposing labels as evidence."""

from .common import InputError, digest, identifier, integer, issue


ADAPTER = "answerability_top10_reference_jsonl_v1"
SIGNATURE = {"sample_id", "query", "reference_grade", "reference_source"}


def is_reference_export(rows):
    return bool(rows and isinstance(rows[0][1], dict) and SIGNATURE <= set(rows[0][1]))


def valid_grade(value):
    return type(value) in (int, float) and value in (-1, 0, 1, 2, 3)


def reference_export_adapter(rows, issues):
    queries, responses, labels = {}, {}, []
    run_id = "answerability-top10-export"
    for line, row in rows:
        ref = {"path": "raw/source.jsonl", "record": line}
        if not isinstance(row, dict) or not SIGNATURE <= set(row):
            raise InputError(f"Top10 reference export has an incompatible record at line {line}")
        qid = identifier(row["sample_id"])
        query = row["query"]
        rank = integer(row.get("rank"), 1)
        url = row.get("url")
        if not isinstance(query, str) or not query.strip() or not isinstance(url, str) or not url.strip():
            raise InputError(f"Top10 reference export requires query and url strings, line {line}")
        if rank > 10:
            raise InputError(f"Top10 reference export rank must be 1..10, line {line}")
        if qid in queries and queries[qid]["query_text"] != query:
            raise InputError(f"Top10 reference export has conflicting query text for {qid}")
        queries.setdefault(qid, {"query_id": qid, "query_text": query, "source_ref": ref})
        response = responses.setdefault(qid, {
            "query_id": qid, "search_run_id": run_id, "status": "ok",
            "source_ref": ref, "results": [],
        })
        if any(r["rank"] == rank for r in response["results"]):
            raise InputError(f"Top10 reference export has duplicate rank {rank} for {qid}")
        response["results"].append({"rank": rank, "raw_ref": ref, **{
            key: row.get(key) for key in ("url", "title", "snippet", "text", "published_at")}})

        grade, source = row["reference_grade"], row["reference_source"]
        if not valid_grade(grade) or source not in ("human", "jev"):
            raise InputError(f"Invalid reference_grade or reference_source, line {line}")
        human_valid = row.get("human_review_status") == "graded" and valid_grade(row.get("human_grade"))
        if source == "human":
            if not human_valid or grade != row["human_grade"]:
                raise InputError(f"Human reference does not match a graded human label, line {line}")
            version = row.get("human_source") or "not_recorded"
            reason = row.get("human_reason") or ""
            rubric = "not_recorded"
        else:
            jev = row.get("jev_v10")
            if (human_valid or row.get("label_status") != "numeric" or not isinstance(jev, dict)
                    or not valid_grade(jev.get("grade")) or jev["grade"] != grade
                    or jev.get("sample_id") != qid or jev.get("rank") != rank):
                raise InputError(f"Jev reference does not match its numeric label, or overrides a human label, line {line}")
            version = row.get("jev_source") or "jev_v10_snapshot"
            reason = ""
            rubric = jev.get("prompt_hash") or "not_recorded"
        response_id = "response_" + digest([run_id, qid])
        labels.append({
            "query_id": qid, "document_key": "url_exact_v1:" + digest(url),
            "result_id": "result_" + digest([response_id, rank]),
            "grade": int(grade), "source": source, "version": version,
            "label_schema_version": "answerability_human_v1" if source == "human" else "answerability_jev_v1",
            "evidence_mode": "api_only", "reference_scope": "pooled",
            "reason": reason, "rubric_version": rubric, "source_ref": ref,
        })
    for qid, response in responses.items():
        if sorted(r["rank"] for r in response["results"]) != list(range(1, 11)):
            raise InputError(f"Top10 reference export requires ranks 1..10 for {qid}")
    issue(issues, "REFERENCE_EXPORT_IMPORTED",
          "已识别 Top10 可回答性参考集：sample_id 映射为 query_id，query 映射为 query_text；"
          "人工/Jev 标签独立保存，Golden Answer 仅保留在原始文件中。", "info")
    issue(issues, "REFERENCE_EXPORT_PROVENANCE_UNKNOWN",
          "该参考集未记录搜索服务和采集时间，保留为未知；网页发布时间、标注时间不作为采集时间。", "warning")
    return {"_adapter": ADAPTER, "queries": list(queries.values()),
            "search_runs": [{"search_run_id": run_id, "provider": "not_recorded", "requested_top_k": 10,
                             "query_scope": list(queries)}],
            "responses": list(responses.values()), "page_snapshots": [], "reference_labels": labels}
