"""Validate structural facts without deciding search quality."""

from collections import Counter
from datetime import datetime
from urllib.parse import urlsplit

from .common import (
    InputError, digest, empty, identifier, integer, issue, number, timestamp,
)


class Normalizer:
    def __init__(self, issues):
        self.issues = issues

    def convert(self, value, parser, field, location, required=False, severity="error"):
        if empty(value):
            if required:
                issue(self.issues, "MISSING_FIELD", f"Missing {field}",
                      severity, field=field, **location)
            return None
        try:
            return parser(value)
        except (ValueError, TypeError, OverflowError) as exc:
            issue(self.issues, "INVALID_FIELD", f"{field}: {exc}",
                  severity, field=field, **location)
            return None

    def text(self, value, field, location, required=False):
        def parse(text):
            if not isinstance(text, str):
                raise InputError("Expected a string")
            if required and not text.strip():
                raise InputError("Text is blank")
            return text
        # Keep empty evidence strings distinct from missing fields.
        if value == "" and not required:
            return ""
        return self.convert(value, parse, field, location, required)

    def time(self, value, field, location):
        return self.convert(value, timestamp, field, location, severity="warning")

    def interval(self, start, end, loc):
        if start and end:
            begin = datetime.fromisoformat(start.replace("Z", "+00:00"))
            finish = datetime.fromisoformat(end.replace("Z", "+00:00"))
            if begin > finish:
                issue(self.issues, "REVERSED_TIME_WINDOW", "Start time is after end time", **loc)

    def normalize(self, data):
        queries, runs, responses = [], [], []
        query_ids, run_ids, pairs = set(), set(), set()
        for row in data["queries"]:
            loc = {"source_ref": row.get("source_ref")}
            qid = self.convert(row.get("query_id"), identifier, "query_id", loc, True)
            if qid is None:
                continue
            if qid in query_ids:
                issue(self.issues, "DUPLICATE_QUERY_ID", "Query ID appears more than once",
                      query_id=qid, **loc)
                continue
            query_ids.add(qid)
            query = {
                "query_id": qid,
                "query_text": self.text(row.get("query_text"), "query_text", loc, True),
                "raw_query": self.text(row.get("raw_query"), "raw_query", loc),
                **{f: self.text(row.get(f), f, loc) for f in ("language", "locale", "category")},
                "query_issued_at": self.time(row.get("query_issued_at"), "query_issued_at", loc),
                "query_issued_at_raw": row.get("query_issued_at"),
                "request_frequency": self.convert(
                    row.get("request_frequency"), lambda v: integer(v, 1),
                    "request_frequency", loc),
                "source_ref": row.get("source_ref"),
            }
            queries.append(query)
        if not queries:
            issue(self.issues, "NO_QUERIES", "An explicit nonempty query registry is required")

        for row in data["search_runs"]:
            loc = {"source_ref": row.get("source_ref")}
            rid = self.convert(row.get("search_run_id"), identifier, "search_run_id", loc, True)
            if rid is None:
                continue
            if rid in run_ids:
                issue(self.issues, "DUPLICATE_RUN_ID", "SearchRun ID appears more than once",
                      search_run_id=rid, **loc)
                continue
            run_ids.add(rid)
            scope = row.get("query_scope", sorted(query_ids))
            if not isinstance(scope, list):
                issue(self.issues, "INVALID_SCOPE", "query_scope must be an array", **loc)
                scope = []
            normalized_scope = []
            for value in scope:
                qid = self.convert(value, identifier, "query_scope entry", loc, True)
                if qid is not None:
                    normalized_scope.append(qid)
            if len(set(normalized_scope)) != len(normalized_scope):
                issue(self.issues, "DUPLICATE_SCOPE_QUERY", "Duplicate query in run scope", **loc)
            for qid in set(normalized_scope) - query_ids:
                issue(self.issues, "UNKNOWN_SCOPE_QUERY", "Run scope references unknown query",
                      query_id=qid, **loc)
            params = row.get("request_parameters", {})
            if not isinstance(params, dict):
                issue(self.issues, "INVALID_PARAMETERS", "request_parameters must be an object", **loc)
                params = {}
            run = {
                "search_run_id": rid,
                "provider": self.text(row.get("provider"), "provider", loc, True),
                "system_version": self.text(row.get("system_version"), "system_version", loc),
                "collector_version": self.text(row.get("collector_version"), "collector_version", loc),
                "requested_top_k": self.convert(row.get("requested_top_k"), lambda v: integer(v, 1),
                                                "requested_top_k", loc),
                "collection_started_at": self.time(row.get("collection_started_at"),
                                                   "collection_started_at", loc),
                "collection_completed_at": self.time(row.get("collection_completed_at"),
                                                     "collection_completed_at", loc),
                "request_parameters": params, "query_scope": sorted(set(normalized_scope)),
                "source_query_window": row.get("source_query_window"),
                "source_ref": row.get("source_ref"),
            }
            self.interval(run["collection_started_at"], run["collection_completed_at"], loc)
            window = run["source_query_window"]
            if window is not None:
                if not isinstance(window, dict):
                    issue(self.issues, "INVALID_QUERY_WINDOW", "source_query_window must be an object", **loc)
                    run["source_query_window"] = None
                else:
                    run["source_query_window"] = {
                        "start": self.time(window.get("start"), "source_query_window.start", loc),
                        "end": self.time(window.get("end"), "source_query_window.end", loc),
                        "start_raw": window.get("start"), "end_raw": window.get("end"),
                        "source_timezone": self.text(window.get("source_timezone"),
                                                     "source_query_window.source_timezone", loc),
                    }
                    self.interval(run["source_query_window"]["start"],
                                  run["source_query_window"]["end"], loc)
            runs.append(run)
        if not runs:
            issue(self.issues, "NO_SEARCH_RUNS", "At least one SearchRun is required")
        run_map = {run["search_run_id"]: run for run in runs}

        for row in data["responses"]:
            loc = {"source_ref": row.get("source_ref")}
            qid = self.convert(row.get("query_id"), identifier, "query_id", loc, True)
            rid = self.convert(row.get("search_run_id"), identifier, "search_run_id", loc, True)
            if qid is None or rid is None:
                continue
            loc.update(query_id=qid, search_run_id=rid)
            if qid not in query_ids or rid not in run_ids:
                issue(self.issues, "ORPHAN_RESPONSE", "Unknown query or SearchRun", **loc)
            elif qid not in run_map[rid]["query_scope"]:
                issue(self.issues, "RESPONSE_OUTSIDE_SCOPE", "Response is outside declared scope", **loc)
            if (rid, qid) in pairs:
                issue(self.issues, "DUPLICATE_RESPONSE", "Multiple responses for the same run/query", **loc)
                continue
            pairs.add((rid, qid))
            response_id = "response_" + digest([rid, qid])
            raw_results = row.get("results")
            if not isinstance(raw_results, list):
                issue(self.issues, "INVALID_RESULTS", "results must be an array, including when empty", **loc)
                raw_results = []
            results = [self.result(result, response_id, index, loc)
                       for index, result in enumerate(raw_results)]
            ranks = [result["rank"] for result in results]
            valid_ranks = [rank for rank in ranks if rank is not None]
            if len(valid_ranks) != len(set(valid_ranks)):
                issue(self.issues, "DUPLICATE_RANK", "Rank must be unique within a response", **loc)
            if len(valid_ranks) == len(ranks) and sorted(ranks) != list(range(1, len(ranks) + 1)):
                issue(self.issues, "NON_CONTIGUOUS_RANK", "Ranks must start at 1 without gaps", **loc)
            results.sort(key=lambda result: (result["rank"] is None, result["rank"] or 0))
            count = len(results)
            reported = self.convert(row.get("reported_result_count"), integer,
                                    "reported_result_count", loc)
            if reported is not None and count != reported:
                issue(self.issues, "RESULT_COUNT_MISMATCH",
                      f"Source reports {reported}; actual count is {count}", **loc)
            raw_status = row.get("status")
            status = self.status(raw_status, count, loc)
            http_status = self.convert(row.get("http_status"), integer, "http_status", loc)
            if http_status is not None and not 100 <= http_status <= 599:
                issue(self.issues, "INVALID_HTTP_STATUS", "HTTP status must be in 100..599", **loc)
            if status in ("ok", "empty") and http_status is not None and http_status >= 400:
                issue(self.issues, "STATUS_CONFLICT", "Successful status conflicts with HTTP error", **loc)
            top_k = run_map.get(rid, {}).get("requested_top_k")
            if top_k and 0 < count < top_k:
                issue(self.issues, "SHORT_RESPONSE", f"Returned {count} of requested {top_k}",
                      "info", **loc)
            if top_k and count > top_k:
                issue(self.issues, "OVERFULL_RESPONSE", "More results than requested; preserved",
                      "warning", **loc)
            url_counts = Counter(result["url"] for result in results if result["url"])
            for url, occurrences in url_counts.items():
                if occurrences > 1:
                    issue(self.issues, "EXACT_DUPLICATE_URL",
                          f"Exact URL occurs {occurrences} times; all positions preserved",
                          "info", document_key="url_exact_v1:" + digest(url), **loc)
            responses.append({
                "response_id": response_id, "query_id": qid, "search_run_id": rid,
                "status": status, "source_status": raw_status, "http_status": http_status,
                "attempt_count": self.convert(row.get("attempt_count"), integer, "attempt_count", loc),
                "retrieved_at": self.time(row.get("retrieved_at"), "retrieved_at", loc),
                "retrieved_at_raw": row.get("retrieved_at"),
                "reported_result_count": reported, "actual_result_count": count,
                "error": {"kind": row.get("error_kind"), "message": row.get("error_message")}
                         if row.get("error_kind") or row.get("error_message") else None,
                "source_ref": row.get("source_ref"), "results": results,
            })
        # A missing response is not a successful empty result.
        for run in runs:
            rid = run["search_run_id"]
            for qid in run["query_scope"]:
                if qid in query_ids and (rid, qid) not in pairs:
                    issue(self.issues, "MISSING_RESPONSE", "Planned query has no source response",
                          "warning", query_id=qid, search_run_id=rid)
                    responses.append({
                        "response_id": "response_" + digest([rid, qid]),
                        "query_id": qid, "search_run_id": rid, "status": "missing_record",
                        "source_status": None, "http_status": None, "attempt_count": None,
                        "retrieved_at": None, "retrieved_at_raw": None,
                        "reported_result_count": None, "actual_result_count": 0,
                        "error": None, "source_ref": None, "results": [],
                    })
        scoped = {qid for run in runs for qid in run["query_scope"]}
        for qid in query_ids - scoped:
            issue(self.issues, "QUERY_NOT_SCHEDULED", "Query is not scheduled in any SearchRun",
                  "warning", query_id=qid)
        pages = self.pages(data.get("page_snapshots", []))
        page_ids = {page["page_snapshot_id"] for page in pages}
        page_map = {page["page_snapshot_id"]: page for page in pages}
        all_results = {result["result_id"]: result for response in responses
                       for result in response["results"]}
        for result in all_results.values():
            if result.get("page_snapshot_id") and result["page_snapshot_id"] not in page_ids:
                issue(self.issues, "UNKNOWN_PAGE_SNAPSHOT", "Result references an unknown page snapshot",
                      result_id=result["result_id"])
            elif result.get("page_snapshot_id"):
                page = page_map[result["page_snapshot_id"]]
                if not result["url"] or result["url"] not in (page["requested_url"], page["final_url"]):
                    issue(self.issues, "PAGE_URL_MISMATCH",
                          "Page snapshot URL does not match the linked result",
                          result_id=result["result_id"])
        result_queries = {result["result_id"]: response["query_id"] for response in responses
                          for result in response["results"]}
        labels = self.labels(data.get("reference_labels", []), query_ids, all_results,
                             page_map, result_queries)
        return {
            "queries": sorted(queries, key=lambda q: q["query_id"]),
            "search_runs": sorted(runs, key=lambda r: r["search_run_id"]),
            "responses": sorted(responses, key=lambda r: (r["search_run_id"], r["query_id"])),
            "page_snapshots": pages, "reference_labels": labels,
        }

    def status(self, value, count, loc):
        if value == "ok":
            return "ok" if count else "empty"
        if value == "empty":
            if count:
                issue(self.issues, "STATUS_CONFLICT", "Empty response contains results", **loc)
            return "empty"
        if value in ("error", "not_collected", "missing_record", "pending", "skipped"):
            if count:
                issue(self.issues, "RESULTS_WITH_UNSUCCESSFUL_STATUS",
                      "Results retained despite unsuccessful collection status", "warning", **loc)
            return "not_collected" if value in ("pending", "skipped") else value
        issue(self.issues, "UNKNOWN_EXECUTION_STATUS",
              "Execution status is absent or unsupported; not inferred from result count",
              "warning", **loc)
        return "unknown"

    def result(self, row, response_id, index, response_loc):
        if not isinstance(row, dict):
            issue(self.issues, "INVALID_RESULT", "Result must be an object", index=index, **response_loc)
            row = {}
        loc = {**response_loc, "source_ref": row.get("raw_ref", response_loc.get("source_ref")),
               "result_index": index}
        rank = self.convert(row.get("rank"), lambda v: integer(v, 1), "rank", loc, True)
        result_id = "result_" + digest([response_id, rank if rank is not None else ["invalid", index]])
        # No synthetic content or truncation is performed.
        result = {field: self.text(row.get(field), field, loc)
                  for field in ("url", "title", "snippet", "text")}
        for field in ("url", "title"):
            if not result[field] or not result[field].strip():
                issue(self.issues, "MISSING_" + field.upper(), f"{field} is missing or blank",
                      "warning", **loc)
        if not any(result.get(f) and result[f].strip() for f in ("snippet", "text")):
            issue(self.issues, "MISSING_API_TEXT", "No snippet or text; other evidence is retained",
                  "warning", **loc)
        if result["url"]:
            try:
                url = urlsplit(result["url"])
                valid_url = url.scheme in ("http", "https") and bool(url.hostname)
            except ValueError:
                valid_url = False
            if not valid_url:
                issue(self.issues, "UNSUPPORTED_URL", "URL is not a valid HTTP(S) URL; preserved",
                      "warning", **loc)
        pub_raw = row.get("published_at_raw", row.get("published_at"))
        metadata = row.get("source_metadata", {})
        if not isinstance(metadata, dict):
            issue(self.issues, "INVALID_METADATA", "source_metadata must be an object", **loc)
            metadata = {}
        result.update({
            "result_id": result_id, "rank": rank,
            "document_key": "url_exact_v1:" + digest(result["url"]) if result["url"] else None,
            "published_at": self.time(pub_raw, "published_at", loc), "published_at_raw": pub_raw,
            "source_score": self.convert(row.get("source_score"), number, "source_score", loc),
            "source_metadata": metadata, "raw_ref": row.get("raw_ref"),
            "page_snapshot_id": self.convert(row.get("page_snapshot_id"), identifier,
                                             "page_snapshot_id", loc),
        })
        # Identity includes evidence, not its provider-specific rank or score.
        result["evidence_hash"] = digest({key: result[key] for key in
                                         ("url", "title", "snippet", "text", "published_at_raw")} |
                                        {"metadata": {key: value for key, value in metadata.items()
                                                      if key != "id"}})
        return result

    def pages(self, rows):
        pages, seen = [], set()
        for row in rows:
            loc = {"source_ref": row.get("source_ref")}
            pid = self.convert(row.get("page_snapshot_id"), identifier, "page_snapshot_id", loc, True)
            if pid is None:
                continue
            if pid in seen:
                issue(self.issues, "DUPLICATE_PAGE_ID", "Duplicate page snapshot ID", **loc)
            seen.add(pid)
            page = {key: row.get(key) for key in (
                "requested_url", "final_url", "fetch_status", "http_status",
                "content_type", "extractor_version", "extraction_status", "truncated",
                "raw_content_ref", "source_ref",
            )}
            for field in ("requested_url", "final_url", "content_type", "extractor_version",
                          "extraction_status", "raw_content_ref"):
                page[field] = self.text(row.get(field), field, loc, required=field == "requested_url")
            page["http_status"] = self.convert(row.get("http_status"), integer, "page.http_status", loc)
            if page["http_status"] is not None and not 100 <= page["http_status"] <= 599:
                issue(self.issues, "INVALID_HTTP_STATUS", "Page HTTP status must be in 100..599", **loc)
            if page["fetch_status"] not in (None, "ok", "error", "not_fetched", "unknown"):
                issue(self.issues, "INVALID_FETCH_STATUS", "Unsupported page fetch status", **loc)
            page["page_snapshot_id"] = pid
            page["body_text"] = self.text(row.get("body_text"), "body_text", loc)
            page["fetched_at"] = self.time(row.get("fetched_at"), "fetched_at", loc)
            page["fetched_at_raw"] = row.get("fetched_at")
            page["content_hash"] = digest(page["body_text"]) if page["body_text"] is not None else None
            if row.get("content_hash") and row["content_hash"] != page["content_hash"]:
                issue(self.issues, "PAGE_HASH_MISMATCH", "Declared body hash does not match body_text", **loc)
            if page["truncated"] is not None and not isinstance(page["truncated"], bool):
                issue(self.issues, "INVALID_TRUNCATION_FLAG", "truncated must be boolean or null", **loc)
            if not page["body_text"]:
                issue(self.issues, "NO_PAGE_BODY", "Snapshot does not contain body_text", "warning", **loc)
            pages.append(page)
        return pages

    def labels(self, rows, query_ids, results, pages, result_queries):
        labels, seen = [], set()
        for row in rows:
            loc = {"source_ref": row.get("source_ref")}
            qid = self.convert(row.get("query_id"), identifier, "label.query_id", loc, True)
            doc = self.text(row.get("document_key"), "label.document_key", loc, True)
            if qid not in query_ids:
                issue(self.issues, "ORPHAN_LABEL", "Label references an unknown query", **loc)
            label = {key: self.text(row.get(key), f"label.{key}", loc) for key in (
                "evidence_mode", "result_id", "page_snapshot_id", "query_spec_version",
                "reference_scope", "reason", "rubric_version",
            )}
            label["source_ref"] = row.get("source_ref")
            label.update(query_id=qid, document_key=doc,
                         grade=self.convert(row.get("grade"), number, "label.grade", loc))
            for field in ("label_schema_version", "source", "version"):
                label[field] = self.text(row.get(field), f"label.{field}", loc, True)
            if label["reference_scope"] not in ("pooled", "corpus_complete", "unknown"):
                issue(self.issues, "INVALID_REFERENCE_SCOPE",
                      "reference_scope must be pooled, corpus_complete or unknown", **loc)
            mode = label["evidence_mode"]
            if mode == "api_only":
                result = results.get(label["result_id"])
                if result is None or result["document_key"] != doc or result_queries.get(label["result_id"]) != qid:
                    issue(self.issues, "LABEL_EVIDENCE_MISMATCH",
                          "API label must identify its exact result/document", **loc)
            elif mode == "page_content":
                page = pages.get(label["page_snapshot_id"])
                if page is None:
                    issue(self.issues, "LABEL_EVIDENCE_MISMATCH",
                          "Page label must reference an imported snapshot", **loc)
                elif doc not in {"url_exact_v1:" + digest(url) for url in
                                 (page["requested_url"], page["final_url"]) if url}:
                    issue(self.issues, "LABEL_EVIDENCE_MISMATCH",
                          "Page label document_key does not match snapshot URL", **loc)
            elif mode != "document":
                issue(self.issues, "INVALID_LABEL_MODE",
                      "evidence_mode must be api_only, page_content or document", **loc)
            # A document label may concern an unretrieved document: necessary for recall pools.
            key = (qid, doc, mode, label["result_id"], label["page_snapshot_id"],
                   label["query_spec_version"], label["label_schema_version"],
                   label["source"], label["version"])
            if key in seen:
                issue(self.issues, "DUPLICATE_LABEL", "Duplicate reference label binding", **loc)
            seen.add(key)
            labels.append(label)
        return labels
