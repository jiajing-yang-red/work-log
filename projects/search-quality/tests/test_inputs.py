import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from quality_input.common import InputError, digest, timestamp
from quality_input.pipeline import import_dataset, iter_api_inputs, load_dataset


def fixture():
    return {
        "queries": [{"query_id": key, "query_text": f"Question {key}"}
                    for key in ("q1", "q2", "q3", "q4")],
        "search_runs": [{
            "search_run_id": "run1", "provider": "ateve", "requested_top_k": 10,
            "collection_completed_at": "2026-10-20T13:36:00+08:00",
        }],
        "responses": [
            {"query_id": "q1", "search_run_id": "run1", "status": "ok",
             "reported_result_count": 2,
             "results": [{"rank": rank, "url": "https://example.com/page",
                          "title": "Title", "snippet": "x" * 40000, "source_score": 0.91,
                          "source_metadata": {"id": f"results.#{rank-1}"}}
                         for rank in (1, 2)]},
            {"query_id": "q2", "search_run_id": "run1", "status": "ok", "results": []},
            {"query_id": "q3", "search_run_id": "run1", "status": "error",
             "http_status": 503, "error_kind": "timeout", "results": []},
        ],
    }


class InputTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def write(self, name, value):
        path = self.root / name
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return path

    def ingest(self, data=None, name="out", **options):
        path = self.write(f"{name}.json", fixture() if data is None else data)
        output = self.root / name
        report = import_dataset(path, output, **options)
        return output, report

    def test_preserves_empty_failed_missing_duplicate_and_long_evidence(self):
        output, report = self.ingest(use_collection_time=True)
        self.assertEqual(report["status"], "ready")
        self.assertEqual(report["counts"]["response_status"],
                         {"ok": 1, "empty": 1, "error": 1, "missing_record": 1})
        dataset = load_dataset(output)
        self.assertEqual(len(dataset["queries"]), 4)
        results = dataset["responses"][0]["results"]
        self.assertEqual(len(results), 2)
        self.assertEqual(len(results[0]["snippet"]), 40000)
        self.assertNotEqual(results[0]["result_id"], results[1]["result_id"])
        self.assertEqual(results[0]["document_key"], results[1]["document_key"])
        self.assertEqual(results[0]["evidence_hash"], results[1]["evidence_hash"])
        self.assertEqual(dataset["manifest"]["evaluation_as_of"], "2026-10-20T05:36:00Z")
        self.assertEqual(report["issue_counts"]["EXACT_DUPLICATE_URL"], 1)

    def test_judge_projection_excludes_provider_rank_and_source_score(self):
        output, _ = self.ingest()
        items = list(iter_api_inputs(load_dataset(output)))
        self.assertEqual(len(items), 2)
        prompt = json.dumps(items[0]["judge_input"])
        for forbidden in ('"provider"', '"rank"', '"source_score"', '"id"', "0.91"):
            self.assertNotIn(forbidden, prompt)
        self.assertEqual(items[0]["collection_status"], "ok")

    def test_time_unknown_is_not_invented(self):
        data = fixture()
        data["responses"][0]["results"][0]["published_at"] = "2026-10-20"
        output, report = self.ingest(data)
        self.assertEqual(report["status"], "ready")
        dataset = load_dataset(output)
        result = dataset["responses"][0]["results"][0]
        self.assertIsNone(result["published_at"])
        self.assertEqual(result["published_at_raw"], "2026-10-20")
        self.assertIsNone(dataset["manifest"]["evaluation_as_of"])
        self.assertIn("EVALUATION_TIME_UNSPECIFIED", report["issue_counts"])

    def test_timezone_nanos_and_subsecond_collection_order(self):
        self.assertEqual(timestamp("2026-10-12T02:19:48.000000000Z"), "2026-10-12T02:19:48Z")
        with self.assertRaises(InputError):
            timestamp("2026-10-12T02:19:48")
        data = fixture()
        data["responses"][0]["retrieved_at"] = "2026-10-20T05:36:00.500Z"
        output, _ = self.ingest(data, use_collection_time=True)
        self.assertEqual(load_dataset(output)["manifest"]["evaluation_as_of"],
                         "2026-10-20T05:36:00.500000Z")

    def test_short_response_is_successful_not_failed(self):
        output, report = self.ingest()
        self.assertEqual(load_dataset(output)["responses"][0]["status"], "ok")
        self.assertIn("SHORT_RESPONSE", report["issue_counts"])

    def test_count_rank_and_status_corruption_blocks_consumption(self):
        corruptions = [
            ("RESULT_COUNT_MISMATCH", lambda r: r.update(reported_result_count=7)),
            ("DUPLICATE_RANK", lambda r: r["results"][1].update(rank=1)),
            ("NON_CONTIGUOUS_RANK", lambda r: r["results"][1].update(rank=3)),
            ("INVALID_FIELD", lambda r: r["results"][1].update(rank=True)),
            ("STATUS_CONFLICT", lambda r: r.update(status="empty")),
            ("STATUS_CONFLICT", lambda r: r.update(http_status=500)),
        ]
        for index, (code, corrupt) in enumerate(corruptions):
            with self.subTest(code=code, index=index):
                data = fixture()
                corrupt(data["responses"][0])
                output, report = self.ingest(data, name=f"bad{index}")
                self.assertEqual(report["status"], "blocked")
                self.assertIn(code, report["issue_counts"])
                self.assertTrue((output / "raw/source.json").is_file())
                with self.assertRaises(InputError):
                    load_dataset(output)

    def test_query_registry_conflicts_and_orphans_block(self):
        for index, change in enumerate((
            lambda d: d["queries"].append({"query_id": "q1", "query_text": "Different"}),
            lambda d: d["responses"][0].update(query_id="unknown"),
            lambda d: d["responses"].append(copy.deepcopy(d["responses"][0])),
        )):
            data = fixture()
            change(data)
            _, report = self.ingest(data, name=f"conflict{index}")
            self.assertEqual(report["status"], "blocked")

    def test_missing_titles_urls_and_text_are_preserved_as_warnings(self):
        data = fixture()
        data["responses"][0]["results"][0].update(url=None, title="", snippet=None)
        output, report = self.ingest(data)
        self.assertEqual(report["status"], "ready")
        result = load_dataset(output)["responses"][0]["results"][0]
        self.assertIsNone(result["url"])
        self.assertEqual(result["title"], "")
        self.assertIsNone(result["document_key"])
        self.assertIn("MISSING_API_TEXT", report["issue_counts"])

    def test_missing_execution_status_is_unknown_even_with_http_200(self):
        data = fixture()
        del data["responses"][1]["status"]
        data["responses"][1]["http_status"] = 200
        output, _ = self.ingest(data)
        self.assertEqual(load_dataset(output)["responses"][1]["status"], "unknown")

    def test_versions_are_reproducible_and_time_policy_changes_version(self):
        source = self.write("source.json", fixture())
        for name, options in (("a", {}), ("b", {}), ("c", {"use_collection_time": True})):
            import_dataset(source, self.root / name, **options)
        versions = [load_dataset(self.root / name)["manifest"]["dataset_version"]
                    for name in ("a", "b", "c")]
        self.assertEqual(versions[0], versions[1])
        self.assertNotEqual(versions[0], versions[2])
        with self.assertRaises(FileExistsError):
            import_dataset(source, self.root / "a")

    def test_mutated_bundle_or_manifest_is_rejected(self):
        output, _ = self.ingest()
        with (output / "responses.jsonl").open("a") as target:
            target.write("\n")
        with self.assertRaisesRegex(InputError, "checksum"):
            load_dataset(output)
        other, _ = self.ingest(name="other")
        manifest = json.loads((other / "manifest.json").read_text())
        manifest["evaluation_as_of"] = "2030-01-01T00:00:00Z"
        (other / "manifest.json").write_text(json.dumps(manifest))
        with self.assertRaisesRegex(InputError, "checksum"):
            load_dataset(other)

    def test_csv_mapping_and_empty_marker(self):
        source = self.root / "source.csv"
        source.write_text(
            "\ufeffqid,q,position,link,status,n\n"
            "q1,hello,1,https://example.com,ok,1\n"
            "q2,world,,,empty,0\n", encoding="utf-8")
        mapping = self.write("mapping.json", {
            "fields": {"query_id": "qid", "query_text": "q", "rank": "position",
                       "url": "link", "reported_result_count": "n"},
            "defaults": {"search_run_id": "r1", "provider": "exa", "requested_top_k": 10},
        })
        report = import_dataset(source, self.root / "csv", mapping_path=mapping)
        self.assertEqual(report["status"], "ready")
        self.assertEqual(report["counts"]["response_status"], {"empty": 1, "ok": 1})
        dataset = load_dataset(self.root / "csv")
        self.assertEqual(dataset["responses"][0]["results"][0]["rank"], 1)
        self.assertEqual(dataset["search_runs"][0]["provider"], "exa")

    def test_jsonl_mapping_allows_missing_optional_key_on_empty_rows(self):
        source = self.root / "source.jsonl"
        rows = [
            {"query_id": "q1", "query_text": "hello", "rank": 1,
             "url": "https://example.com", "desc": "evidence", "status": "ok"},
            {"query_id": "q2", "query_text": "world", "status": "empty"},
        ]
        source.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
        mapping = self.write("mapping.json", {
            "fields": {"snippet": "desc"},
            "defaults": {"search_run_id": "r1", "provider": "demo"},
        })
        report = import_dataset(source, self.root / "jsonl", mapping_path=mapping)
        self.assertEqual(report["counts"]["queries"], 2)
        self.assertEqual(report["status"], "ready")

    def test_flat_conflicting_shared_fields_block(self):
        source = self.root / "source.csv"
        source.write_text(
            "query_id,query_text,search_run_id,provider,rank,url,status\n"
            "q1,first,r1,demo,1,https://example.com/a,ok\n"
            "q1,second,r1,demo,2,https://example.com/b,ok\n")
        report = import_dataset(source, self.root / "out")
        self.assertEqual(report["status"], "blocked")
        self.assertIn("FIELD_CONFLICT", report["issue_counts"])

    def test_multiple_providers_keep_independent_responses_and_missing_slots(self):
        data = fixture()
        data["search_runs"].append({"search_run_id": "run2", "provider": "exa"})
        second = copy.deepcopy(data["responses"][0])
        second["search_run_id"] = "run2"
        data["responses"].append(second)
        output, report = self.ingest(data)
        self.assertEqual(report["counts"]["queries"], 4)
        self.assertEqual(report["counts"]["responses"], 8)
        results = [r["results"][0] for r in load_dataset(output)["responses"] if r["results"]]
        self.assertEqual(results[0]["document_key"], results[1]["document_key"])
        self.assertNotEqual(results[0]["result_id"], results[1]["result_id"])

    def test_reference_pool_can_include_unretrieved_docs_and_unknown_grades(self):
        data = fixture()
        data["reference_labels"] = [
            {"query_id": "q1", "document_key": "url_exact_v1:" + digest("https://example.com/other"),
             "grade": grade, "label_schema_version": "relevance-v1",
             "source": "human", "version": f"v{index}", "evidence_mode": "document",
             "reference_scope": "pooled"}
            for index, grade in enumerate((3, None))
        ]
        output, report = self.ingest(data)
        self.assertEqual(report["status"], "ready")
        labels = load_dataset(output)["reference_labels"]
        self.assertIsNone(labels[1]["grade"])
        self.assertEqual(report["capabilities"]["reference_scopes_declared"], ["pooled"])
        self.assertFalse(report["capabilities"]["reference_completeness_verified"])

    def test_page_snapshot_separate_from_api_and_no_fallback(self):
        data = fixture()
        data["page_snapshots"] = [{
            "page_snapshot_id": "p1", "requested_url": "https://example.com/page",
            "body_text": "Independent page content", "fetch_status": "ok",
            "fetched_at": "2026-10-20T05:36:00Z", "truncated": False,
        }]
        data["responses"][0]["results"][0]["page_snapshot_id"] = "p1"
        output, report = self.ingest(data)
        self.assertEqual(report["status"], "ready")
        loaded = load_dataset(output)
        self.assertEqual(loaded["page_snapshots"][0]["body_text"], "Independent page content")
        self.assertNotEqual(loaded["responses"][0]["results"][0]["snippet"],
                            loaded["page_snapshots"][0]["body_text"])
        data["responses"][0]["results"][1]["page_snapshot_id"] = "nonexistent"
        _, blocked = self.ingest(data, name="bad")
        self.assertIn("UNKNOWN_PAGE_SNAPSHOT", blocked["issue_counts"])

    def test_page_for_another_url_cannot_be_attached_to_result(self):
        data = fixture()
        data["page_snapshots"] = [{
            "page_snapshot_id": "p1", "requested_url": "https://example.com/other",
            "body_text": "Unrelated document content", "fetch_status": "ok",
        }]
        data["responses"][0]["results"][0]["page_snapshot_id"] = "p1"
        _, report = self.ingest(data)
        self.assertEqual(report["status"], "blocked")
        self.assertIn("PAGE_URL_MISMATCH", report["issue_counts"])

    def test_nonfinite_and_duplicate_json_keys_are_blocked(self):
        for index, text in enumerate(('{"queries": [], "queries": []}', '{"x": NaN}')):
            source = self.root / f"bad{index}.json"
            source.write_text(text)
            report = import_dataset(source, self.root / f"bad{index}")
            self.assertEqual(report["status"], "blocked")
            self.assertIn("IMPORT_FAILED", report["issue_counts"])


class SQLiteTests(unittest.TestCase):
    setUp = InputTests.setUp
    tearDown = InputTests.tearDown

    def create_sqlite(self, path, wal=False):
        c = sqlite3.connect(path)
        if wal:
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA wal_autocheckpoint=0")
        c.executescript("""
            CREATE TABLE sampling_run (run_id TEXT, top_k INTEGER, api_endpoint TEXT,
                created_at TEXT, completed_at TEXT, start_time TEXT, end_time TEXT);
            CREATE TABLE queries (query_id TEXT, run_id TEXT, query_text TEXT,
                collect_status TEXT, result_count INTEGER, collected_at TEXT);
            CREATE TABLE query_urls (url_id INTEGER, query_id TEXT, rank INTEGER,
                url TEXT, title TEXT, snippet TEXT, published_at TEXT, result_json TEXT);
        """)
        c.execute("INSERT INTO sampling_run VALUES (?,?,?,?,?,?,?)",
                  ("r", 10, "https://api.ateve.ai/v1/search", "2026-10-20T05:00:00Z",
                   "2026-10-20T06:00:00Z", "2026-10-12T02:19:48.000000000Z",
                   "2026-10-12T08:20:00.000000000Z"))
        c.execute("INSERT INTO queries VALUES (?,?,?,?,?,?)",
                  ("q", "r", "example query", "ok", 1, "2026-10-20T05:59:00Z"))
        api = {"url": "https://example.com", "title": "Example", "snippet": "Evidence",
               "published_at": "2026-10-19T00:00:00Z", "score": 0.5}
        c.execute("INSERT INTO query_urls VALUES (?,?,?,?,?,?,?,?)",
                  (1, "q", 1, api["url"], api["title"], api["snippet"],
                   api["published_at"], json.dumps(api)))
        c.commit()
        return c

    def test_sqlite_wal_snapshot_and_original_file_unchanged(self):
        source = self.root / "source.sqlite"
        c = self.create_sqlite(source, wal=True)
        try:
            before = source.read_bytes()
            report = import_dataset(source, self.root / "sqlite", use_collection_time=True)
            self.assertEqual(report["status"], "ready")
            self.assertEqual(source.read_bytes(), before)
            dataset = load_dataset(self.root / "sqlite")
            self.assertEqual(dataset["responses"][0]["results"][0]["snippet"], "Evidence")
            self.assertEqual(dataset["manifest"]["source_query_windows"][0]["start"],
                             "2026-10-12T02:19:48Z")
            self.assertEqual(dataset["manifest"]["evaluation_as_of"], "2026-10-20T06:00:00Z")
        finally:
            c.close()

    def test_sqlite_column_json_conflict_blocks(self):
        source = self.root / "source.sqlite"
        c = self.create_sqlite(source)
        c.execute("UPDATE query_urls SET title='Different'")
        c.commit()
        c.close()
        report = import_dataset(source, self.root / "sqlite")
        self.assertEqual(report["status"], "blocked")
        self.assertIn("FIELD_CONFLICT", report["issue_counts"])

    def test_sqlite_empty_json_object_does_not_bypass_consistency_check(self):
        source = self.root / "source.sqlite"
        c = self.create_sqlite(source)
        c.execute("UPDATE query_urls SET result_json='{}'")
        c.commit()
        c.close()
        report = import_dataset(source, self.root / "sqlite")
        self.assertEqual(report["status"], "blocked")
        self.assertIn("FIELD_CONFLICT", report["issue_counts"])


if __name__ == "__main__":
    unittest.main()
