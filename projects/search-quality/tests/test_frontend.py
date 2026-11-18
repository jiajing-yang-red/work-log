import http.client
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from frontend.__main__ import make_server
from frontend.service import ACTIVE, PROJECT, Conflict, Service, read_credentials
from quality_eval.schemes import prompt_for
from quality_eval.storage import checked_read, checked_write
from quality_input.common import InputError, canonical
from quality_input.pipeline import import_dataset
from quality_llm import LLMError


class FrontendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        (self.project / "examples").mkdir(parents=True)
        for name in ("evaluation.mock.json", "evaluation.deepseek.json", "evaluation.models.template.json",
                     "evaluation.qianwen.json", "evaluation.zhipu.json", "evaluation.ateve-jev.json"):
            shutil.copy(PROJECT / "examples" / name, self.project / "examples" / name)
        (self.project / ".env").write_text("export DEEPSEEK_API_KEY='frontend-fixture-secret'\n")
        source = {
            "queries": [{"query_id": "q1", "query_text": "ExampleDB installation"},
                        {"query_id": "q2", "query_text": "Empty query"}],
            "search_runs": [{"search_run_id": "s1", "provider": "ateve", "requested_top_k": 10,
                             "collection_completed_at": "2026-10-20T00:00:00Z"}],
            "responses": [
                {"query_id": "q1", "search_run_id": "s1", "status": "ok", "results": [
                    {"rank": rank, "url": "https://example.com/docs",
                     "title": "<script>untrusted()</script>", "snippet": "Install ExampleDB."}
                    for rank in (1, 2)]},
                {"query_id": "q2", "search_run_id": "s1", "status": "empty", "results": []},
            ],
        }
        raw = self.root / "input.json"
        raw.write_text(json.dumps(source))
        self.data_root = self.root / "data"
        self.data_root.mkdir()
        import_dataset(raw, self.data_root / "fixture", use_collection_time=True)
        self.service = Service(self.project, self.data_root)
        self.environment = patch.dict(os.environ, {"PYTHONPATH": str(PROJECT)})
        self.environment.start()

    def tearDown(self):
        self.service.close()
        self.environment.stop()
        self.temp.cleanup()

    def request(self, **overrides):
        return {"dataset": "fixture", "models": ["mock_a"], "query_model": "mock_a",
                "schemes": ["legacy_five_v1", "google_overview_v1"], "mode": "api_only",
                "limit": None, "repeats": 1, **overrides}

    def finish(self, job):
        process = self.service.processes[job["id"]]
        process.wait(timeout=15)
        # The watcher has only a local state transition left after process exit.
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            detail = self.service.detail(job["id"])
            if detail["job"]["status"] not in ACTIVE:
                return detail
            time.sleep(.02)
        self.fail("Worker did not finish")

    def test_complete_mock_run_filter_export_and_history_preserve_unknowns(self):
        preview = self.service.preview_scope({"dataset": "fixture", "limit": None})
        job = self.service.start(self.request())
        detail = self.finish(job)
        self.assertEqual(detail["job"]["status"], "completed")
        self.assertGreaterEqual(detail["job"]["finished_at"], detail["job"]["created_at"])
        self.assertTrue(detail["job"]["simulated"])
        self.assertEqual(detail["job"]["query_count"], 2)
        self.assertEqual(detail["job"]["query_count"], preview["selected"]["queries"])
        self.assertEqual(detail["job"]["result_count"], preview["selected"]["results"])
        self.assertEqual(detail["records"], 14)
        ranking = detail["ranking"]
        self.assertEqual(len(ranking["groups"]), 1)
        self.assertEqual(ranking["groups"][0]["eligible_queries"], 0)
        self.assertEqual(ranking["groups"][0]["excluded"], {"simulated": 2})
        self.assertNotIn("queries", ranking["groups"][0])
        ranking_export = json.loads(self.service.download(job["id"], "ranking_metrics.json"))
        self.assertEqual(len(ranking_export["groups"][0]["queries"]), 2)
        self.assertTrue((self.service.root_for(job["id"]) / 'ranking_metrics.json').is_file())
        distributions = detail["answerability_distributions"]
        self.assertEqual(len(distributions), 1)
        for distribution in distributions:
            self.assertEqual(distribution["expected"], 2)
            self.assertEqual(distribution["rated"], 0)
            self.assertEqual(distribution["excluded"], {"simulated": 2})
            self.assertEqual(distribution["pending"], 0)
        self.assertEqual(detail["metrics"]["page_quality"]["statuses"]["unjudgeable"], 2)
        self.assertEqual(detail["summary"]["selected_query_count"], 2)
        self.assertEqual(len([q for q in detail["query_executions"] if not q["result_count"]]), 1)
        self.assertEqual(detail["comparison"]["pairs"], [])
        results = self.service.results(job["id"], {"scheme": "legacy_five_v1", "model": "mock_a",
                                                   "dimension": "freshness", "status": "not_applicable"})
        self.assertEqual(results["total"], 2)
        self.assertEqual(results["items"][0]["source"]["snippet"], "Install ExampleDB.")
        self.assertIsNotNone(results["items"][0]["query_spec"])
        self.assertEqual(self.service.results(job["id"], {"q": "nonmatching"})["total"], 0)
        exported = self.service.download(job["id"], "judgments.jsonl")
        self.assertEqual(len(exported.splitlines()), 14)
        self.assertNotIn(b"frontend-fixture-secret", exported)
        with self.assertRaises(FileNotFoundError):
            self.service.download(job["id"], "../../.env")
        other = Service(self.project, self.data_root)
        self.assertEqual(other.jobs()[0]["status"], "completed")

    def test_scope_preview_counts_selected_instances_and_usable_snapshot_coverage(self):
        source = json.loads((self.root / "input.json").read_text())
        source["queries"] = [{"query_id": "q3", "query_text": "Third query"}, *reversed(source["queries"])]
        source["search_runs"].append({**source["search_runs"][0], "search_run_id": "s2", "provider": "exa"})
        source["page_snapshots"] = [{
            "page_snapshot_id": pid, "requested_url": "https://example.com/docs",
            "body_text": "Independent page content.", "fetch_status": "ok",
            "extraction_status": "complete", "truncated": truncated,
            "fetched_at": "2026-10-20T00:00:00Z",
        } for pid, truncated in (("p1", False), ("p2", True), ("p3", False))]
        for item in source["responses"][0]["results"]:
            item["page_snapshot_id"] = "p1"
        source["responses"][0]["results"].append({
            "rank": 3, "url": "https://example.com/docs", "page_snapshot_id": "p2",
        })
        source["responses"].extend([
            {"query_id": "q1", "search_run_id": "s2", "status": "ok", "results": [
                {"rank": 1, "url": "https://example.com/docs", "page_snapshot_id": "p1"}]},
            {"query_id": "q3", "search_run_id": "s1", "status": "ok", "results": [
                {"rank": 1, "url": "https://example.com/docs", "page_snapshot_id": "p3"}]},
        ])
        path = self.root / "scope.json"
        path.write_text(json.dumps(source))
        report = import_dataset(path, self.data_root / "scope", use_collection_time=True)
        self.assertEqual(report["status"], "ready")
        with patch("frontend.service.subprocess.Popen") as process:
            one = self.service.preview_scope({"dataset": "scope", "limit": 1})
            two = self.service.preview_scope({"dataset": "scope", "limit": 2})
            all_queries = self.service.preview_scope({"dataset": "scope", "limit": None})
            capped = self.service.preview_scope({"dataset": "scope", "limit": 50})
            process.assert_not_called()
        # q1 sorts first, despite being last in the file; repeated URLs across providers remain instances.
        self.assertEqual(one["selected"], {
            "queries": 1, "results": 4, "queries_without_results": 0,
            "usable_pages": 1, "results_with_usable_pages": 3, "human_labels": 0,
        })
        self.assertEqual(one["total"], {"queries": 3, "results": 5, "pages": 3, "human_labels": 0})
        self.assertEqual(two["selected"]["results"], 4)
        self.assertEqual(two["selected"]["queries_without_results"], 1)
        self.assertEqual(all_queries["selected"]["results"], 5)
        self.assertEqual(all_queries["selected"]["usable_pages"], 2)
        self.assertEqual(all_queries["selected"]["results_with_usable_pages"], 4)
        self.assertEqual(capped, all_queries)
        self.assertEqual(self.service.jobs(), [])
        self.assertNotIn("body_text", canonical(one))

    def test_scope_preview_rejects_invalid_selection_and_changed_data(self):
        for request in (None, [], {"dataset": []}, {"dataset": "../fixture"},
                        {"dataset": "fixture", "models": ["mock_a"]},
                        *({"dataset": "fixture", "limit": limit} for limit in (0, -1, True, "1"))):
            with self.subTest(request=request), self.assertRaises(InputError):
                self.service.preview_scope(request)
        preview = self.service.preview_scope({"dataset": "fixture", "limit": 1})
        self.assertEqual(preview["selected"]["results"], 2)
        self.assertEqual(preview["selected"]["usable_pages"], 0)
        with (self.data_root / "fixture" / "queries.jsonl").open("a") as handle:
            handle.write("\n")
        with self.assertRaises(InputError):
            self.service.preview_scope({"dataset": "fixture", "limit": 1})

    def test_profile_and_dataset_validation_before_start(self):
        invalid = [
            {"models": ["mock_a", "deepseek"]}, {"query_model": "missing"},
            {"query_model": "deepseek"},
            {"models": ["deepseek"], "query_model": "mock_a"},
            {"schemes": []}, {"schemes": ["google_overview_v1", "google_overview_v1"]},
            {"models": ["openai"], "query_model": "openai"}, {"dataset": "../../.env"},
            {"mode": "page_content"}, {"limit": True}, {"limit": 0}, {"repeats": 11},
        ]
        for values in invalid:
            with self.subTest(values=values), self.assertRaises(InputError):
                self.service.start(self.request(**values))
        self.assertEqual(self.service.jobs(), [])

    def test_load_local_file_imports_snapshot_and_runs_from_new_dataset(self):
        source = self.root / "input.json"
        original = source.read_bytes()
        with patch("frontend.service.subprocess.Popen") as process:
            loaded = self.service.dataset_store.add({"path": f'"{source}"', "name": "新数据集"})
            process.assert_not_called()
        self.assertEqual(loaded["status"], "ready")
        self.assertTrue(loaded["imported"])
        item = loaded["dataset"]
        self.assertEqual(item["source_path"], str(source.resolve()))
        self.assertEqual(item["name"], "新数据集")
        self.assertEqual(item["queries"], 2)
        self.assertEqual(item["as_of"], "2026-10-20T00:00:00Z")
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual((Path(item["path"]) / "raw/source.json").read_bytes(), original)
        other = Service(self.project, self.data_root)
        self.assertIn(item["id"], {d["id"] for d in other.datasets()})
        job = self.service.start(self.request(dataset=item["id"], query_understanding="direct"))
        self.assertEqual(self.finish(job)["job"]["status"], "completed")
        self.assertEqual(job["dataset_path"], item["path"])

    def test_external_dataset_directories_persist_without_name_collisions_or_copies(self):
        original = self.data_root / "fixture"
        external = self.root / "elsewhere" / "fixture"
        shutil.copytree(original, external)
        files = {str(p.relative_to(external)): p.read_bytes() for p in external.rglob("*") if p.is_file()}
        store = self.service.dataset_store
        loaded = store.add({"path": str(external / "manifest.json")})
        self.assertFalse(loaded["imported"])
        item = loaded["dataset"]
        self.assertTrue(item["external"])
        self.assertNotEqual(item["id"], "fixture")
        self.assertEqual(item["path"], str(external.resolve()))
        store.add({"path": str(external)})
        self.assertEqual(len(store.registered_paths()), 1)
        other = Service(self.project, self.data_root)
        self.assertIn(item["id"], {d["id"] for d in other.datasets()})
        job = self.service.start(self.request(dataset=item["id"], query_understanding="direct"))
        self.assertEqual(self.finish(job)["job"]["status"], "completed")
        self.assertEqual(job["dataset_path"], str(external.resolve()))
        self.assertEqual({str(p.relative_to(external)): p.read_bytes()
                          for p in external.rglob("*") if p.is_file()}, files)
        with (external / "queries.jsonl").open("a") as f:
            f.write("\n")
        with self.assertRaisesRegex(InputError, "checksum"):
            store.add({"path": str(external)})
        with self.assertRaisesRegex(InputError, "checksum"):
            self.service.start(self.request(dataset=item["id"], query_understanding="direct"))

    def test_dataset_import_errors_are_reported_and_not_selectable(self):
        store = self.service.dataset_store
        bad = self.root / "bad.json"
        bad.write_text('{"not":"a supported dataset"}')
        loaded = store.add({"path": str(bad)})
        self.assertEqual(loaded["status"], "blocked")
        self.assertTrue(Path(loaded["report_path"]).is_file())
        self.assertTrue(loaded["report"]["issues"])
        self.assertEqual([d["id"] for d in store.list()], ["fixture"])
        source = self.root / "input.json"
        for request in ({"path": "relative.json"}, {"path": str(self.root / "missing.json")},
                        {"path": str(self.project / ".env")}, {"path": str(self.root)},
                        {"path": str(source), "time_policy": "explicit"},
                        {"path": str(source), "time_policy": "explicit", "evaluation_as_of": "2026-10-21"},
                        {"path": str(source), "time_policy": "bad"}):
            with self.subTest(request=request), self.assertRaises(InputError):
                store.add(request)
        store.lock.acquire()
        try:
            with self.assertRaisesRegex(InputError, "正在导入"):
                store.add({"path": str(source)})
        finally:
            store.lock.release()

    def test_local_file_browser_navigation_filter_and_pagination(self):
        folder = self.root / "files with spaces"
        folder.mkdir()
        (folder / "nested").mkdir()
        (folder / "nested" / "manifest.json").write_text("{}")
        (folder / ".hidden.json").write_text("{}")
        (folder / "unrelated.txt").write_text("text")
        for i in range(105):
            (folder / f"input-{i:03}.json").write_text("{}")
        first = self.service.dataset_store.browse({"path": str(folder)})
        self.assertEqual(first["total"], 106)
        self.assertEqual(len(first["items"]), 100)
        self.assertTrue(first["items"][0]["bundle"])
        self.assertEqual(first["parent"], str(self.root.resolve()))
        last = self.service.dataset_store.browse({"path": str(folder), "page": 2})
        self.assertEqual(len(last["items"]), 6)
        filtered = self.service.dataset_store.browse({"path": str(folder), "filter": "input-104"})
        self.assertEqual([e["name"] for e in filtered["items"]], ["input-104.json"])
        self.assertEqual(self.service.dataset_store.browse({"path": str(folder / "input-104.json")})["path"],
                         str(folder.resolve()))
        with self.assertRaises(InputError):
            self.service.dataset_store.browse({"path": str(folder), "page": True})

    def test_flat_file_import_supports_mapping_and_explicit_or_unknown_time(self):
        store = self.service.dataset_store
        source = self.root / "custom.csv"
        source.write_text("qid,q,position,link,status\nq1,hello,1,https://example.com,ok\n")
        mapping = self.root / "mapping.json"
        mapping.write_text(json.dumps({
            "fields": {"query_id": "qid", "query_text": "q", "rank": "position", "url": "link"},
            "defaults": {"search_run_id": "r1", "provider": "exa"},
        }))
        loaded = store.add({"path": str(source), "mapping_path": str(mapping), "time_policy": "explicit",
                            "evaluation_as_of": "2026-10-21T12:00:00+08:00"})
        self.assertEqual(loaded["status"], "ready")
        self.assertEqual(loaded["dataset"]["name"], "custom")
        self.assertEqual(loaded["dataset"]["as_of"], "2026-10-21T04:00:00Z")
        jsonl = self.root / "flat.jsonl"
        jsonl.write_text(json.dumps({"query_id": "q1", "query_text": "hello", "search_run_id": "s1",
                                     "provider": "demo", "status": "ok", "rank": 1,
                                     "url": "https://example.com", "snippet": "Evidence"}))
        missing_time = store.add({"path": str(jsonl)})
        self.assertEqual(missing_time["status"], "blocked")
        self.assertIn("EVALUATION_TIME_UNAVAILABLE", missing_time["report"]["issue_counts"])
        loaded = store.add({"path": str(jsonl), "time_policy": "unspecified"})
        self.assertEqual(loaded["status"], "ready")
        self.assertIsNone(loaded["dataset"]["as_of"])

    def test_prompt_preview_tracks_selected_scheme_and_understanding_without_calls(self):
        with patch("frontend.service.list_models") as models, patch("frontend.service.subprocess.Popen") as process:
            direct = self.service.prompt_templates({"schemes": "legacy_five_v1", "query_understanding": "direct"})
            self.assertEqual([p["kind"] for p in direct["items"]], ["legacy_url", "duplicates"])
            item = direct["items"][0]
            self.assertEqual(item["system"], prompt_for("legacy_url", "direct"))
            self.assertIn("当前已关闭独立 query", item["rules"])
            self.assertNotIn("Output JSON schema:", item["rules"])
            self.assertIn("query_assessment", item["schema"]["properties"])
            self.assertIsNone(item["payload"])
            self.assertIsNone(item["user"])
            self.assertEqual(item["messages"], [{"role": "system", "content": item["system"]}])
            frozen = self.service.prompt_templates({"schemes": "google_overview_v1", "query_understanding": "frozen"})
            self.assertEqual([p["kind"] for p in frozen["items"]], ["query_spec", "google_nm", "google_pq"])
            models.assert_not_called()
            process.assert_not_called()
        for filters in ({"schemes": "../../.env"}, {"query_understanding": "bad"}):
            with self.assertRaises(InputError):
                self.service.prompt_templates(filters)
        self.assertEqual(self.service.jobs(), [])

    def test_editable_prompts_are_versioned_previewed_and_frozen_per_run(self):
        request = {"kind": "answerability", "query_understanding": "direct",
                   "name": "人工调试 v1", "rules": "CUSTOM A：只评价当前结果的可回答性。\n保留逐字证据。"}
        with patch("frontend.service.subprocess.Popen") as process:
            first = self.service.prompt_store.save(request)
            preview = self.service.prompt_templates({"schemes": "answerability_only_v1", "query_understanding": "direct",
                "prompt_versions": json.dumps({"answerability": first["id"]})})["items"][0]
            process.assert_not_called()
        self.assertEqual(preview["rules"], request["rules"])
        self.assertEqual(preview["prompt_version"]["id"], first["id"])
        self.assertIn("Output JSON schema:", preview["system"])
        self.assertNotIn("CUSTOM A", preview["default_rules"])
        job = self.service.start(self.request(schemes=["answerability_only_v1"], query_understanding="direct",
                                             prompt_versions={"answerability": first["id"]}))
        second = self.service.prompt_store.save({**request, "name": "人工调试 v2", "rules": "CUSTOM B：新的分档规则。"})
        self.assertNotEqual(first["id"], second["id"])
        self.finish(job)
        root = self.service.root_for(job["id"])
        config = json.loads((root / "config.json").read_text())
        self.assertEqual(config["prompt_overrides"], {"answerability": request["rules"]})
        tasks = self.service.prompt_tasks(job["id"])["items"]
        for task in tasks:
            saved = self.service.task_prompt(job["id"], task["id"])
            self.assertEqual(saved["system"], preview["system"])
            self.assertNotIn("CUSTOM A", saved["user"])
            self.assertEqual(saved["prompt_version"]["id"], first["id"])
            self.assertEqual(saved["prompt_hash"], first["prompt_hash"])
        old_system = self.service.task_prompt(job["id"], tasks[0]["id"])["system"]
        newer = self.service.start(self.request(schemes=["answerability_only_v1"], query_understanding="direct",
                                               prompt_versions={"answerability": second["id"]}))
        self.finish(newer)
        new_task = self.service.prompt_tasks(newer["id"])["items"][0]
        self.assertNotEqual(new_task["id"], tasks[0]["id"])
        self.assertTrue(self.service.task_prompt(newer["id"], new_task["id"])["system"].startswith("CUSTOM B"))
        self.assertEqual(self.service.task_prompt(job["id"], tasks[0]["id"])["system"], old_system)

    def test_prompt_versions_reject_invalid_rules_and_wrong_task_bindings(self):
        base = {"kind": "answerability", "query_understanding": "direct", "name": "test", "rules": "评分规则"}
        for change in ({"rules": " "}, {"rules": "x"*50001}, {"rules": "Output JSON schema:\n{}"},
                       {"name": ""}, {"kind": "../../.env"}, {"query_understanding": "bad"}, {"extra": True}):
            with self.subTest(change=list(change)), self.assertRaises(InputError):
                self.service.prompt_store.save({**base, **change})
        item = self.service.prompt_store.save(base)
        for selection, kinds, mode in (({"query_spec": item["id"]}, ["query_spec"], "frozen"),
                                      ({"answerability": item["id"]}, ["answerability"], "frozen"),
                                      ({"answerability": "../.env"}, ["answerability"], "direct"),
                                      ({"answerability": item["id"]}, ["legacy_url"], "direct")):
            with self.assertRaises(InputError):
                self.service.prompt_store.resolve(selection, kinds, mode)
        with self.assertRaises(InputError):
            self.service.start(self.request(schemes=["answerability_only_v1"], query_understanding="frozen",
                                           prompt_versions={"answerability": item["id"]}))
        self.assertEqual(self.service.jobs(), [])

    def test_saved_prompts_are_exact_snapshots_and_cover_all_task_kinds(self):
        job = self.service.start(self.request())
        self.finish(job)
        root = self.service.root_for(job["id"])
        tasks = self.service.prompt_tasks(job["id"])["items"]
        self.assertEqual({t["kind"] for t in tasks},
                         {"query_spec", "legacy_url", "duplicates", "google_nm", "google_pq"})
        self.assertEqual(len({t["id"] for t in tasks}), len(tasks))
        for item in tasks:
            folder = "specs" if item["kind"] == "query_spec" else "evaluation"
            path = root / folder / "tasks" / (item["id"] + ".json")
            before = path.read_bytes()
            saved = checked_read(path)
            with patch("frontend.service.prompt_for", side_effect=AssertionError("Must not rebuild history")):
                result = self.service.task_prompt(job["id"], item["id"])
            self.assertTrue(result["available"])
            self.assertEqual(result["system"], saved["request"]["system"])
            self.assertEqual(result["user"], canonical(saved["request"]["payload"]))
            self.assertEqual(result["payload"], saved["request"]["payload"])
            self.assertEqual(result["messages"], [
                {"role": "system", "content": saved["request"]["system"]},
                {"role": "user", "content": canonical(saved["request"]["payload"])},
            ])
            self.assertIn("Output JSON schema:", result["messages"][0]["content"])
            self.assertFalse(result["wire_request"]["available"])
            self.assertEqual(result["invocation"], "not_called" if saved.get("blocked_reason") else "simulated")
            self.assertIsInstance(result["schema"], dict)
            self.assertEqual(path.read_bytes(), before)
            self.assertNotIn("frontend-fixture-secret", json.dumps(result))
            self.assertNotIn("call", result)
            if item["kind"] == "google_pq":
                self.assertEqual(result["blocked_reason"], "page_snapshot_missing")

    def test_prompt_snapshot_missing_and_invalid_paths_do_not_fall_back_to_current_rules(self):
        job = self.service.start(self.request(query_understanding="direct"))
        self.finish(job)
        item = self.service.prompt_tasks(job["id"])["items"][0]
        path = self.service.root_for(job["id"]) / "evaluation/tasks" / (item["id"] + ".json")
        saved = checked_read(path)
        saved.pop("request")
        checked_write(path, saved)
        result = self.service.task_prompt(job["id"], item["id"])
        self.assertFalse(result["available"])
        self.assertNotIn("system", result)
        self.assertNotIn("messages", result)
        self.assertFalse(result["wire_request"]["available"])
        with self.assertRaises(InputError):
            self.service.task_prompt(job["id"], "../../.env")
        with self.assertRaises(FileNotFoundError):
            self.service.task_prompt(job["id"], "f" * 64)
        path.unlink()
        path.symlink_to(self.project / ".env")
        with self.assertRaises(InputError):
            self.service.task_prompt(job["id"], item["id"])
        with self.assertRaises(InputError):
            self.service.task_response(job["id"], item["id"])

    def test_prompt_request_unavailable_config_keeps_messages_and_marks_not_called(self):
        job = self.service.start(self.request(query_understanding="direct"))
        self.finish(job)
        root = self.service.root_for(job["id"])
        item = self.service.prompt_tasks(job["id"])["items"][0]
        path = root / "evaluation/tasks" / (item["id"] + ".json")
        saved = checked_read(path)
        saved.update(simulated=False, status="error", call=None, error_code="missing_credentials")
        checked_write(path, saved)
        (root / "config.json").unlink()
        with patch("frontend.service.read_credentials", side_effect=AssertionError("No credentials needed")), \
                patch("frontend.service.prompt_for", side_effect=AssertionError("No current template fallback")):
            result = self.service.task_prompt(job["id"], item["id"])
        self.assertEqual(result["invocation"], "not_called")
        self.assertEqual(result["messages"][1]["content"], canonical(saved["request"]["payload"]))
        self.assertFalse(result["wire_request"]["available"])

    def test_response_view_reads_old_and_new_snapshots_without_model_calls(self):
        job = self.service.start(self.request())
        self.finish(job)
        items = self.service.prompt_tasks(job["id"])["items"]
        with patch("quality_llm.LLMClient.complete", side_effect=AssertionError("No new model call")):
            for item in items:
                result = self.service.task_response(job["id"], item["id"])
                if item["kind"] == "google_pq":
                    self.assertEqual(result["availability"], "not_called")
                else:
                    self.assertEqual(result["availability"], "legacy_text")
                    self.assertTrue(result["diagnostics"]["attempts"][0]["response"]["text"])
                self.assertNotIn("frontend-fixture-secret", json.dumps(result))
        item = next(t for t in items if t["kind"] == "legacy_url")
        path = self.service.root_for(job["id"]) / "evaluation/tasks" / (item["id"] + ".json")
        saved = checked_read(path)
        saved.update(status="error", call=None, error_code="incomplete_response", simulated=False)
        saved.pop("request")  # Response remains readable even without a prompt snapshot.
        checked_write(path, saved)
        self.assertEqual(self.service.task_response(job["id"], item["id"])["availability"], "not_recorded")
        saved["response_diagnostics"] = {"attempts": [{"attempt": 1, "outcome": "incomplete_response",
                                                       "response": {"text": '{"partial":', "finish_reason": "length"}}]}
        checked_write(path, saved)
        before = path.read_bytes()
        result = self.service.task_response(job["id"], item["id"])
        self.assertEqual(result["availability"], "recorded")
        self.assertEqual(result["diagnostics"], saved["response_diagnostics"])
        self.assertEqual(result["failure"]["code"], "output_truncated")
        self.assertEqual(path.read_bytes(), before)
        with self.assertRaises(InputError):
            self.service.task_response(job["id"], "../../.env")
        with self.assertRaises(FileNotFoundError):
            self.service.task_response(job["id"], "f" * 64)

    def test_historical_stream_failure_is_explained_without_rewriting_results(self):
        job = self.service.start(self.request(query_understanding="direct", schemes=["legacy_five_v1"]))
        self.finish(job)
        root = self.service.root_for(job["id"])
        item = next(t for t in self.service.prompt_tasks(job["id"])["items"] if t["kind"] == "legacy_url")
        task_path = root / "evaluation/tasks" / (item["id"] + ".json")
        saved = checked_read(task_path)
        saved.update(status="error", call=None, error_code="incomplete_response", simulated=False)
        saved["response_diagnostics"] = {"attempts": [{"attempt": 1, "outcome": "incomplete_response",
            "http_status": 200, "latency_seconds": 121.5,
            "response": {"text": "", "response_id": "test-response", "stream_info": {
                "completed": False, "events": 1397, "error": "Stream interrupted or timed out"}}}]}
        checked_write(task_path, saved)
        config_path = root / "config.json"
        config = json.loads(config_path.read_text())
        config["models"][saved["model_profile"]].update(stream=True, timeout_seconds=120)
        config_path.write_text(json.dumps(config))
        judgments_path = root / "evaluation/judgments.jsonl"
        rows = [json.loads(line) for line in judgments_path.read_text().splitlines()]
        for row in rows:
            if row["task_id"] == item["id"]:
                row.update(judgment_status="error", task_status="error", value=None,
                           error_code="incomplete_response", reason="Incomplete response")
        judgments_path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        before = {p: p.read_bytes() for p in (task_path, config_path, judgments_path)}
        with patch("quality_llm.LLMClient.complete", side_effect=AssertionError("No new model call")):
            response = self.service.task_response(job["id"], item["id"])
            self.assertEqual(response["failure"]["code"], "stream_timeout_or_interrupted")
            self.assertEqual(response["error_code"], "incomplete_response")
            self.assertEqual(response["limits"]["stream_timeout_seconds"], 120)
            self.assertEqual(response["diagnostics"]["attempts"][0]["response"]["text"], "")
            detail = self.service.detail(job["id"])
            self.assertEqual({f["code"] for f in detail["failures"]}, {"stream_timeout_or_interrupted"})
            results = self.service.results(job["id"], {"status": "error"})
            failures = [m["failure"]["code"] for r in results["items"]
                        for m in r["dimensions"].values() if m["failure"]]
            self.assertTrue(failures)
            self.assertEqual(set(failures), {"stream_timeout_or_interrupted"})
        self.assertEqual(self.service.download(job["id"], "judgments.jsonl"), before[judgments_path])
        self.assertEqual({p: p.read_bytes() for p in before}, before)

    def test_delete_restore_removes_results_without_changing_input_or_original_files(self):
        job = self.service.start(self.request(query_understanding="direct"))
        self.finish(job)
        root = self.service.root_for(job["id"])
        original = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}
        input_manifest = (self.data_root / "fixture/manifest.json").read_bytes()
        self.service.detail(job["id"])
        self.service.results(job["id"], {})
        self.assertTrue(self.service._context_cache)
        self.assertTrue(self.service._result_cache)
        self.assertEqual(self.service.delete(job["id"]), {"id": job["id"], "deleted": True})
        self.assertEqual(self.service.jobs(), [])
        self.assertFalse(root.exists())
        self.assertEqual(self.service._result_cache, {})
        self.assertEqual(self.service._context_cache, {})
        for read in (lambda: self.service.detail(job["id"]),
                     lambda: self.service.results(job["id"], {}),
                     lambda: self.service.download(job["id"], "judgments.jsonl"),
                     lambda: self.service.delete(job["id"])):
            with self.assertRaises(FileNotFoundError):
                read()
        # Restore also works after restarting the service, without spawning a worker.
        other = Service(self.project, self.data_root)
        self.assertEqual(other.jobs(), [])
        self.assertEqual(other.restore(job["id"])["id"], job["id"])
        self.assertEqual(other.processes, {})
        self.assertEqual(len(other.jobs()), 1)
        self.assertEqual({str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()},
                         original)
        self.assertEqual((self.data_root / "fixture/manifest.json").read_bytes(), input_manifest)
        self.assertGreater(other.detail(job["id"])["records"], 0)
        with self.assertRaises(FileNotFoundError):
            other.restore(job["id"])

    def test_delete_rejects_path_escape_and_symlinks(self):
        for invalid in ("../fixture", ".trash", "", "f" * 31):
            with self.subTest(invalid=invalid):
                with self.assertRaises(InputError):
                    self.service.delete(invalid)
                with self.assertRaises(InputError):
                    self.service.restore(invalid)
        outside = self.root / "untouched"
        outside.mkdir()
        (outside / "keep").write_text("keep")
        link = self.service.runs_root / ("f" * 32)
        link.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(FileNotFoundError):
            self.service.delete(link.name)
        (self.service.runs_root / ".trash").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(InputError):
            self.service.restore("f" * 32)
        self.assertEqual((outside / "keep").read_text(), "keep")

    def test_failure_diagnostics_support_old_records_and_preserve_exports(self):
        job = self.service.start(self.request(query_understanding="direct"))
        self.finish(job)
        path = self.service.root_for(job["id"]) / "evaluation/judgments.jsonl"
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        rows[0].update(judgment_status="error", value=None, task_status="invalid_output",
                       reason="Evidence path does not exist")
        rows[0].pop("error_code", None)  # Historical records did not store error codes.
        rows[1].update(judgment_status="error", value=None, error_code="transport_error",
                       reason="Network request failed or timed out")
        rows[2]["evidence_repairs"] = [{"original_path": "/result/metadata/url",
                                       "corrected_path": "/result/url"}]
        path.write_text("".join(json.dumps(r) + "\n" for r in rows))
        before = path.read_bytes()
        detail = self.service.detail(job["id"])
        self.assertEqual({f["code"] for f in detail["failures"]},
                         {"evidence_path_missing", "transport_error"})
        self.assertEqual(detail["evidence_repairs"], {"records": 1, "count": 1})
        results = self.service.results(job["id"], {"status": "error"})
        labels = {m["failure"]["label"] for r in results["items"]
                  for m in r["dimensions"].values() if m["failure"]}
        self.assertEqual(labels, {"证据路径错误", "接口连接失败"})
        exported = self.service.download(job["id"], "judgments.jsonl")
        self.assertNotIn(b'"failure":', exported)
        self.assertEqual(path.read_bytes(), before)

    def test_query_model_can_be_independent_of_scoring_models(self):
        job = self.service.start(self.request(models=["mock_a"], query_model="mock_b"))
        detail = self.finish(job)
        self.assertEqual(detail["job"]["status"], "completed")
        self.assertEqual(detail["job"]["query_model"], "mock_b")
        self.assertEqual(detail["job"]["models"], ["mock_a"])
        root = self.service.root_for(job["id"])
        config = json.loads((root / "config.json").read_text())
        self.assertEqual(set(config["models"]), {"mock_a", "mock_b"})
        self.assertEqual({e["model"] for e in config["experiments"]}, {"mock_a"})
        specs = [json.loads(line) for line in (root / "specs/query_specs.jsonl").read_text().splitlines()]
        self.assertEqual({s["provenance"]["model_profile"] for s in specs}, {"mock_b"})
        results = self.service.results(job["id"], {})
        self.assertEqual({row["model"] for row in results["items"]}, {"mock_a"})
        self.assertEqual(detail["records"], 14)

    def test_qianwen_streaming_evaluation_with_independent_parser_and_no_models_api(self):
        from test_evaluation_layer import valid_answer
        from test_streaming import event
        records = []
        class Provider(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                records.append((self.path, self.headers.get('Authorization'), body))
                payload = json.loads(body['messages'][1]['content'])
                kind = 'legacy_url' if 'result' in payload else 'duplicates' if 'results' in payload else 'query_spec'
                text = json.dumps(valid_answer(kind, payload), ensure_ascii=False)
                middle = len(text) // 2
                raw = event(text[:middle], model=body['model']) + event(text[middle:], 'stop', model=body['model'])
                raw += event(choices=[], model=body['model'], usage={'prompt_tokens': 20, 'completion_tokens': 40, 'total_tokens': 60})
                raw += b'data: [DONE]\n\n'
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            config_path = self.project / 'examples/evaluation.qianwen.json'
            config = json.loads(config_path.read_text())
            config['models']['qianwen'].update(base_url=f'http://127.0.0.1:{server.server_port}/compatible-mode/v1', requests_per_second=1000)
            config_path.write_text(json.dumps(config))
            with patch.dict(os.environ, {'QIANWEN_API_KEY': ''}):
                self.assertFalse(next(m for m in self.service.catalog()['models'] if m['id'] == 'qianwen')['ready'])
            with (self.project / '.env').open('a') as env:
                env.write("\nexport QIANWEN_API_KEY='qianwen-fixture-key'\n")
            group = self.service.discover_models({'profiles': ['qianwen']})['groups'][0]
            self.assertEqual(group['source'], 'configured')
            self.assertEqual(records, [])
            custom_versions = {kind: self.service.prompt_store.save({"kind": kind,
                "query_understanding": "frozen", "name": "Custom " + kind,
                "rules": "CUSTOM WIRE " + kind + "：按约定 JSON 格式输出。"})
                for kind in ('query_spec', 'legacy_url')}
            job = self.service.start(self.request(models=['qianwen'], query_model='qianwen',
                scoring_model_ids={'qianwen': 'qwen3.8-flash'}, query_model_id='qwen3.7-plus', schemes=['legacy_five_v1'],
                prompt_versions={kind: version['id'] for kind, version in custom_versions.items()}))
            detail = self.finish(job)
            self.assertEqual(detail['job']['status'], 'completed')
            self.assertFalse(detail['job']['simulated'])
            self.assertEqual({r[2]['model'] for r in records}, {'qwen3.8-flash', 'qwen3.7-plus'})
            self.assertTrue(all(r[0] == '/compatible-mode/v1/chat/completions' and r[1] == 'Bearer qianwen-fixture-key' for r in records))
            self.assertTrue(all(r[2]['stream'] and r[2]['stream_options']['include_usage'] for r in records))
            self.assertTrue(all('max_tokens' in r[2] and 'catalog_models' not in r[2] for r in records))
            # Changing today's configuration must not change historical request inspection.
            config['models']['qianwen']['parameters'] = {'temperature': 0.9}
            config_path.write_text(json.dumps(config))
            for item in self.service.prompt_tasks(job['id'])['items']:
                with patch('frontend.service.read_credentials', side_effect=AssertionError('Must not read keys')):
                    prompt = self.service.task_prompt(job['id'], item['id'])
                wire = prompt['wire_request']
                self.assertEqual(prompt['invocation'], 'attempted')
                self.assertEqual(wire['source'], 'reconstructed')
                self.assertEqual(wire['protocol'], 'openai_chat')
                self.assertTrue(wire['endpoint'].endswith('/compatible-mode/v1/chat/completions'))
                self.assertIn(wire['body'], [record[2] for record in records])
                self.assertEqual(prompt['messages'], wire['body']['messages'])
                if item['kind'] in custom_versions:
                    self.assertTrue(wire['body']['messages'][0]['content'].startswith(custom_versions[item['kind']]['rules']))
                    self.assertIn('Output JSON schema:', wire['body']['messages'][0]['content'])
                    self.assertNotIn('CUSTOM WIRE', wire['body']['messages'][1]['content'])
                self.assertNotIn('qianwen-fixture-key', json.dumps(prompt))
                self.assertNotIn('Authorization', json.dumps(wire))
                response = self.service.task_response(job['id'], item['id'])
                attempt = response['diagnostics']['attempts'][0]
                self.assertTrue(attempt['response']['stream_info']['completed'])
                self.assertEqual(attempt['response']['finish_reason'], 'stop')
                self.assertEqual(attempt['response']['returned_model'], 'qwen3.7-plus' if item['kind'] == 'query_spec' else 'qwen3.8-flash')
                self.assertNotIn('qianwen-fixture-key', json.dumps(response))
            self.assertNotIn(b'qianwen-fixture-key', self.service.download(job['id'], 'judgments.jsonl'))
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_zhipu_scores_with_custom_prompt_and_exposes_request_and_response(self):
        from test_evaluation_layer import measure
        from test_streaming import event
        records = []
        answer = {"dimensions": {"answerability": measure(3)}}
        class Provider(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                records.append((self.path, self.headers.get('Authorization'), body))
                raw = event(choices=[{"index": 0, "delta": {"reasoning_content": "private reasoning"}}])
                raw += event(json.dumps(answer), 'stop', model=body['model'],
                             usage={'prompt_tokens': 20, 'completion_tokens': 40, 'total_tokens': 60})
                raw += b'data: [DONE]\n\n'
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            config_path = self.project / 'examples/evaluation.zhipu.json'
            config = json.loads(config_path.read_text())
            profile = config['models']['zhipu']
            self.assertEqual(profile['base_url'], 'https://open.bigmodel.cn/api/paas/v4')
            self.assertEqual(profile['max_concurrency'], 1)
            profile.update(base_url=f'http://127.0.0.1:{server.server_port}/api/paas/v4', requests_per_second=1000)
            config_path.write_text(json.dumps(config))
            with patch.dict(os.environ, {'ZHIPU_API_KEY': ''}):
                self.assertFalse(next(m for m in self.service.catalog()['models'] if m['id'] == 'zhipu')['ready'])
                with self.assertRaises(InputError):
                    self.service.start(self.request(models=['zhipu'], query_understanding='direct'))
                self.assertEqual(self.service.jobs(), [])
            with (self.project / '.env').open('a') as env:
                env.write("\nexport ZHIPU_API_KEY='zhipu-fixture-key'\n")
            self.assertTrue(next(m for m in self.service.catalog()['models'] if m['id'] == 'zhipu')['ready'])
            with patch('quality_llm.models.get_json', side_effect=AssertionError('No model catalog HTTP request')):
                group = self.service.discover_models({'profiles': ['zhipu']})['groups'][0]
            self.assertEqual(group['source'], 'configured')
            self.assertEqual([m['id'] for m in group['models']], ['glm-4.7-flash'])
            self.assertEqual(records, [])
            version = self.service.prompt_store.save({"kind": "answerability", "query_understanding": "direct",
                "name": "GLM custom prompt", "rules": "CUSTOM GLM：根据 query 和结果证据评分，按 JSON 输出。"})
            job = self.service.start(self.request(models=['zhipu'], query_understanding='direct',
                scoring_model_ids={'zhipu': 'glm-4.7-flash'}, schemes=['answerability_only_v1'],
                prompt_versions={'answerability': version['id']}))
            detail = self.finish(job)
            self.assertEqual(detail['job']['status'], 'completed')
            self.assertFalse(detail['job']['simulated'])
            self.assertEqual(len(records), 2)
            rows = self.service.results(job['id'], {})['items']
            self.assertEqual(len(rows), 2)
            scores = [r['dimensions']['answerability'] for r in rows]
            self.assertTrue(all(r['judgment_status'] == 'rated' and r['value'] == 3 for r in scores))
            for path, auth, body in records:
                self.assertEqual(path, '/api/paas/v4/chat/completions')
                self.assertEqual(auth, 'Bearer zhipu-fixture-key')
                self.assertEqual(body['model'], 'glm-4.7-flash')
                self.assertEqual(body['thinking'], {'type': 'enabled'})
                self.assertEqual(body['response_format'], {'type': 'json_object'})
                self.assertEqual(body['max_tokens'], 16384)
                self.assertTrue(body['stream'])
                self.assertNotIn('stream_options', body)
                self.assertNotIn('max_completion_tokens', body)
                self.assertTrue(body['messages'][0]['content'].startswith(version['rules']))
                self.assertIn('Output JSON schema:', body['messages'][0]['content'])
            for item in self.service.prompt_tasks(job['id'])['items']:
                prompt = self.service.task_prompt(job['id'], item['id'])
                self.assertIn(prompt['wire_request']['body'], [r[2] for r in records])
                response = self.service.task_response(job['id'], item['id'])
                received = response['diagnostics']['attempts'][0]['response']
                self.assertEqual(json.loads(received['text']), answer)
                self.assertEqual(received['usage']['total_tokens'], 60)
                self.assertTrue(received['stream_info']['completed'])
                self.assertEqual(received['returned_model'], 'glm-4.7-flash')
                for secret in ('zhipu-fixture-key', 'private reasoning'):
                    self.assertNotIn(secret, json.dumps([prompt, response]))
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_discovered_id_reaches_query_parser_without_changing_scorer(self):
        catalog = {"endpoint": None, "source": "simulation", "models": [
            {"id": "another-parser-model", "name": "Parser", "selectable": True, "reason": ""},
        ]}
        with patch("frontend.service.list_models", return_value=catalog) as fetch:
            result = self.service.discover_models({"profiles": ["mock_a"]})
            self.assertEqual(result["groups"][0]["models"][0]["id"], "another-parser-model")
            job = self.service.start(self.request(query_model_id="another-parser-model"))
            fetch.assert_called_once()  # Starting can reuse the recently fetched list.
        detail = self.finish(job)
        self.assertEqual(detail["job"]["status"], "completed")
        self.assertEqual(detail["job"]["query_model_id"], "another-parser-model")
        root = self.service.root_for(job["id"])
        config = json.loads((root / "config.json").read_text())
        self.assertEqual(config["models"][config["query_model"]]["model"], "another-parser-model")
        self.assertEqual(config["models"]["mock_a"]["model"], "simulated-a")
        self.assertEqual({e["model"] for e in config["experiments"]}, {"mock_a"})
        specs = [json.loads(line) for line in (root / "specs/query_specs.jsonl").read_text().splitlines()]
        self.assertEqual({s["provenance"]["call"]["returned_model"] for s in specs}, {"another-parser-model"})
        self.assertEqual({s["provenance"]["model_profile"] for s in specs}, {config["query_model"]})

    def test_catalog_refresh_partial_failure_and_rejection_of_unlisted_model(self):
        def provider(profile, key):
            if profile["protocol"] == "mock":
                return {"endpoint": None, "source": "simulation", "models": [
                    {"id": profile["model"], "name": profile["model"], "selectable": True, "reason": ""}]}
            self.assertEqual(key, "frontend-fixture-secret")
            raise LLMError("models_http_error", "获取模型列表失败（HTTP 401）。")

        with patch("frontend.service.list_models", side_effect=provider) as fetch:
            first = self.service.discover_models({"profiles": ["mock_a", "deepseek"]})
            second = self.service.discover_models({"profiles": ["mock_a", "deepseek"]})
            self.assertEqual(fetch.call_count, 4)  # Explicit refresh always calls the provider.
        self.assertEqual(first, second)
        self.assertEqual([g["status"] for g in first["groups"]], ["ready", "error"])
        self.assertEqual(first["groups"][1]["models"], [])
        self.assertNotIn("frontend-fixture-secret", json.dumps(first))
        with self.assertRaises(InputError):
            self.service.start(self.request(query_model_id="not-returned-by-api"))
        self.assertEqual(self.service.jobs(), [])

    def test_disabled_query_parser_skips_discovery_credentials_and_preparation(self):
        # An unconfigured parser profile and stale ID must not be used in direct mode.
        with patch("frontend.service.list_models", side_effect=AssertionError("Must not discover parser models")):
            job = self.service.start(self.request(query_understanding="direct",
                                                  query_model="openai", query_model_id="stale"))
        detail = self.finish(job)
        self.assertEqual(detail["job"]["status"], "completed")
        self.assertEqual(detail["job"]["query_understanding"], "direct")
        self.assertIsNone(detail["job"]["query_model"])
        self.assertIsNone(detail["job"]["query_model_id"])
        self.assertEqual(detail["job"]["preparation"]["status"], "skipped")
        self.assertEqual(detail["preparation"], [])
        self.assertEqual(detail["records"], 14)
        root = self.service.root_for(job["id"])
        self.assertFalse((root / "specs").exists())
        config = json.loads((root / "config.json").read_text())
        self.assertEqual(set(config["models"]), {"mock_a"})
        self.assertEqual(config["query_understanding"], "direct")
        rows = self.service.results(job["id"], {})["items"]
        self.assertTrue(all(r["query_spec"] is None and r["query_understanding"] == "direct" for r in rows))
        exported = [json.loads(line) for line in self.service.download(job["id"], "judgments.jsonl").splitlines()]
        self.assertEqual({r["query_understanding"] for r in exported}, {"direct"})
        with self.assertRaises(FileNotFoundError):
            self.service.download(job["id"], "query_specs.jsonl")

    def test_selected_scoring_model_reaches_judge_and_keeps_parser_independent(self):
        def provider(profile, key):
            return {"endpoint": None, "source": "simulation", "models": [
                {"id": model_id, "name": model_id, "selectable": True, "reason": ""}
                for model_id in (profile["model"], "alternate-judge")]}
        for understanding in ("frozen", "direct"):
            with self.subTest(understanding=understanding):
                with patch("frontend.service.list_models", side_effect=provider):
                    self.service.discover_models({"profiles": ["mock_a"]})
                    job = self.service.start(self.request(
                        scoring_model_ids={"mock_a": "alternate-judge"},
                        query_model_id="simulated-a", query_understanding=understanding))
                detail = self.finish(job)
                self.assertEqual(detail["job"]["status"], "completed")
                self.assertEqual(detail["job"]["scoring_model_ids"], {"mock_a": "alternate-judge"})
                root = self.service.root_for(job["id"])
                config = json.loads((root / "config.json").read_text())
                self.assertEqual(config["models"]["mock_a"]["model"], "alternate-judge")
                self.assertEqual({e["model"] for e in config["experiments"]}, {"mock_a"})
                calls = [checked_read(p) for p in (root / "evaluation/tasks").glob("*.json")]
                self.assertTrue(any(c["call"] for c in calls))
                self.assertEqual({c["call"]["returned_model"] for c in calls if c["call"]}, {"alternate-judge"})
                self.assertEqual({c["model_id"] for c in calls}, {"alternate-judge"})
                if understanding == "frozen":
                    self.assertNotEqual(config["query_model"], "mock_a")
                    self.assertEqual(config["models"][config["query_model"]]["model"], "simulated-a")
                    specs = [json.loads(line) for line in (root / "specs/query_specs.jsonl").read_text().splitlines()]
                    self.assertEqual({s["provenance"]["call"]["returned_model"] for s in specs}, {"simulated-a"})
                else:
                    self.assertIsNone(config["query_model"])
                    self.assertFalse((root / "specs").exists())
                exported = [json.loads(line) for line in self.service.download(job["id"], "judgments.jsonl").splitlines()]
                self.assertEqual({r["model_id"] for r in exported}, {"alternate-judge"})
                results = self.service.results(job["id"], {"model": "mock_a"})
                self.assertEqual({r["model_id"] for r in results["items"]}, {"alternate-judge"})
                self.assertEqual({e["model_id"] for e in detail["summary"]["experiments"].values()}, {"alternate-judge"})
        self.assertEqual(self.service.profiles()["mock_a"]["model"], "simulated-a")

    def test_scoring_interfaces_keep_their_selected_model_ids_in_separate_runs(self):
        def provider(profile, key):
            model_id = profile["model"] + "-chosen"
            return {"endpoint": None, "source": "simulation", "models": [
                {"id": model_id, "name": model_id, "selectable": True, "reason": ""}]}
        selected = {"mock_a": "simulated-a-chosen", "mock_b": "simulated-b-chosen"}
        for name, model_id in selected.items():
            with patch("frontend.service.list_models", side_effect=provider) as fetch:
                job = self.service.start(self.request(models=[name],
                    scoring_model_ids={name: model_id}, query_understanding="direct"))
                self.assertEqual(fetch.call_count, 1)
            detail = self.finish(job)
            self.assertEqual(detail["job"]["status"], "completed")
            self.assertEqual(detail["job"]["models"], [name])
            items = self.service.results(job["id"], {"model": name})["items"]
            self.assertTrue(items)
            self.assertEqual({r["model_id"] for r in items}, {model_id})

    def test_scoring_model_must_be_single_before_discovery_or_worker_launch(self):
        with patch("frontend.service.list_models") as discover, patch("frontend.service.subprocess.Popen") as process:
            for names in ([], ["mock_a", "mock_b"], ["deepseek", "qianwen"], ["mock_a", "mock_a"]):
                with self.subTest(models=names), self.assertRaisesRegex(InputError, "只能选择一个评分模型"):
                    self.service.start(self.request(models=names))
            discover.assert_not_called()
            process.assert_not_called()
        self.assertEqual(self.service.jobs(), [])

    def test_invalid_scoring_model_selections_are_rejected_before_start(self):
        for selected in ({}, {"mock_a": ""}, {"mock_a": False}, {"mock_a": []},
                         {"mock_b": "simulated-b"}, {"mock_a": "invented"},
                         {"mock_a": "simulated-a", "mock_b": "simulated-b"}):
            with self.subTest(selected=selected), self.assertRaises(InputError):
                self.service.start(self.request(scoring_model_ids=selected, query_understanding="direct"))
        for catalog in (
            {"endpoint": None, "source": "api", "models": []},
            {"endpoint": None, "source": "api", "models": [
                {"id": "blocked", "name": "blocked", "selectable": False, "reason": "Not text generation"}]},
        ):
            with patch("frontend.service.list_models", return_value=catalog):
                self.service.discover_models({"profiles": ["mock_a"]})
                with self.assertRaises(InputError):
                    self.service.start(self.request(scoring_model_ids={"mock_a": "blocked"},
                                                    query_understanding="direct"))
        with patch("frontend.service.list_models", side_effect=LLMError("models_http_error", "HTTP 401")):
            self.service.discover_models({"profiles": ["mock_a"]})  # Failure invalidates the last catalog.
            with self.assertRaisesRegex(InputError, "401"):
                self.service.start(self.request(scoring_model_ids={"mock_a": "simulated-a"},
                                                query_understanding="direct"))
        self.assertEqual(self.service.jobs(), [])

    def test_literal_credentials_not_executed_or_returned_by_catalog(self):
        catalog = self.service.catalog()
        self.assertNotIn("frontend-fixture-secret", json.dumps(catalog))
        self.assertTrue(next(p for p in catalog["models"] if p["id"] == "deepseek")["ready"])
        path = self.project / ".env"
        path.write_text("export DEEPSEEK_API_KEY='$(touch should-never-exist)'\n")
        with self.assertRaises(InputError):
            read_credentials(path, {"DEEPSEEK_API_KEY"})
        self.assertFalse((self.project / "should-never-exist").exists())
        path.write_text("export DEEPSEEK_API_KEY='broken\n")
        with self.assertRaises(InputError):
            read_credentials(path, {"DEEPSEEK_API_KEY"})

    def test_cancellation_and_single_active_batch(self):
        real_popen = subprocess.Popen
        def sleeping(*args, **kwargs):
            return real_popen([sys.executable, "-c", "import time; time.sleep(30)"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        with patch("frontend.service.subprocess.Popen", side_effect=sleeping):
            job = self.service.start(self.request())
            with self.assertRaises(Conflict):
                self.service.start(self.request())
            with self.assertRaisesRegex(Conflict, "先停止"):
                self.service.delete(job["id"])
            cancelled = self.service.cancel(job["id"])
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(cancelled["finished_at"], cancelled["updated_at"])
        self.assertIsNotNone(self.service.processes[job["id"]].poll())
        self.assertEqual(self.service.detail(job["id"])["records"], 0)
        self.service.delete(job["id"])
        self.assertEqual(self.service.restore(job["id"])["status"], "cancelled")

    def test_unexpected_process_exit_and_interrupted_history(self):
        real_popen = subprocess.Popen
        with patch("frontend.service.subprocess.Popen",
                   side_effect=lambda *a, **k: real_popen([sys.executable, "-c", "raise SystemExit(7)"])):
            job = self.service.start(self.request())
        ended = self.finish(job)["job"]
        self.assertEqual(ended["status"], "failed")
        self.assertEqual(ended["finished_at"], ended["updated_at"])
        path = self.service.root_for(job["id"]) / "job.json"
        saved = json.loads(path.read_text())
        saved["status"] = "running"
        saved.pop("finished_at")
        path.write_text(json.dumps(saved))
        other = Service(self.project, self.data_root)
        self.assertEqual(other.jobs()[0]["status"], "interrupted")
        self.assertNotIn("finished_at", other.jobs()[0])
        self.assertEqual(other.jobs()[0]["updated_at"], saved["updated_at"])

    def test_preparation_failure_records_end_time_without_scoring(self):
        from frontend.worker import execute
        root = self.root / "prepare-failed"
        root.mkdir()
        (root / "job.json").write_text(json.dumps({
            "created_at": "2026-01-01T00:00:00Z", "dataset_path": str(self.data_root / "fixture"),
            "query_count": 2, "limit": None,
        }))
        with patch("frontend.worker.load_config", return_value={"query_understanding": "frozen"}), \
                patch("frontend.worker.prepare_specs", return_value={"ready_count": 0}), \
                patch("frontend.worker.run") as score:
            execute(root)
        saved = json.loads((root / "job.json").read_text())
        self.assertEqual(saved["status"], "partial")
        self.assertGreaterEqual(saved["finished_at"], saved["created_at"])
        score.assert_not_called()

    def test_native_jev_run_preserves_scores_probabilities_and_wire_request(self):
        received = []
        class JevHandler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def send_json(self, value):
                body = json.dumps(value).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                self.send_json({"data": [{"id": "ateve-jev-v1", "supported_protocols": ["typesafe:systemone"]}]})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                received.append(body)
                self.send_json({"model": "ateve-jev-v1", "answers": {"answerability": {
                    "type": "choice", "choice": "3", "confidence": 1,
                    "probabilities": {"-1": 0, "0": 0, "1": 0, "2": 0, "3": 1}}},
                    "usage": {"input_tokens": 100, "output_tokens": 20}})
        server = ThreadingHTTPServer(("127.0.0.1", 0), JevHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            path = self.project / "examples/evaluation.ateve-jev.json"
            config = json.loads(path.read_text())
            profile = config["models"]["ateve_jev"]
            profile["base_url"] = f"http://127.0.0.1:{server.server_port}/gateway/typesafe/v1"
            profile["models_url"] = f"http://127.0.0.1:{server.server_port}/gateway/v1/models"
            path.write_text(json.dumps(config))
            with (self.project / ".env").open("a") as handle:
                handle.write("export ATEVE_JEV_API_KEY='fixture-jev-key'\n")
            question = json.loads(prompt_for("answerability_jev"))
            question["instructions"] = "CUSTOM JEV: use only the provided result."
            version = self.service.prompt_store.save({"kind": "answerability_jev", "query_understanding": "direct",
                "name": "Jev native fixture", "rules": json.dumps(question)})
            job = self.service.start(self.request(models=["ateve_jev"], query_understanding="direct",
                                                 query_model=None, schemes=["answerability_only_v1"],
                                                 prompt_versions={"answerability_jev": version["id"]},
                                                 scoring_model_ids={"ateve_jev": "ateve-jev-v1"}))
            detail = self.finish(job)
            self.assertEqual(detail["job"]["status"], "completed")
            self.assertEqual(detail["statuses"], {"rated": 2})
            self.assertEqual(detail["answerability_distributions"][0]["rated"], 2)
            rows = self.service.results(job["id"], {})["items"]
            measurement = rows[0]["dimensions"]["answerability"]
            self.assertEqual(measurement["value"], 3)
            self.assertEqual(measurement["decision"]["probabilities"]["3"], 1)
            self.assertEqual(measurement["evidence"], [])
            self.assertEqual(len(received), 2)
            for body in received:
                self.assertNotIn("human_references", json.dumps(body))
                self.assertNotIn("reference_labels", json.dumps(body))
                self.assertEqual(set(body), {"model", "state", "questions"})
                self.assertEqual(body["questions"]["answerability"], question)
                self.assertNotIn("evidence_catalog", body["state"])
            prompt = self.service.task_prompt(job["id"], measurement["task_id"])
            self.assertEqual(prompt["wire_request"]["source"], "saved_request")
            self.assertIn(prompt["wire_request"]["body"], received)
            self.assertEqual(prompt["prompt_kind"], "answerability_jev")
            self.assertEqual(prompt["prompt_version"]["id"], version["id"])
            self.assertEqual(prompt["prompt_hash"], version["prompt_hash"])
            self.assertEqual(json.loads(prompt["rules"]), question)
            self.assertEqual(prompt["messages"], [])
            self.assertIsNone(prompt["schema"])
            with patch("frontend.service.prompt_for", side_effect=AssertionError("Do not rebuild history")):
                self.assertEqual(self.service.task_prompt(job["id"], measurement["task_id"]), prompt)
            response = self.service.task_response(job["id"], measurement["task_id"])
            self.assertEqual(response["availability"], "recorded")
            self.assertIn('"probabilities"', response["diagnostics"]["attempts"][0]["response"]["text"])
            self.assertNotIn("fixture-jev-key", json.dumps([prompt, response]))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_jev_prompt_preview_and_versions_are_isolated_from_generative_models(self):
        filters = {"model_profile": "ateve_jev", "query_understanding": "direct"}
        preview = self.service.prompt_templates(filters)["items"][0]
        self.assertEqual(preview["kind"], "answerability_jev")
        self.assertEqual(preview["wire_request"]["source"], "template")
        self.assertEqual(preview["wire_request"]["body"]["state"], {})
        self.assertEqual(preview["wire_request"]["body"]["questions"]["answerability"], json.loads(preview["rules"]))
        general = self.service.prompt_store.save({"kind": "answerability", "query_understanding": "direct",
            "name": "Other models", "rules": "CUSTOM GENERATIVE RULES"})
        native = self.service.prompt_store.save({"kind": "answerability_jev", "query_understanding": "direct",
            "name": "Jev only", "rules": preview["rules"]})
        self.assertEqual([v["id"] for v in self.service.prompt_templates(filters)["items"][0]["versions"]], [native["id"]])
        generic = self.service.prompt_templates({"model_profile": "deepseek", "query_understanding": "direct"})["items"][0]
        self.assertEqual([v["id"] for v in generic["versions"]], [general["id"]])
        self.assertEqual(generic["system"], prompt_for("answerability", "direct"))
        frozen = self.service.prompt_templates({**filters, "query_understanding": "frozen"})["items"]
        self.assertEqual([item["kind"] for item in frozen], ["query_spec", "answerability_jev"])
        with self.assertRaises(InputError):
            self.service.prompt_templates({**filters, "prompt_versions": json.dumps({"answerability": general["id"]})})
        with patch("frontend.service.subprocess.Popen") as launch, self.assertRaises(InputError):
            self.service.start(self.request(models=["ateve_jev"], query_understanding="direct",
                schemes=["answerability_only_v1"], prompt_versions={"answerability": general["id"]}))
        launch.assert_not_called()

    def test_native_jev_rejects_unsupported_scheme_and_query_parser_before_launch(self):
        with patch("frontend.service.subprocess.Popen") as launch:
            with self.assertRaisesRegex(InputError, "可回答性"):
                self.service.start(self.request(models=["ateve_jev"], query_understanding="direct"))
            with self.assertRaisesRegex(InputError, "不生成需求说明"):
                self.service.start(self.request(models=["ateve_jev"], query_model="ateve_jev",
                                                schemes=["answerability_only_v1"]))
            launch.assert_not_called()

    def test_http_ui_api_boundaries_and_post_roundtrip(self):
        server = make_server(self.service, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def call(method, path, body=None, headers=None):
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            connection.request(method, path, json.dumps(body) if body is not None else None, headers or {})
            response = connection.getresponse()
            value = response.read()
            connection.close()
            return response.status, value
        try:
            self.assertEqual(call("GET", "/")[0], 200)
            self.assertEqual(call("GET", "/timing.js")[0], 200)
            self.assertEqual(call("GET", "/.env")[0], 404)
            self.assertEqual(call("GET", "/../.env")[0], 404)
            self.assertEqual(call("GET", "/api/bootstrap", headers={"Host": "evil.example"})[0], 403)
            self.assertEqual(call("GET", "/api/bootstrap", headers={"Origin": "https://evil.example"})[0], 403)
            code, body = call("GET", "/api/bootstrap")
            self.assertEqual(code, 200)
            self.assertNotIn(b"frontend-fixture-secret", body)
            token = json.loads(body)["token"]
            version_body = {"kind": "answerability", "query_understanding": "direct",
                            "name": "HTTP 中文长规则", "rules": "评分规则。"*4000}
            self.assertEqual(call("POST", "/api/prompt-versions", version_body,
                                  {"Content-Type": "application/json"})[0], 403)
            code, body = call("POST", "/api/prompt-versions", version_body,
                             {"Content-Type": "application/json", "X-Eval-Token": token})
            self.assertEqual(code, 201)
            self.assertEqual(json.loads(body)["rules"], version_body["rules"])
            code, body = call("GET", "/api/prompts?schemes=legacy_five_v1&query_understanding=direct")
            self.assertEqual(code, 200)
            self.assertEqual(len(json.loads(body)["items"]), 2)
            self.assertEqual(call("GET", "/api/prompts?schemes=invalid")[0], 400)
            self.assertEqual(call("POST", "/api/jobs", self.request(), {"Content-Type": "application/json"})[0], 403)
            headers = {"Content-Type": "application/json", "X-Eval-Token": token}
            for endpoint in ("/api/datasets", "/api/datasets/browse", "/api/datasets/preview"):
                self.assertEqual(call("POST", endpoint, {"path": str(self.root)},
                                      {"Content-Type": "application/json"})[0], 403)
                self.assertEqual(call("POST", endpoint, {"path": str(self.root)},
                                      {**headers, "Origin": "https://evil.example"})[0], 403)
            code, body = call("POST", "/api/datasets/browse", {"path": str(self.root)}, headers)
            self.assertEqual(code, 200)
            self.assertIn("input.json", {e["name"] for e in json.loads(body)["items"]})
            code, body = call("POST", "/api/datasets", {"path": str(self.root / "input.json")}, headers)
            self.assertEqual(code, 200)
            self.assertEqual(json.loads(body)["status"], "ready")
            self.assertNotIn(b"frontend-fixture-secret", body)
            self.assertEqual(call("POST", "/api/models/discover", {"profiles": ["mock_a"]},
                                  {"Content-Type": "application/json"})[0], 403)
            code, body = call("POST", "/api/models/discover", {"profiles": ["mock_a"]}, headers)
            self.assertEqual(code, 200)
            self.assertEqual(json.loads(body)["groups"][0]["models"][0]["id"], "simulated-a")
            self.assertNotIn(b"frontend-fixture-secret", body)
            self.assertEqual(call("POST", "/api/jobs", self.request(),
                                 {**headers, "Origin": "https://evil.example"})[0], 403)
            code, body = call("POST", "/api/jobs", self.request(
                scoring_model_ids={"mock_a": "simulated-a"}, query_model_id="simulated-a"), headers)
            self.assertEqual(code, 201)
            job = json.loads(body)
            self.assertEqual(job["scoring_model_ids"], {"mock_a": "simulated-a"})
            self.finish(job)
            code, body = call("GET", f"/api/jobs/{job['id']}/prompts")
            self.assertEqual(code, 200)
            task_id = json.loads(body)["items"][0]["id"]
            code, body = call("GET", f"/api/jobs/{job['id']}/prompts/{task_id}")
            self.assertEqual(code, 200)
            self.assertTrue(json.loads(body)["available"])
            self.assertNotIn(b"frontend-fixture-secret", body)
            self.assertEqual(call("GET", f"/api/jobs/{job['id']}/prompts/invalid")[0], 400)
            code, body = call("GET", f"/api/jobs/{job['id']}/responses/{task_id}")
            self.assertEqual(code, 200)
            self.assertEqual(json.loads(body)["id"], task_id)
            self.assertNotIn(b"frontend-fixture-secret", body)
            self.assertEqual(call("GET", f"/api/jobs/{job['id']}/responses/invalid")[0], 400)
            self.assertEqual(call("GET", f"/api/jobs/{job['id']}/results?q=ExampleDB")[0], 200)
            code, body = call("GET", f"/api/jobs/{job['id']}/ranking")
            self.assertEqual(code, 200)
            self.assertEqual(json.loads(body)["version"], "answerability-ranking-v1")
            self.assertEqual(call("GET", f"/api/jobs/{job['id']}/download/ranking_metrics.json")[0], 200)
            self.assertEqual(call("GET", f"/api/jobs/{job['id']}/ranking-comparison?baseline={job['id']}")[0], 400)
            self.assertEqual(call("GET", f"/api/jobs/{job['id']}/download/summary.json")[0], 200)
            delete_url = f"/api/jobs/{job['id']}/delete"
            restore_url = f"/api/jobs/{job['id']}/restore"
            self.assertEqual(call("POST", delete_url, {}, {"Content-Type": "application/json"})[0], 403)
            self.assertEqual(call("POST", delete_url, {},
                                  {**headers, "Origin": "https://evil.example"})[0], 403)
            code, body = call("POST", "/api/datasets/preview", {"dataset": "fixture", "limit": 1}, headers)
            self.assertEqual(code, 200)
            self.assertEqual(json.loads(body)["selected"]["queries"], 1)
            self.assertEqual(json.loads(body)["selected"]["results"], 2)
            self.assertEqual(call("POST", "/api/datasets/preview",
                                  {"dataset": "fixture", "limit": 0}, headers)[0], 400)
            self.assertEqual(call("POST", "/api/datasets/preview",
                                  {"dataset": "missing"}, headers)[0], 400)
            self.assertEqual(call("POST", delete_url, {"path": "/tmp"}, headers)[0], 400)
            self.assertEqual(call("GET", delete_url)[0], 404)
            self.assertEqual(call("POST", delete_url, {}, headers)[0], 200)
            self.assertEqual(call("GET", f"/api/jobs/{job['id']}")[0], 404)
            self.assertEqual(call("GET", f"/api/jobs/{job['id']}/download/summary.json")[0], 404)
            self.assertEqual(json.loads(call("GET", "/api/jobs")[1])["jobs"], [])
            self.assertEqual(call("POST", restore_url, {}, {"Content-Type": "application/json"})[0], 403)
            self.assertEqual(call("POST", restore_url, {}, headers)[0], 200)
            self.assertEqual(call("GET", f"/api/jobs/{job['id']}")[0], 200)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
