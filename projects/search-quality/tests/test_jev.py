import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from quality_eval.config import load_config
from quality_eval.contracts import schema_for, validate_answer
from quality_eval.jev import validate_jev_answer
from quality_eval.schemes import make_tasks, prompt_for, validate_prompt_overrides
from quality_input.common import InputError
from quality_llm.client import LLMClient, LLMError, validate_profile
from quality_llm.models import list_models
from quality_llm.protocols import build_request
from quality_llm.systemone import parse_choice_prompt
from quality_input.common import digest


PROJECT = Path(__file__).resolve().parents[1]
PROFILE = load_config(PROJECT / "examples/evaluation.ateve-jev.json")["models"]["ateve_jev"]


def native(grade="3"):
    return {"model": "ateve-jev-v1", "id": "native-response", "answers": {
        "answerability": {"type": "choice", "choice": grade, "confidence": 0.875,
                          "probabilities": {str(i): 0.9 if str(i) == grade else 0.025 for i in (-1, 0, 1, 2, 3)}}},
        "usage": {"input_tokens": 200, "output_tokens": 25}}


class JevTests(unittest.TestCase):
    def test_native_prompt_sends_explicit_criteria_without_generative_requirements(self):
        system = prompt_for("answerability_jev", "direct")
        question = parse_choice_prompt(system)
        payload = {"query": {"query_text": "Who wrote the novel?"}, "result": {"title": "Author"}}
        _, _, body = build_request(PROFILE, system, payload, schema_for("answerability"), "fixture-key")
        self.assertEqual(body["questions"]["answerability"], question)
        self.assertEqual(body["state"], payload)
        for forbidden in ("Output JSON schema:", "evidence_catalog", "只返回 schema", "【本次原生 Choice 调用约定】"):
            self.assertNotIn(forbidden, system)
        self.assertIn("完整正确答案", question["criteria"]["2"])
        for definition in question["criteria"].values():
            self.assertGreater(len(definition), 30)

    def test_prompt_validation_rejects_missing_duplicate_or_changed_labels(self):
        question = json.loads(prompt_for("answerability_jev"))
        bad = ["not JSON", '{"type":"choice","type":"choice","instructions":"x","criteria":{}}']
        for change in (lambda q: q["criteria"].pop("2"), lambda q: q["criteria"].update({"4": "extra"}),
                       lambda q: q["criteria"].update({"0": " "}), lambda q: q.update(type="score")):
            q = copy.deepcopy(question)
            change(q)
            bad.append(json.dumps(q))
        for rules in bad:
            with self.subTest(rules=rules), self.assertRaises(InputError):
                validate_prompt_overrides({"answerability_jev": rules})

    def test_mixed_models_use_separate_prompts_and_native_criteria_change_task_identity(self):
        config = load_config(PROJECT / "examples/evaluation.ateve-jev.json")
        config["models"]["generator"] = validate_profile({"protocol": "openai_chat", "model": "fixture", "api_key_env": "FIXTURE_KEY"})
        config["experiments"].append({**config["experiments"][0], "id": "generative", "model": "generator"})
        data = {"queries": [{"query_id": "q", "query_text": "Question"}], "page_snapshots": [],
                "manifest": {"evaluation_as_of": None, "dataset_version": "fixture"},
                "responses": [{"query_id": "q", "search_run_id": "s", "response_id": "r", "status": "ok",
                               "results": [{"result_id": "u", "rank": 1, "url": "https://example.com", "snippet": "answer"}]}]}
        original = make_tasks(data, config, {}, ["q"])
        native, generated = original
        self.assertEqual(native["prompt_kind"], "answerability_jev")
        self.assertNotIn("evidence_catalog", native["payload"])
        self.assertIn("evidence_catalog", generated["payload"])
        self.assertEqual(generated["system"], prompt_for("answerability", "direct"))
        q = json.loads(native["system"])
        q["criteria"]["1"] += " Boundary adjustment."
        config["prompt_overrides"] = {"answerability_jev": json.dumps(q, ensure_ascii=False)}
        changed = make_tasks(data, config, {}, ["q"])
        self.assertNotEqual(native["task_id"], changed[0]["task_id"])
        self.assertEqual(changed[0]["prompt_hash"], digest(changed[0]["system"]))
        self.assertEqual(generated["task_id"], changed[1]["task_id"])

    def test_actual_native_request_uses_current_rubric_and_only_supplied_payload(self):
        payload = {"query": {"text": "What is the capital of France?"},
                   "result": {"title": "France", "snippet": "Paris is the capital of France."}}
        system = prompt_for("answerability", "direct", {"answerability": "Custom rubric: 3 means fully answered."})
        endpoint, headers, body = build_request(PROFILE, system, payload, schema_for("answerability"), "fixture-key")
        self.assertEqual(endpoint, "https://tokendance.space/gateway/typesafe/v1/systemone")
        self.assertEqual(headers["Authorization"], "Bearer fixture-key")
        self.assertEqual(set(body), {"model", "state", "questions"})
        self.assertEqual(body["state"], payload)
        self.assertEqual(body["model"], "ateve-jev-v1")
        question = body["questions"]["answerability"]
        self.assertIn("Custom rubric", question["instructions"])
        self.assertNotIn("Output JSON schema:", question["instructions"])
        self.assertEqual(set(question["criteria"]), {"-1", "0", "1", "2", "3"})
        self.assertEqual(question["type"], "choice")
        self.assertNotIn("fixture-key", json.dumps(body))

    def test_all_grades_keep_probabilities_without_fabricating_citations(self):
        for grade in ("-1", "0", "1", "2", "3"):
            with self.subTest(grade=grade):
                result = validate_jev_answer(native(grade), "answerability")
                item = result["judgment"]["dimensions"]["answerability"]
                self.assertEqual(item["value"], int(grade))
                self.assertEqual(item["evidence"], [])
                self.assertEqual(item["decision"], native(grade)["answers"]["answerability"])
                self.assertEqual(result["validation_version"], "systemone-choice-v1")
        # Existing generative judges still need evidence for a positive grade.
        with self.assertRaisesRegex(InputError, "requires result/page evidence"):
            validate_answer("answerability", {"dimensions": {"answerability": {
                "status": "rated", "value": 3, "reason": "unsupported", "evidence": []}}}, {})

    def test_invalid_native_decisions_are_rejected(self):
        mutations = [
            lambda d: d.update(choice="4"), lambda d: d.update(choice=3),
            lambda d: d.update(choice="0"), lambda d: d.update(type="score"),
            lambda d: d.update(confidence=True), lambda d: d.update(confidence=-0.1),
            lambda d: d.update(probabilities={"3": 1}),
            lambda d: d["probabilities"].update({"3": 0.8}),
            lambda d: d["probabilities"].update({"0": float("nan")}),
        ]
        for change in mutations:
            value = native()
            change(value["answers"]["answerability"])
            with self.assertRaises(InputError):
                validate_jev_answer(value, "answerability")
        with self.assertRaises(InputError):
            validate_jev_answer({"answers": {}}, "answerability")
        with self.assertRaises(InputError):
            validate_jev_answer(native(), "google_nm")

    def test_client_preserves_native_response_and_actual_request_without_secret(self):
        def transport(url, headers, body, timeout):
            self.assertNotIn("messages", body)
            response = native()
            response["internal_reasoning"] = "never-save-private-reasoning"
            return response
        client = LLMClient(PROFILE, transport)
        with patch.dict(os.environ, {"ATEVE_JEV_API_KEY": "fixture-key"}):
            call = client.complete(prompt_for("answerability"), {"result": {}, "query": {}}, schema_for("answerability"))
        self.assertEqual(json.loads(call["text"]), native())
        self.assertEqual(call["diagnostics"]["wire_request"]["body"]["model"], "ateve-jev-v1")
        self.assertIsNone(call["diagnostics"]["max_output_tokens"])
        self.assertNotIn("fixture-key", json.dumps(call))
        self.assertNotIn("never-save", json.dumps(call))
        self.assertEqual(call["usage"]["output_tokens"], 25)

    def test_missing_native_answer_retains_failure_diagnostics(self):
        client = LLMClient(PROFILE, lambda *args: {"model": "ateve-jev-v1", "error": {"code": "bad_state", "message": "fixture-key"}})
        with patch.dict(os.environ, {"ATEVE_JEV_API_KEY": "fixture-key"}):
            with self.assertRaises(LLMError) as raised:
                client.complete(prompt_for("answerability"), {}, schema_for("answerability"))
        self.assertEqual(raised.exception.attempts, 1)
        self.assertNotIn("fixture-key", json.dumps(raised.exception.diagnostics))
        self.assertIn("bad_state", json.dumps(raised.exception.diagnostics))

    def test_catalog_uses_gateway_route_and_filters_native_protocol(self):
        def transport(url, headers, timeout):
            self.assertEqual(url, "https://tokendance.space/gateway/v1/models")
            return {"data": [
                {"id": "ateve-jev-v1", "supported_protocols": ["typesafe:systemone"]},
                {"id": "bocha-jev-v1", "supported_protocols": ["typesafe:systemone"]},
                {"id": "chat-only", "supported_protocols": ["openai:chat"]}]}
        found = list_models(PROFILE, "fixture-key", transport)
        self.assertEqual([m["id"] for m in found["models"]], ["ateve-jev-v1", "bocha-jev-v1"])
        with self.assertRaises(InputError):
            validate_profile({**PROFILE, "models_url": "https://other.example/models"})
        with self.assertRaises(InputError):
            validate_profile({**PROFILE, "parameters": {"temperature": 0}})

    def test_native_configuration_rejects_unsupported_tasks_before_network(self):
        raw = json.loads((PROJECT / "examples/evaluation.ateve-jev.json").read_text())
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            changed = copy.deepcopy(raw)
            changed["experiments"][0]["scheme"] = "legacy_five_v1"
            path.write_text(json.dumps(changed))
            with self.assertRaises(InputError):
                load_config(path)
            changed = copy.deepcopy(raw)
            changed.update(query_model="ateve_jev", query_understanding="frozen")
            path.write_text(json.dumps(changed))
            with self.assertRaises(InputError):
                load_config(path)
