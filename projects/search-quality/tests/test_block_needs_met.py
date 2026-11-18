import copy
import json
import os
import re
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from frontend.service import PROJECT, Service
from frontend.distributions import block_distributions
from quality_eval import block_needs_met as block
from quality_eval.block_prompt import SOURCES, assemble
from quality_eval.config import validate_config
from quality_eval.execution import execute_task
from quality_eval.runner import plan, run, flatten
from quality_eval.schemes import make_tasks, prompt_for
from quality_eval.storage import checked_read, checked_write
from quality_input.common import InputError, read_jsonl
from quality_input.pipeline import import_dataset, load_dataset
from quality_llm import LLMClient


def answer(payload, score=3):
    return {"skill_version": "2.1.1", "rating_mode": "block_content_needs_met",
            "evidence_scope": "provided_text_fields", "score_scale": block.GRADES,
            "results": [{**{k: payload[k] for k in ("id", "input_index", "query", "url")},
                         "status": "rated", "intent": "Synthetic test intent", "interpretation": "common",
                         "score": score, "rating": block.GRADES[str(score)], "confidence": "high",
                         "reason": "Synthetic transport fixture, not a semantic evaluation.",
                         "evidence": [{"field": "snippet", "text": "fixture text"}],
                         "limitations": ["Provided text fields only."], "needs_review": False}]}


def compact_answer(score=3):
    return {"score": score, "reason": "用于传输测试的固定说明，不代表实际内容评分。",
            "evidence": ["fixture text"]}


class BlockNeedsMetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "source.json"
        self.raw = [{"query": " fixture query ", "results": [
            {"title": "", "snippet": "fixture text", "url": "https://example.test/ a",
             "teacherScore": 999, "features": {"secret_label": 999}},
            {"title": "fixture title", "snippet": "fixture text", "url": "https://example.test/ a"}]},
            {"query": "fixture query", "results": [{"title": "fixture title", "snippet": "fixture text",
                                                        "url": "https://example.test/b"}]}]
        self.source.write_text(json.dumps(self.raw))
        self.dataset = self.root / "data/fixture"
        self.dataset.parent.mkdir()
        report = import_dataset(self.source, self.dataset)
        self.assertEqual(report["status"], "ready")
        self.config = validate_config({
            "query_understanding": "direct", "models": {"deepseek": {
                "protocol": "openai_chat", "model": "deepseek-flash", "base_url": "https://api.deepseek.com",
                "api_key_env": "TEST_BLOCK_KEY", "requests_per_second": 1000, "max_concurrency": 4,
                "output_mode": "json_object", "token_limit_field": "max_tokens"}},
            "experiments": [{"id": "block", "scheme": block.SCHEME, "model": "deepseek"}]})
        self.data = load_dataset(self.dataset)
        self.tasks = make_tasks(self.data, self.config, {}, [q["query_id"] for q in self.data["queries"]])

    def tearDown(self):
        self.temp.cleanup()

    def test_import_keeps_order_duplicates_empty_and_raw_bytes(self):
        self.assertEqual((self.dataset / "raw/source.json").read_bytes(), self.source.read_bytes())
        self.assertEqual([t["payload"]["input_index"] for t in self.tasks], [0, 1, 2])
        self.assertEqual(self.tasks[0]["payload"]["query"], " fixture query ")
        self.assertEqual(self.tasks[0]["payload"]["title"], "")
        self.assertEqual(self.tasks[0]["payload"]["url"], self.tasks[1]["payload"]["url"])
        self.assertNotEqual(self.tasks[0]["task_id"], self.tasks[1]["task_id"])
        for t in self.tasks:
            self.assertEqual(set(t["payload"]), {"id", "input_index", "query", "title", "snippet", "url"})
            self.assertNotIn("secret_label", json.dumps(t["payload"]))
        self.assertEqual(self.tasks[2]["source_ref"]["pointer"], "/1/results/0")

    def test_full_skill_and_conditions_loaded(self):
        system = prompt_for(block.KIND, "direct")
        main = (block.ROOT / "SKILL.md").read_text()
        grades = main.split("## 3. 五档完整标准\n", 1)[1].split("## 4.", 1)[0]
        grades = grades.replace("[rubric.md](references/rubric.md)", "第4节的四字段与不确定性规则").replace(
            "[context-rules.md](references/context-rules.md)", "第4节的上下文规则")
        # Independently check original clauses, including exceptions, after
        # regrouping. No compiler audit is trusted as proof of preservation.
        for line in grades.splitlines():
            if line.strip():
                self.assertIn(line, system)
        context = (block.ROOT / "references/context-rules.md").read_text()
        for line in context.splitlines():
            if line and not line.startswith(("# ", "按问题阅读")):
                self.assertIn(line.lstrip("# "), system)
        examples = (block.ROOT / "references/io-and-examples.md").read_text().split("## 应用示例", 1)[1]
        for line in examples.splitlines():
            if line.startswith("| "):
                self.assertIn(line.replace("insufficient_evidence", "`score: null`"), system)
        self.assertEqual(len(block.bundle_manifest()["files"]), 5)
        self.assertEqual(block.output_schema()["properties"]["results"]["maxItems"], 1)

    def test_source_sections_accounted_and_no_file_navigation(self):
        compiled = assemble(block.ROOT)
        expected = {(name, heading) for name in SOURCES for heading in
                    ["intro", *re.findall(r"^## (.+)$", (block.ROOT / name).read_text(), re.M)]}
        self.assertEqual({(r["source"], r["heading"]) for r in compiled["coverage"]}, expected)
        for item in compiled["coverage"]:
            self.assertIn(item["rendered_text"], compiled["text"])
        self.assertNotRegex(compiled["text"], r"references/|SKILL\.md|按需读取")
        self.assertIn("reason 用简明中文", compiled["text"])
        self.assertNotIn("`needs_review`", compiled["text"])
        # An added rule section cannot silently disappear from the API prompt.
        copied = self.root / "skill"
        for name in SOURCES:
            target = copied / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text((block.ROOT / name).read_text())
        with (copied / "SKILL.md").open("a") as f:
            f.write("\n## 新增条件\n需要维护者纳入提示词。\n")
        with self.assertRaisesRegex(ValueError, "Unclassified"):
            assemble(copied)

    def test_frozen_override_does_not_recompile_existing_tasks(self):
        frozen = copy.deepcopy(self.config)
        frozen["prompt_overrides"] = {block.KIND: "Historical frozen rules, retained verbatim."}
        ids = [q["query_id"] for q in self.data["queries"]]
        before = make_tasks(self.data, frozen, {}, ids)
        with patch.object(block, "rules", side_effect=AssertionError("must use frozen rules")):
            after = make_tasks(self.data, frozen, {}, ids)
        self.assertEqual(before, after)
        self.assertIn("Historical frozen rules, retained verbatim.", after[0]["system"])

    def test_example_and_frontend_preview_use_current_assembly(self):
        example = json.loads((PROJECT / "examples/evaluation.needs-met.deepseek-flash.json").read_text())
        self.assertEqual(example["prompt_overrides"][block.KIND], block.rules())
        service = Service(PROJECT, self.dataset.parent, self.root / "preview-runs")
        try:
            item = service.prompt_templates({"schemes": block.SCHEME, "query_understanding": "direct"})["items"][0]
            self.assertEqual(item["system"], prompt_for(block.KIND, "direct"))
            self.assertEqual(item["prompt_version"]["assembly_version"], block.PROMPT_VERSION)
        finally:
            service.close()

    def test_native_validation_all_five_grades_and_abstention(self):
        payload = self.tasks[0]["payload"]
        for score in range(5):
            result = block.validate(answer(payload, score), payload)
            self.assertEqual(result["judgment"]["dimensions"][block.DIMENSION]["value"], score)
        a = answer(payload)
        a["results"][0].update(status="insufficient_evidence", score=None, rating=None, confidence=None,
                               evidence=[], needs_review=True)
        result = block.validate(a, payload)
        self.assertEqual(result["judgment"]["dimensions"][block.DIMENSION]["status"], "unjudgeable")

    def test_compact_grades_abstention_and_program_association(self):
        payload = self.tasks[1]["payload"]
        for value in range(5):
            result = block.validate(compact_answer(value), payload, block.COMPACT_CONTRACT)
            item = result["skill_output"]["results"][0]
            self.assertEqual(item["rating"], block.GRADES[str(value)])
            for key in ("id", "input_index", "query", "url"):
                self.assertEqual(item[key], payload[key])
            for key in ("intent", "confidence", "needs_review", "interpretation", "limitations"):
                self.assertNotIn(key, item)
            self.assertEqual(item["evidence"], [{"field": "snippet", "text": "fixture text"}])
        raw = {"score": None, "reason": "抽取缺失，无法判断块内容。", "evidence": []}
        result = block.validate(raw, payload, block.COMPACT_CONTRACT)
        self.assertEqual(result["judgment"]["dimensions"][block.DIMENSION]["status"], "unjudgeable")
        self.assertIsNone(result["skill_output"]["results"][0]["rating"])

    def test_compact_rejects_bad_grades_and_nonliteral_evidence(self):
        for change in ({"score": True}, {"score": 3.0}, {"score": -1}, {"score": 5}, {"score": "HM"},
                       {"evidence": []}, {"evidence": [""]}, {"evidence": ["   "]},
                       {"evidence": ["invented evidence"]}, {"evidence": ["fixture text"] * 4},
                       {"evidence": [{"field": "snippet", "text": "fixture text"}]},
                       {"reason": ""}, {"id": "not-required"}):
            with self.subTest(change=change), self.assertRaises(InputError):
                block.validate({**compact_answer(), **change}, self.tasks[0]["payload"], block.COMPACT_CONTRACT)
        for missing in ("score", "reason", "evidence"):
            raw = compact_answer();del raw[missing]
            with self.assertRaises(InputError):
                block.validate(raw, self.tasks[0]["payload"], block.COMPACT_CONTRACT)

    def test_legacy_and_compact_contracts_follow_frozen_config(self):
        ids = [q["query_id"] for q in self.data["queries"]]
        historical = copy.deepcopy(self.config)
        historical["prompt_overrides"] = {block.KIND: "Historical full output instructions."}
        old_task = make_tasks(self.data, historical, {}, ids)[0]
        self.assertNotIn("block_output_contract", old_task)
        self.assertIn('"results"', old_task["system"])
        task = self.tasks[0]
        self.assertEqual(task["block_output_contract"], block.COMPACT_CONTRACT)
        self.assertEqual(set(json.loads(task["system"].split("Output JSON schema:\n")[1])["properties"]),
                         {"score", "reason", "evidence"})
        class Client:
            profile = {"protocol": "openai_chat"}
            def complete(self, system, payload, schema, mock_response):
                self.schema = schema
                return {"text": json.dumps(answer(payload) if "results" in schema["properties"] else compact_answer())}
        client = Client()
        self.assertEqual(execute_task(old_task, client, 100000, [])["status"], "success")
        self.assertIn("results", client.schema["properties"])
        self.assertEqual(execute_task(task, client, 100000, [])["status"], "success")
        self.assertEqual(set(client.schema["properties"]), {"score", "reason", "evidence"})
        for invalid in (None, "unknown", True):
            with self.assertRaises(InputError):
                validate_config({**self.config, "block_output_contract": invalid})

    def test_reject_mismatched_identity_labels_citations_and_low_confidence(self):
        payload = self.tasks[0]["payload"]
        for update in ({"score": 4}, {"score": True}, {"rating": "HM+"}, {"input_index": 99},
                       {"query": "fixture query"}, {"id": "wrong"}, {"url": "https://other.test"},
                       {"confidence": "low"}, {"evidence": []},
                       {"evidence": [{"field": "title", "text": "fixture text"}]},
                       {"status": "insufficient_evidence", "needs_review": True}):
            a = answer(payload); a["results"][0].update(update)
            with self.subTest(update=update), self.assertRaises(InputError):
                block.validate(a, payload)
        a = answer(payload); a["results"].append(copy.deepcopy(a["results"][0]))
        with self.assertRaises(InputError): block.validate(a, payload)

    def test_transport_checkpoint_resume_and_native_export(self):
        calls = []
        def transport(url, headers, body, timeout):
            self.assertTrue(url.endswith("/chat/completions"))
            self.assertEqual(body["response_format"], {"type": "json_object"})
            self.assertEqual(body["model"], "deepseek-flash")
            p = json.loads(body["messages"][1]["content"]); calls.append(p["input_index"])
            return {"model": "deepseek-flash", "choices": [{"finish_reason": "stop", "message": {
                "content": json.dumps(compact_answer())}}], "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
        factory = lambda p: LLMClient(p, transport=transport)
        output = self.root / "run"
        with patch.dict(os.environ, {"TEST_BLOCK_KEY": "offline-fixture"}):
            summary = run(self.dataset, self.config, None, output, client_factory=factory)
            self.assertEqual(summary["status"], "completed")
            self.assertEqual(sorted(calls), [0, 1, 2])
            (output / "tasks" / (self.tasks[1]["task_id"] + ".json")).unlink()
            calls.clear()
            run(self.dataset, self.config, None, output, resume=True, client_factory=factory)
            self.assertEqual(calls, [1])
            changed = copy.deepcopy(self.config); changed["models"]["deepseek"]["max_output_tokens"] += 1
            with self.assertRaises(InputError):
                run(self.dataset, changed, None, output, resume=True, client_factory=factory)
        saved = checked_read(output / "tasks" / (self.tasks[0]["task_id"] + ".json"))
        self.assertNotIn("system", saved["request"])
        self.assertEqual(len(list((output / "prompts").glob("*.json"))), 1)
        exported = [r for _, r in read_jsonl(output / "block_content_needs_met.jsonl")]
        self.assertEqual([r["input_index"] for r in exported], [0, 1, 2])
        self.assertEqual(exported[0]["skill_output"]["results"][0]["rating"], "HM")

    def test_preflight_and_overlong_never_call_transport(self):
        with patch("quality_llm.client.http_transport", side_effect=AssertionError("network forbidden")):
            self.assertEqual(plan(self.dataset, self.config)["network_calls_made"], 0)
            service = Service(PROJECT, self.dataset.parent, self.root / "ui-runs")
            with patch.object(service, "_discover_profile", side_effect=AssertionError("discovery forbidden")):
                report = service.start({"dataset": "fixture", "models": ["deepseek"],
                                        "scoring_model_ids": {"deepseek": "deepseek-flash"},
                                        "schemes": [block.SCHEME], "query_understanding": "direct"}, dry_run=True)
            self.assertEqual(report["network_calls_made"], 0)
            self.assertFalse(report["job_created"])
            self.assertEqual(service.jobs(), [])
            self.assertEqual(report["runtime"]["max_workers"], 32)
            self.assertEqual(report["models"]["deepseek"]["max_concurrency"], 32)
            self.assertEqual(report["models"]["deepseek"]["requests_per_second"], 16)
            self.assertEqual(report["models"]["deepseek"]["max_output_tokens"], 16384)
            task = copy.deepcopy(self.tasks[0]); task["payload"]["title"] = "x" * 110000
            outcome = execute_task(task, LLMClient(self.config["models"]["deepseek"]), 100000, [])
            self.assertEqual(outcome["status"], "input_too_large")
            self.assertIsNone(flatten([outcome])[0]["value"])
            group = block_distributions(flatten([outcome]),
                {"schemes": [], "models": ["deepseek"], "repeats": 1}, 1)[0]
            self.assertEqual(group["excluded"], {"input_too_large": 1})

    def test_invalid_response_not_repaired_or_scored_zero(self):
        class Client:
            profile = {"protocol": "openai_chat"}
            def complete(self, system, payload, schema, mock_response):
                a = compact_answer(); a["evidence"][0] = "invented evidence"
                return {"text": json.dumps(a)}
        outcome = execute_task(self.tasks[0], Client(), 100000, [])
        self.assertEqual(outcome["status"], "invalid_output")
        row = flatten([outcome])[0]
        self.assertIsNone(row["value"])
        self.assertEqual(row["judgment_status"], "error")
        dist = block_distributions([row], {"schemes": [block.SCHEME], "models": ["deepseek"], "repeats": 1}, 3)
        self.assertEqual(sum(g["rated"] for g in dist), 0)

    def test_modes_and_jev_cannot_bypass_skill_boundary(self):
        for key, value in (("query_understanding", "frozen"), ("mode", "page_content")):
            config = copy.deepcopy(self.config)
            if key == "mode": config["experiments"][0][key] = value
            else: config[key] = value
            with self.assertRaises(InputError): validate_config(config)

    def test_mock_worker_frontend_prompt_export_and_resume(self):
        service = Service(PROJECT, self.dataset.parent, self.root / "worker-runs")
        try:
            job = service.start({"dataset": "fixture", "models": ["mock_a"], "schemes": [block.SCHEME],
                                 "query_understanding": "direct"})
            service.processes[job["id"]].wait(timeout=15)
            root = service.root_for(job["id"])
            detail = service.detail(job["id"])
            self.assertEqual(detail["job"]["status"], "completed")
            self.assertEqual(detail["job"]["prompt_versions"][block.KIND]["assembly_version"], block.PROMPT_VERSION)
            self.assertEqual(detail["block_needs_met_distributions"][0]["rated"], 0)
            saved_paths = list((root / "evaluation/tasks").glob("*.json"))
            self.assertEqual(len(saved_paths), 3)
            task = checked_read(saved_paths[0])
            snapshot = service._task_snapshot(job["id"], task["task_id"])
            self.assertIn("Fully Meets", snapshot["request"]["system"])
            self.assertEqual(len(service.download(job["id"], "block_content_needs_met.jsonl").splitlines()), 3)
            saved_paths[0].unlink()
            # Emulate a process exit before its last checkpoint. No existing judgment is rerun.
            saved_job = json.loads((root / "job.json").read_text())
            saved_job["status"] = "interrupted"
            (root / "job.json").write_text(json.dumps(saved_job))
            service.resume(job["id"])
            service.processes[job["id"]].wait(timeout=15)
            summary = service.detail(job["id"])["summary"]
            self.assertEqual(next(iter(summary["experiments"].values()))["cache_hits"], 2)
        finally:
            service.close()

    def test_explicit_failure_resume_keeps_successful_checkpoints(self):
        service = Service(PROJECT, self.dataset.parent, self.root / "retry-worker-runs")
        try:
            job = service.start({"dataset": "fixture", "models": ["mock_a"], "schemes": [block.SCHEME],
                                 "query_understanding": "direct"})
            service.processes[job["id"]].wait(timeout=15)
            root = service.root_for(job["id"])
            paths = list((root / "evaluation/tasks").glob("*.json"))
            kept = {p: p.read_bytes() for p in paths[1:]}
            failed = checked_read(paths[0])
            failed.update(status="error", judgment=None, error_code="transport_error")
            checked_write(paths[0], failed)
            saved_job = json.loads((root / "job.json").read_text())
            saved_job["status"] = "partial"
            (root / "job.json").write_text(json.dumps(saved_job))
            with self.assertRaises(InputError):
                service.resume(job["id"], retry_failed="yes")
            config_bytes = (root / "config.json").read_bytes()
            service.resume(job["id"], retry_failed=True)
            service.processes[job["id"]].wait(timeout=15)
            self.assertEqual(checked_read(paths[0])["sampling_attempt"], 2)
            self.assertEqual(checked_read(paths[0])["status"], "success")
            self.assertEqual((root / "config.json").read_bytes(), config_bytes)
            archives = list((root / "previous_reports").glob("resume-*"))
            self.assertEqual(len(archives), 1)
            self.assertTrue((archives[0] / "summary.json").is_file())
            self.assertTrue((archives[0] / "evaluation/judgments.jsonl").is_file())
            for path, contents in kept.items():
                self.assertEqual(path.read_bytes(), contents)
        finally:
            service.close()

