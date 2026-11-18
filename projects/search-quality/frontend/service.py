import copy
import json
import os
import re
import secrets
import shlex
import subprocess
import sys
import threading
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from quality_eval.config import load_config, validate_config
from quality_eval.calibration import human_references, compare_with_human, valid_grade
from quality_eval.query_specs import select_queries
from quality_eval.runner import flatten, plan
from quality_eval import block_needs_met as block
from quality_eval.schemes import SCHEMES, page_evidence, prompt_for, prompt_kind
from quality_eval.contracts import schema_for
from quality_eval.storage import atomic_json, checked_read
from quality_input.common import InputError, canonical, digest, read_json, read_jsonl
from quality_input.pipeline import load_dataset, utc_now
from quality_llm import LLMError
from quality_llm.models import list_models
from quality_llm.client import validate_profile
from quality_llm.protocols import build_request
from quality_llm.systemone import parse_choice_prompt
from quality_metrics.ranking import VERSION as RANKING_VERSION, build_rankings, compare_groups, summary as ranking_summary
from .diagnostics import failure_info
from quality_eval.recovery import DEFAULT_POLICY, recovery_summary
from .datasets import DatasetStore
from .distributions import answerability_distributions, block_distributions
from .prompts import PromptStore


PROJECT = Path(__file__).resolve().parent.parent
ACTIVE = {"queued", "running"}
DIMENSION_LABELS = {
    block.DIMENSION: "Block Content Needs Met",
    "answerability": "可回答性", "authoritative": "权威性", "freshness": "时效性",
    "noise": "噪声", "duplicate": "重复性", "needs_met": "需求满足度", "page_quality": "页面质量",
}
SCHEME_LABELS = {"answerability_only_v1": "可回答性 · 人工校准", "legacy_five_v1": "旧体系 · 五维评估", "google_overview_v1": "Google · NM / PQ"}
SCHEME_LABELS[block.SCHEME] = "Block Content Needs Met · skill 2.1.1"
PROMPT_LABELS = {
    block.KIND: "Block Content Needs Met · 完整 skill 2.1.1",
    "answerability": "可回答性 · 单维评分",
    "answerability_jev": "Jev · 可回答性 v1",
    "query_spec": "Query 需求解析",
    "legacy_url": "旧体系 · 可回答性 / 权威性 / 时效性 / 噪声",
    "duplicates": "旧体系 · 重复性",
    "google_nm": "Google · 需求满足度",
    "google_pq": "Google · 页面质量",
}


def prompt_content(system, payload=None):
    """Keep exact messages for copying; split the embedded schema only for display."""
    if system.lstrip().startswith("{"):
        try:
            question = parse_choice_prompt(system)
        except ValueError:
            pass
        else:
            question = {key: question[key] for key in ("type", "instructions", "criteria")}
            return {"system": system, "rules": json.dumps(question, ensure_ascii=False, indent=2),
                    "schema": None, "payload": payload,
                    "user": canonical(payload) if payload is not None else None,
                    "messages": [], "native_question": question}
    rules, separator, raw_schema = system.rpartition("\nOutput JSON schema:\n")
    try:
        schema = json.loads(raw_schema) if separator else None
    except ValueError:
        schema = None
    user = canonical(payload) if payload is not None else None
    messages = [{"role": "system", "content": system}]
    if user is not None:
        messages.append({"role": "user", "content": user})
    return {"system": system, "rules": rules if schema is not None else system,
            "schema": schema, "payload": payload,
            "user": user, "messages": messages}


class Conflict(InputError):
    pass


def read_credentials(path, names):
    """Parse only literal key assignments; never execute .env as a shell script."""
    values = {name: os.environ[name] for name in names if os.environ.get(name)}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            match = re.fullmatch(r"\s*(?:export\s+)?([A-Za-z_]\w*)\s*=(.*)", line)
            if not match or match[1] not in names:
                continue
            try:
                parts = shlex.split(match[2], comments=True)
            except ValueError:
                raise InputError(".env 中的 API Key 引号格式有误。") from None
            if len(parts) > 1:
                raise InputError(".env 中的 API Key 必须是单个字符串。")
            if parts and parts[0].strip():
                if "$" in parts[0] or "`" in parts[0]:
                    raise InputError(".env 仅支持直接填写密钥，不支持变量或命令替换。")
                values[match[1]] = parts[0]
    return values


def json_rows(path):
    return [row for _, row in read_jsonl(path)] if path.is_file() else []


class Service:
    def __init__(self, project=PROJECT, data_root=None, runs_root=None):
        self.project = Path(project).resolve()
        self.data_root = Path(data_root or self.project.parent / "output").resolve()
        self.runs_root = Path(runs_root or self.data_root / "frontend-runs").resolve()
        self.runs_root.mkdir(parents=True, exist_ok=True)
        self.dataset_store = DatasetStore(self.data_root)
        self.prompt_store = PromptStore(self.data_root / ".prompt-versions")
        self.token = secrets.token_urlsafe(32)
        self.lock = threading.RLock()
        self.processes = {}
        self._result_cache = {}
        self._outcome_cache = {}
        self._context_cache = {}
        self._ranking_cache = {}
        self._model_catalogs = {}
        self._catalog_lock = threading.Lock()
        # A restarted service never claims an old run is still monitored.
        for path in self.runs_root.glob("*/job.json"):
            job = read_json(path)
            if job.get("status") in ACTIVE:
                job.update(status="interrupted", stage="finished",
                           message="服务上次意外停止；已保留完成的记录。本次不自动重跑。")
                atomic_json(path, job)

    def profiles(self):
        profiles = {}
        for name in ("evaluation.models.template.json", "evaluation.mock.json", "evaluation.deepseek.json",
                     "evaluation.qianwen.json", "evaluation.zhipu.json", "evaluation.ateve-jev.json"):
            path = self.project / "examples" / name
            if path.is_file():
                profiles.update(load_config(path)["models"])
        return profiles

    def credentials(self, profiles):
        names = {p["api_key_env"] for p in profiles.values() if p.get("api_key_env")}
        return read_credentials(self.project / ".env", names)

    def datasets(self):
        return self.dataset_store.list()

    def preview_scope(self, request):
        """Count actual selected inputs using the same query selection as a run."""
        if not isinstance(request, dict) or set(request) - {"dataset", "limit"}:
            raise InputError("统计配置包含不支持的字段。")
        dataset_id = request.get("dataset")
        datasets = {item["id"]: item for item in self.datasets()}
        if not isinstance(dataset_id, str) or dataset_id not in datasets:
            raise InputError("请选择现有输入数据集。")
        data = load_dataset(datasets[dataset_id]["path"])
        selected = set(select_queries(data, request.get("limit")))
        responses = [r for r in data["responses"] if r["query_id"] in selected]
        results = [item for response in responses for item in response["results"]]
        pages = {p["page_snapshot_id"]: p for p in data["page_snapshots"]}
        usable_results = [item for item in results if page_evidence(item, pages)[1] is None]
        refs = human_references(data)
        return {
            "dataset": dataset_id,
            "selected": {
                "queries": len(selected), "results": len(results),
                "queries_without_results": len(selected - {
                    r["query_id"] for r in responses if r["results"]}),
                "usable_pages": len({r["page_snapshot_id"] for r in usable_results}),
                "results_with_usable_pages": len(usable_results),
                "human_labels": sum(r["result_id"] in refs for r in results),
            },
            "total": {
                "queries": len(data["queries"]),
                "results": sum(len(r["results"]) for r in data["responses"]),
                "pages": len(pages),
                "human_labels": len(refs),
            },
        }

    def calibration_samples(self, request):
        if not isinstance(request, dict) or set(request) - {"dataset", "query_id", "page"}:
            raise InputError("人工校准样本查询参数无效。")
        dataset = next((d for d in self.datasets() if d["id"] == request.get("dataset")), None)
        if not dataset:
            raise InputError("请选择现有输入数据集。")
        page = request.get("page", 1)
        qid = request.get("query_id", "")
        if type(page) is not int or page < 1 or not isinstance(qid, str):
            raise InputError("查询或页码无效。")
        data = load_dataset(dataset["path"])
        refs = human_references(data)
        queries = {q["query_id"]: q["query_text"] for q in data["queries"]}
        items = [{"query_id": response["query_id"], "query": queries[response["query_id"]],
                  **{k: r.get(k) for k in ("result_id", "rank", "title", "url", "snippet", "published_at")},
                  "reference": refs[r["result_id"]]}
                 for response in data["responses"] for r in response["results"] if r["result_id"] in refs]
        selected = [r for r in items if not qid or r["query_id"] == qid]
        selected.sort(key=lambda r: (r["query_id"], r["rank"]))
        page = min(page, max(1, (len(selected) + 19) // 20))
        return {"dataset": dataset["id"], "total_references": len(refs),
                "distribution": dict(Counter(str(r["grade"]) for r in refs.values())),
                "queries": [{"query_id": k, "query": v} for k, v in queries.items()
                            if any(r["query_id"] == k for r in refs.values())],
                "total": len(selected), "page": page, "page_size": 20,
                "items": selected[(page - 1) * 20:page * 20]}

    def catalog(self):
        profiles = self.profiles()
        credentials = self.credentials(profiles)
        visible = []
        for name, profile in profiles.items():
            placeholder = "REPLACE_" in profile["model"] or "REPLACE_" in (profile.get("base_url") or "")
            credential_ready = not profile.get("api_key_env") or bool(credentials.get(profile["api_key_env"]))
            visible.append({
                "id": name, "model": profile["model"], "protocol": profile["protocol"],
                "simulated": profile["protocol"] == "mock",
                "ready": not placeholder and credential_ready,
                "reason": "需配置模型和地址" if placeholder else "需填写 API Key" if not credential_ready else "",
            })
        datasets = self.datasets()
        default = next((d["id"] for d in datasets if d["id"] == "human-calibration-answerability-150-v1"), None)
        return {
            "token": self.token, "datasets": datasets, "models": visible,
            "default_dataset": default, "workspace_mode": "answerability-calibration-v1",
            "recommended_model": "qianwen",
            "schemes": [{"id": s, "name": SCHEME_LABELS[s]} for s in ("answerability_only_v1", block.SCHEME)],
            "jobs": self.jobs(), "dimensions": DIMENSION_LABELS,
            "key_file": str(self.project / ".env"),
        }

    def _discover_profile(self, name, profile, credentials, refresh=True):
        key = credentials.get(profile.get("api_key_env"), "")
        signature = digest({"profile": profile, "credential": key})
        with self._catalog_lock:
            cached = self._model_catalogs.get(signature)
            if not refresh and cached and time.monotonic() - cached[0] < 300:
                return {**cached[1], "profile": name}
        try:
            result = list_models(profile, key)
        except LLMError:
            with self._catalog_lock:
                self._model_catalogs.pop(signature, None)
            raise
        result.update(profile=name, default_model=profile["model"], status="ready")
        with self._catalog_lock:
            self._model_catalogs[signature] = (time.monotonic(), result)
        return result

    def discover_models(self, request):
        if not isinstance(request, dict) or set(request) != {"profiles"}:
            raise InputError("请提供需要查询的接口配置。")
        profiles = self.profiles()
        names = request["profiles"]
        if (not isinstance(names, list) or not names or len(names) > len(profiles) or
                any(not isinstance(name, str) or name not in profiles for name in names) or
                len(set(names)) != len(names)):
            raise InputError("请选择有效的接口配置。")
        credentials = self.credentials(profiles)

        def fetch(name):
            try:
                return self._discover_profile(name, profiles[name], credentials)
            except LLMError as exc:
                return {"profile": name, "status": "error", "models": [],
                        "error": str(exc), "error_code": exc.code}

        with ThreadPoolExecutor(max_workers=4) as pool:
            groups = list(pool.map(fetch, names))
        return {"groups": groups}

    def root_for(self, job_id, *, deleted=False):
        if not isinstance(job_id, str) or not re.fullmatch(r"[a-f0-9]{32}", job_id):
            raise InputError("无效的运行编号。")
        parent = self.runs_root / ".trash" if deleted else self.runs_root
        if parent.is_symlink():
            raise InputError("运行记录目录不能是符号链接。")
        root = parent / job_id
        if not root.is_dir() or root.is_symlink():
            raise FileNotFoundError("找不到该次评测。")
        return root

    def jobs(self):
        rows = []
        for path in self.runs_root.glob("*/job.json"):
            try:
                rows.append(read_json(path))
            except (OSError, ValueError):
                continue
        return sorted(rows, key=lambda item: item["created_at"], reverse=True)

    def start(self, request, *, dry_run=False):
        with self.lock:
            if any(process.poll() is None for process in self.processes.values()):
                raise Conflict("已有评测正在运行，请等待完成或先停止。")
            allowed = {"dataset", "models", "scoring_model_ids", "query_model", "query_model_id", "query_understanding",
                       "schemes", "mode", "limit", "repeats", "prompt_versions"}
            if not isinstance(request, dict) or set(request) - allowed:
                raise InputError("评测配置包含不支持的字段。")
            datasets = {item["id"]: item for item in self.datasets()}
            if request.get("dataset") not in datasets:
                raise InputError("请选择现有输入数据集。")
            profiles = self.profiles()
            names, schemes = request.get("models"), request.get("schemes")
            if isinstance(names, list) and len(names) != 1:
                raise InputError("每次评测只能选择一个评分模型。")
            for values, available, label in ((names, profiles, "模型"), (schemes, SCHEMES, "方案")):
                if (not isinstance(values, list) or not values or
                        any(not isinstance(v, str) or v not in available for v in values) or
                        len(set(values)) != len(values)):
                    raise InputError(f"请选择有效且不重复的{label}。")
            understanding = request.get("query_understanding", "frozen")
            is_block = block.SCHEME in schemes
            if is_block and (schemes != [block.SCHEME] or understanding != "direct" or
                             request.get("mode", "api_only") != "api_only"):
                raise InputError("Block Content Needs Met 请单独运行，并关闭需求解析、选择 API 返回内容。")
            if understanding not in ("frozen", "direct"):
                raise InputError("无效的需求解析模式。")
            kinds = (["query_spec"] if understanding == "frozen" else []) + [
                prompt_kind(kind, profiles[names[0]]["protocol"])
                for scheme in schemes for kind in SCHEMES[scheme].parts]
            prompt_overrides, prompt_versions = self.prompt_store.resolve(
                request.get("prompt_versions", {}), kinds, understanding)
            if is_block:
                if request.get("prompt_versions"):
                    raise InputError("当前方案使用完整冻结的 skill；不接受自定义 Prompt 版本。")
                prompt_overrides[block.KIND] = block.rules()
                prompt_versions[block.KIND] = {"name": "needs-met-rater 2.1.1 · API 结构版 2 · 三字段输出",
                                               "assembly_version": block.PROMPT_VERSION,
                                               "output_contract": block.COMPACT_CONTRACT,
                                               "prompt_hash": digest(prompt_for(block.KIND, "direct")),
                                               "skill_bundle": block.bundle_manifest()}
            query_model = request.get("query_model") if understanding == "frozen" else None
            if understanding == "frozen" and (not isinstance(query_model, str) or query_model not in profiles):
                raise InputError("请选择有效的需求解析模型。")
            if any(profiles[name]["protocol"] == "typesafe" for name in names) and schemes != ["answerability_only_v1"]:
                raise InputError("Ateve-Jev 当前只支持可回答性单维评分。")
            if query_model and profiles[query_model]["protocol"] == "typesafe":
                raise InputError("Jev 不生成需求说明，请选择 DeepSeek、千问等生成式需求解析模型，或关闭需求解析。")
            used_names = list(dict.fromkeys([*names, *([query_model] if query_model else [])]))
            if len({profiles[name]["protocol"] == "mock" for name in used_names}) > 1:
                raise InputError("需求解析与评分必须同为模拟模型或同为真实模型，以免混用需求说明。")
            mode, limit, repeats = request.get("mode", "api_only"), request.get("limit"), request.get("repeats", 1)
            if mode not in ("api_only", "page_content"):
                raise InputError("无效的证据模式。")
            if limit is not None and (type(limit) is not int or limit < 1):
                raise InputError("查询数量必须为正整数，或选择全部。")
            if type(repeats) is not int or not 1 <= repeats <= 10:
                raise InputError("重复次数必须在 1–10 之间。")
            credentials = self.credentials(profiles)
            for name in used_names:
                p = profiles[name]
                if "REPLACE_" in p["model"] or "REPLACE_" in (p.get("base_url") or ""):
                    raise InputError(f"请先填写 {name} 的模型和接口配置。")
                if not dry_run and p.get("api_key_env") and not credentials.get(p["api_key_env"]):
                    raise InputError(f"请在 .env 填写 {p['api_key_env']}。")
            scoring_ids = request.get("scoring_model_ids", {name: profiles[name]["model"] for name in names})
            if (not isinstance(scoring_ids, dict) or set(scoring_ids) != set(names) or
                    any(not isinstance(value, str) or not value.strip() or len(value) > 512
                        for value in scoring_ids.values())):
                raise InputError("请为所选评分接口选择具体型号。")

            def check_available(name, model_id, label):
                try:
                    catalog = self._discover_profile(name, profiles[name], credentials, refresh=False)
                except LLMError as exc:
                    raise InputError(str(exc)) from None
                if not any(m["id"] == model_id and m["selectable"] for m in catalog["models"]):
                    raise InputError(f"{label}不在 {name} 接口的可用列表中，请刷新模型列表。")

            if "scoring_model_ids" in request and not dry_run:
                for name, model_id in scoring_ids.items():
                    check_available(name, model_id, "评分型号")
            selected_model = (request.get("query_model_id", profiles[query_model]["model"])
                              if query_model else None)
            if query_model and (not isinstance(selected_model, str) or not selected_model.strip()):
                raise InputError("请选择需求解析模型。")
            if query_model and "query_model_id" in request and not dry_run:
                check_available(query_model, selected_model, "需求解析模型")
            model_profiles = {name: {**profiles[name], "model": scoring_ids.get(name, profiles[name]["model"])}
                              for name in used_names}
            if is_block:
                for name, profile in model_profiles.items():
                    # The provider counts reasoning tokens in this budget as well as the JSON answer.
                    profile["max_output_tokens"] = 16384
                    # DeepSeek shares its scheduling and retry profile with answerability.
                    if name != "deepseek":
                        profile.update(max_concurrency=4, requests_per_second=2)
            preparation_profile = query_model
            if query_model and selected_model != model_profiles[query_model]["model"]:
                preparation_profile = "query_" + digest([query_model, selected_model])[:16]
                while preparation_profile in model_profiles:
                    preparation_profile += "_"
                model_profiles[preparation_profile] = {**profiles[query_model], "model": selected_model}
            dataset_path = datasets[request["dataset"]]["path"]
            data = load_dataset(dataset_path)
            if mode == "page_content" and not data["page_snapshots"]:
                raise InputError("当前数据集没有网页快照，请选择 API 返回内容模式。")
            ids = select_queries(data, limit)
            selected = set(ids)
            config = {
                "models": model_profiles,
                "query_model": preparation_profile,
                "query_understanding": understanding,
                "prompt_overrides": prompt_overrides,
                "experiments": [{"id": f"{scheme}-{name}", "scheme": scheme, "model": name,
                                 "mode": mode, "repeats": repeats} for scheme in schemes for name in names],
                "runtime": {"max_workers": 32, "max_input_chars": 180000},
                "failure_recovery": dict(DEFAULT_POLICY),
                "approved_authority_domains": load_config(
                    self.project / "examples/evaluation.deepseek.json")["approved_authority_domains"],
            }
            if is_block:
                config["block_output_contract"] = block.COMPACT_CONTRACT
                config["runtime"] = {"max_workers": 32 if "deepseek" in model_profiles else 4,
                                     "max_input_chars": 100000}
            config = validate_config(config)
            if dry_run:
                if understanding != "direct":
                    raise InputError("执行预检请先关闭独立需求解析；预检不会调用模型生成需求。")
                planned = plan(dataset_path, config, limit=limit)
                return {**planned, "dataset": request["dataset"], "runtime": config["runtime"],
                        "models": {name: {k: p[k] for k in ("model", "max_concurrency", "requests_per_second", "max_output_tokens")}
                                   for name, p in model_profiles.items()},
                        "skill_bundle": block.bundle_manifest() if is_block else None,
                        "job_created": False, "inference_started": False}
            job_id = uuid.uuid4().hex
            root = self.runs_root / job_id
            root.mkdir()
            context = {
                "queries": [{k: q[k] for k in ("query_id", "query_text")}
                            for q in data["queries"] if q["query_id"] in selected],
                "responses": [
                    {**{k: r[k] for k in ("query_id", "status", "actual_result_count", "search_run_id", "response_id")},
                     "results": [{k: item.get(k) for k in ("result_id", "rank", "title", "url",
                                                         "snippet", "text", "published_at", "page_snapshot_id", "raw_ref")}
                                 for item in r["results"]]}
                    for r in data["responses"] if r["query_id"] in selected],
            }
            page_ids = {r.get("page_snapshot_id") for response in context["responses"] for r in response["results"]}
            context["pages"] = {p["page_snapshot_id"]: p.get("body_text")
                                for p in data["page_snapshots"] if p["page_snapshot_id"] in page_ids}
            context["human_references"] = {rid: ref for rid, ref in human_references(data).items()
                                            if ref["query_id"] in selected}
            job = {
                "id": job_id, "created_at": utc_now(), "updated_at": utc_now(),
                "status": "queued", "stage": "prepare" if query_model else "plan", "done": 0,
                "total": len(ids) if query_model else 0,
                "dataset": request["dataset"], "dataset_path": dataset_path, "limit": limit,
                "query_count": len(ids),
                "result_count": sum(len(r["results"]) for r in context["responses"]),
                "models": names, "query_model": query_model, "query_model_id": selected_model, "schemes": schemes,
                "scoring_model_ids": scoring_ids,
                "prompt_versions": prompt_versions,
                "query_understanding": understanding,
                "mode": mode, "repeats": repeats,
                "simulated": all(profiles[name]["protocol"] == "mock" for name in names),
            }
            atomic_json(root / "config.json", config)
            atomic_json(root / "context.json", context)
            atomic_json(root / "job.json", job)
            try:
                process = subprocess.Popen(
                    [sys.executable, "-u", "-m", "frontend.worker", "--job", str(root),
                     "--parent-pid", str(os.getpid())],
                    cwd=str(self.project), env={**os.environ, **credentials},
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            except OSError:
                job.update(status="failed", message="无法启动本地评测进程。")
                atomic_json(root / "job.json", job)
                raise InputError("无法启动本地评测进程。") from None
            self.processes[job_id] = process
            threading.Thread(target=self._watch, args=(job_id, process), daemon=True).start()
            return job

    def _watch(self, job_id, process):
        code = process.wait()
        with self.lock:
            try:
                path = self.root_for(job_id) / "job.json"
            except FileNotFoundError:
                return  # A finished run may already have been deleted.
            job = read_json(path)
            if job["status"] in ACTIVE:
                ended = utc_now()
                job.update(status="failed", stage="finished", updated_at=ended, finished_at=ended,
                           message=f"评测进程已退出（状态码 {code}），已保存完成的记录。")
                atomic_json(path, job)

    def delete(self, job_id):
        with self.lock:
            root = self.root_for(job_id)
            job = read_json(root / "job.json")
            process = self.processes.get(job_id)
            if job["status"] in ACTIVE or (process is not None and process.poll() is None):
                raise Conflict("该次评测仍在运行，请先停止评测后再删除。")
            trash = self.runs_root / ".trash"
            if trash.is_symlink():
                raise InputError("回收目录不能是符号链接。")
            trash.mkdir(exist_ok=True)
            destination = trash / job_id
            if destination.exists() or destination.is_symlink():
                raise Conflict("该运行编号已存在回收记录，未执行删除。")
            # Atomic move keeps original results intact and enables undo.
            root.rename(destination)
            self.processes.pop(job_id, None)
            self._ranking_cache.pop(job_id, None)
            prefix = str(root) + os.sep
            for cache in (self._result_cache, self._outcome_cache, self._context_cache):
                for key in list(cache):
                    if key[0].startswith(prefix):
                        del cache[key]
            return {"id": job_id, "deleted": True}

    def restore(self, job_id):
        with self.lock:
            root = self.root_for(job_id, deleted=True)
            destination = self.runs_root / job_id
            if destination.exists() or destination.is_symlink():
                raise Conflict("同编号运行记录已存在，未执行恢复。")
            job = read_json(root / "job.json")
            if job["status"] in ACTIVE:
                raise Conflict("无法恢复仍标记为运行中的记录。")
            root.rename(destination)
            return job

    def cancel(self, job_id):
        with self.lock:
            root = self.root_for(job_id)
            process = self.processes.get(job_id)
            if process is None or process.poll() is not None:
                raise Conflict("该次评测已经停止。")
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
            job = read_json(root / "job.json")
            ended = utc_now()
            job.update(status="cancelled", stage="finished", updated_at=ended, finished_at=ended,
                       message="已停止后续调用，保留完成的记录；已发出的 API 请求仍可能计费。")
            atomic_json(root / "job.json", job)
            return job

    def close(self):
        for job_id, process in list(self.processes.items()):
            if process.poll() is None:
                self.cancel(job_id)

    def resume(self, job_id, *, retry_failed=False):
        """Continue missing work; resample saved failures only when explicitly requested."""
        if type(retry_failed) is not bool:
            raise InputError("retry_failed 必须为布尔值。")
        with self.lock:
            if any(p.poll() is None for p in self.processes.values()):
                raise Conflict("已有评测正在运行。")
            root = self.root_for(job_id)
            job = read_json(root / "job.json")
            if job["schemes"] != [block.SCHEME] or job["status"] not in ("cancelled", "interrupted", "failed", "partial"):
                raise InputError("仅支持继续已停止或中断的 Block Content Needs Met 任务。")
            config = load_config(root / "config.json")
            credentials = self.credentials(config["models"])
            for p in config["models"].values():
                if p.get("api_key_env") and not credentials.get(p["api_key_env"]):
                    raise InputError("请先配置当前任务所需 API Key。")
            lock = root / "evaluation/.run.lock"
            if lock.exists():
                try:
                    pid = int(lock.read_text())
                    if pid <= 0:
                        raise ValueError()
                except ValueError:
                    raise Conflict("旧任务锁缺少有效进程编号，请先检查执行状态。") from None
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    lock.unlink()
                else:
                    raise Conflict("原执行进程仍存在，不能同时恢复。")
            # Completed exports describe the previous attempt. Archive them so
            # readers fall back to current checkpoints while this resume runs.
            reports = ["summary.json", "ranking_metrics.json", "evaluation/summary.json",
                       "evaluation/judgments.jsonl", "evaluation/block_content_needs_met.jsonl",
                       "evaluation/query_executions.jsonl", "evaluation/model_comparison.json",
                       "evaluation/batch_stop.json"]
            existing = [name for name in reports if (root / name).is_file()]
            if existing:
                archive = root / "previous_reports" / ("resume-" + uuid.uuid4().hex)
                for name in existing:
                    target = archive / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    (root / name).rename(target)
            job.pop("finished_at", None)
            job.update(status="queued", stage="plan", resume=True, retry_failed=retry_failed, updated_at=utc_now(),
                       message=("继续未完成任务，并归档后重新尝试已保存的失败项；已有有效评分保持不变。" if retry_failed else
                                "继续未保存任务和未完成的自动恢复；沿用已使用的重试次数，已完成记录不重新评分。"))
            atomic_json(root / "job.json", job)
            try:
                process = subprocess.Popen(
                    [sys.executable, "-u", "-m", "frontend.worker", "--job", str(root), "--parent-pid", str(os.getpid())],
                    cwd=str(self.project), env={**os.environ, **credentials}, stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError:
                job.update(status="failed", stage="finished", message="无法启动恢复进程。")
                atomic_json(root / "job.json", job)
                raise InputError("无法启动恢复进程。") from None
            self.processes[job_id] = process
            threading.Thread(target=self._watch, args=(job_id, process), daemon=True).start()
            return job

    def _rows(self, root):
        completed = root / "evaluation" / "judgments.jsonl"
        if completed.is_file():
            key = (str(completed), completed.stat().st_mtime_ns)
            if key not in self._result_cache:
                self._result_cache[key] = json_rows(completed)
            return self._result_cache[key]
        outcomes = []
        for path in sorted((root / "evaluation/tasks").glob("*.json")):
            key = (str(path), path.stat().st_mtime_ns)
            if key not in self._outcome_cache:
                value = checked_read(path)
                value.pop("request", None)
                value.pop("call", None)
                self._outcome_cache[key] = value
            outcomes.append(self._outcome_cache[key])
        return flatten(outcomes)

    def _context(self, root):
        path = root / "context.json"
        key = (str(path), path.stat().st_mtime_ns)
        if key not in self._context_cache:
            self._context_cache[key] = read_json(path)
        return self._context_cache[key]

    def prompt_templates(self, filters):
        understanding = filters.get("query_understanding", "frozen")
        schemes = filters.get("schemes", "answerability_only_v1").split(",")
        if understanding not in ("frozen", "direct") or any(s not in SCHEMES for s in schemes):
            raise InputError("请选择有效的评测方案和需求解析模式。")
        profile = None
        if filters.get("model_profile"):
            profile = self.profiles().get(filters["model_profile"])
            if not profile:
                raise InputError("请选择有效的评分接口。")
            if filters.get("model_id"):
                profile = {**profile, "model": filters["model_id"]}
        protocol = profile["protocol"] if profile else None
        if protocol == "typesafe" and schemes != ["answerability_only_v1"]:
            raise InputError("Ateve-Jev 当前只支持可回答性单维评分。")
        kinds = (["query_spec"] if understanding == "frozen" else []) + [
            prompt_kind(kind, protocol) for scheme in dict.fromkeys(schemes) for kind in SCHEMES[scheme].parts]
        try:
            selection = json.loads(filters.get("prompt_versions", "{}"))
        except (ValueError, TypeError):
            raise InputError("Prompt 版本选择格式无效。") from None
        overrides, versions = self.prompt_store.resolve(selection, kinds, understanding)
        items = [
            {"id": kind, "kind": kind, "label": PROMPT_LABELS[kind],
             "source": "current", "query_understanding": understanding,
             "prompt_version": versions.get(kind),
             "versions": self.prompt_store.versions(kind, understanding),
             "default_rules": prompt_content(prompt_for(kind, understanding))["rules"],
             **prompt_content(prompt_for(kind, understanding, overrides))}
            for kind in kinds]
        for item in items:
            if item["kind"] == block.KIND and not item["prompt_version"]:
                item["prompt_version"] = {"name": "needs-met-rater 2.1.1 · API 结构版 2 · 三字段输出",
                                          "assembly_version": block.PROMPT_VERSION,
                                          "output_contract": block.COMPACT_CONTRACT,
                                          "prompt_hash": digest(item["system"])}
            if item["kind"] == "answerability_jev":
                endpoint, _, body = build_request(profile, item["system"], {}, schema_for("answerability"), "")
                item["wire_request"] = {"available": True, "source": "template",
                                        "protocol": "typesafe", "endpoint": endpoint, "body": body}
        return {"items": items}

    def prompt_tasks(self, job_id):
        root = self.root_for(job_id)
        context = self._context(root)
        queries = {q["query_id"]: q["query_text"] for q in context["queries"]}
        sources = {r["result_id"]: r for response in context["responses"] for r in response["results"]}
        tasks = {}
        for row in self._rows(root):
            if row["task_id"] in tasks:
                continue
            source = sources.get(row["result_id"], {})
            tasks[row["task_id"]] = {
                "id": row["task_id"], "kind": row["kind"],
                "label": PROMPT_LABELS[row.get("prompt_kind", row["kind"])],
                "query": queries.get(row["query_id"], ""), "query_id": row["query_id"],
                "rank": None if row["kind"] == "duplicates" else source.get("rank"),
                "title": None if row["kind"] == "duplicates" else source.get("title"),
                "model": row["model_profile"], "model_id": row.get("model_id"),
                "repeat": row["repeat"], "status": row["task_status"],
            }
        for path in sorted((root / "specs/tasks").glob("*.json")):
            saved = checked_read(path)
            tasks[saved["task_id"]] = {
                "id": saved["task_id"], "kind": "query_spec", "label": PROMPT_LABELS["query_spec"],
                "query": queries.get(saved["query_id"], ""), "query_id": saved["query_id"],
                "rank": None, "title": None, "model": saved["model_profile"],
                "model_id": None, "repeat": 0, "status": saved["status"],
            }
        return {"items": sorted(tasks.values(), key=lambda t: (
            t["query_id"], list(PROMPT_LABELS).index(t["kind"]),
            t["model"], t["repeat"], t["rank"] or 0))}

    def _task_snapshot(self, job_id, task_id):
        root = self.root_for(job_id)
        if not re.fullmatch(r"[a-f0-9]{64}", task_id):
            raise InputError("无效的评分任务编号。")
        for directory in ("evaluation", "specs"):
            folder = root / directory / "tasks"
            path = folder / (task_id + ".json")
            if not path.is_file():
                continue
            if path.resolve() != path or path.is_symlink():
                raise InputError("任务记录不能是符号链接。")
            saved = checked_read(path)
            if saved.get("task_id") != task_id:
                raise InputError("任务记录编号不匹配。")
            request = saved.get("request") or {}
            ref = request.get("system_ref")
            if ref:
                if not isinstance(ref, str) or not re.fullmatch(r"[a-f0-9]{64}", ref) or ref != saved.get("effective_prompt_hash", saved["prompt_hash"]):
                    raise InputError("无效的冻结 Prompt 引用。")
                prompt = checked_read(root / directory / "prompts" / (ref + ".json"))
                if digest(prompt["system"]) != ref:
                    raise InputError("冻结 Prompt 校验失败。")
                request["system"] = prompt["system"]
            return saved
        raise FileNotFoundError("该任务尚未保存记录。")

    def task_prompt(self, job_id, task_id):
        saved = self._task_snapshot(job_id, task_id)
        request = saved.get("request") or {}
        available = isinstance(request.get("system"), str) and isinstance(request.get("payload"), dict)
        content = prompt_content(request["system"], request["payload"]) if available else {}
        call = saved.get("call") or {}
        diagnostics = saved.get("response_diagnostics") or call.get("diagnostics") or {}
        if (saved.get("blocked_reason") or saved["status"] == "input_too_large"
                or saved.get("error_code") in ("missing_credentials", "configuration_placeholder")):
            invocation = "not_called"
        elif saved.get("simulated"):
            invocation = "simulated"
        elif call or diagnostics.get("attempts") or saved.get("attempts", 0) > 0:
            invocation = "attempted"
        else:
            invocation = "unknown"
        wire = {"available": False, "source": "unavailable",
                "reason": "历史记录缺少完整 Prompt 或模型配置，无法还原请求体。"}
        config_path = self.root_for(job_id) / "config.json"
        captured_wire = diagnostics.get("wire_request")
        if captured_wire and captured_wire.get("protocol") == "typesafe":
            wire = {**captured_wire, "available": True, "source": "saved_request"}
        elif available and config_path.is_file():
            profile = read_json(config_path).get("models", {}).get(saved.get("model_profile"))
            if profile:
                try:
                    profile = validate_profile(profile)
                    if request.get("max_output_tokens") is not None:
                        profile = validate_profile({**profile, "max_output_tokens": request["max_output_tokens"]})
                    if profile["protocol"] == "mock":
                        wire["reason"] = "本地模拟任务没有外部 API 请求体。"
                    elif content["schema"] is not None or profile["output_mode"] != "json_schema":
                        # Historical body was not captured on the wire. Explicitly label this
                        # reconstruction; never read today's model config, templates or keys.
                        endpoint, _, body = build_request(profile, request["system"], request["payload"],
                                                          schema_for("answerability") if content.get("native_question")
                                                          else content["schema"], api_key="")
                        wire = {"available": True, "source": "reconstructed",
                                "protocol": profile["protocol"], "endpoint": endpoint, "body": body}
                except (InputError, ValueError, KeyError, TypeError):
                    wire["reason"] = "历史模型配置无法用于还原请求体；已保存的 Prompt 仍可查看。"
        # Read the historical snapshot, never rebuild it using today's prompt files.
        return {
            "id": task_id, "kind": saved["kind"], "prompt_kind": saved.get("prompt_kind", saved["kind"]),
            "label": PROMPT_LABELS[saved.get("prompt_kind", saved["kind"])],
            "source": "snapshot", "available": available,
            "simulated": saved.get("simulated", False), "status": saved["status"],
            "blocked_reason": saved.get("blocked_reason"),
            "model": saved.get("model_profile"), "model_id": saved.get("model_id"),
            "prompt_hash": saved.get("prompt_hash"), "input_hash": saved.get("input_hash"),
            "effective_prompt_hash": saved.get("effective_prompt_hash", saved.get("prompt_hash")),
            "auto_recovery": saved.get("auto_recovery"),
            "query_understanding": saved.get("query_understanding", "frozen"),
            "sampling_attempt": saved.get("sampling_attempt", 1),
            "invocation": invocation, "wire_request": wire,
            "prompt_version": read_json(self.root_for(job_id) / "job.json").get("prompt_versions", {}).get(
                saved.get("prompt_kind", saved["kind"])),
            **content,
        }

    def task_response(self, job_id, task_id):
        saved = self._task_snapshot(job_id, task_id)
        call = saved.get("call") or {}
        diagnostics = saved.get("response_diagnostics") or call.get("diagnostics")
        recovery = saved.get("auto_recovery")
        if recovery:
            all_attempts = []
            root = self.root_for(job_id)
            directory = "specs" if saved["kind"] == "query_spec" else "evaluation"
            for record in recovery["attempts"]:
                relative = Path(record["path"])
                base = root / directory
                path = base / relative
                if relative.is_absolute() or ".." in relative.parts or path.is_symlink() or path.resolve() != path:
                    raise InputError("无效的恢复记录路径。")
                archived = checked_read(path)
                if archived.get("task_id") != task_id:
                    raise InputError("恢复记录编号不匹配。")
                archived_call = archived.get("call") or {}
                archived_diag = archived.get("response_diagnostics") or archived_call.get("diagnostics") or {}
                attempts = archived_diag.get("attempts") or [{
                    "attempt": archived_call.get("attempts", 0), "outcome": archived.get("error_code", archived["status"]),
                    "response": {k: archived_call[k] for k in ("text", "usage", "returned_model", "response_id") if k in archived_call} or None,
                }]
                all_attempts.extend({**a, "recovery_round": record["round"], "recovery_cause": record["cause"],
                                     "recovery_action": record["action"], "validation_status": archived["status"],
                                     "validation_reason": archived.get("reason"),
                                     "max_output_tokens": record["max_output_tokens"],
                                     "effective_prompt_hash": record["effective_prompt_hash"]} for a in attempts)
            diagnostics = {**(diagnostics or {}), "attempts": all_attempts}
        availability = "recorded" if diagnostics else "not_recorded"
        if not diagnostics and isinstance(call.get("text"), str):
            # Older successful/invalid-output tasks already saved the visible text.
            availability = "legacy_text"
            diagnostics = {"attempts": [{
                "attempt": call.get("attempts"), "outcome": "completed", "http_status": None,
                "latency_seconds": call.get("latency_seconds"),
                "response": {key: call[key] for key in ("text", "usage", "returned_model", "response_id")
                             if key in call},
            }]}
        if not diagnostics and (saved.get("blocked_reason") or saved["status"] == "input_too_large"
                                or saved.get("error_code") in ("missing_credentials", "configuration_placeholder")):
            availability = "not_called"
        config_path = self.root_for(job_id) / "config.json"
        profile = read_json(config_path).get("models", {}).get(saved.get("model_profile"), {}) if config_path.is_file() else {}
        limits = {"network_timeout_seconds": profile.get("timeout_seconds"),
                  "stream_timeout_seconds": profile.get("stream_timeout_seconds", profile.get("timeout_seconds"))
                  if profile.get("stream") else None}
        return {"id": task_id, "status": saved["status"], "simulated": saved.get("simulated", False),
                "error_code": saved.get("error_code"), "reason": saved.get("reason"),
                "availability": availability, "diagnostics": diagnostics, "limits": limits,
                "auto_recovery": recovery,
                "failure": failure_info({**saved, "judgment_status": "error" if saved["status"] in
                                          ("error", "invalid_output") else saved["status"]}, diagnostics),
                "sampling_attempt": saved.get("sampling_attempt", 1)}

    def _failure_info(self, root, row, cache):
        info = failure_info(row)
        if info and info["code"] == "incomplete_response":
            task_id = row["task_id"]
            if task_id not in cache:
                try:
                    saved = self._task_snapshot(root.name, task_id)
                    cache[task_id] = saved.get("response_diagnostics") or (saved.get("call") or {}).get("diagnostics")
                except FileNotFoundError:
                    cache[task_id] = None
            info = failure_info(row, cache[task_id])
        return info

    def detail(self, job_id):
        root = self.root_for(job_id)
        job = read_json(root / "job.json")
        rows = self._rows(root)
        metrics = {}
        for dimension in DIMENSION_LABELS:
            subset = [r for r in rows if r["dimension"] == dimension]
            if subset:
                metrics[dimension] = {
                    "statuses": dict(Counter(r["judgment_status"] for r in subset)),
                    "distribution": dict(Counter(json.dumps(r["value"], ensure_ascii=False)
                                                 for r in subset if r["judgment_status"] == "rated")),
                    "simulated": sum(r["simulated"] for r in subset),
                }
        preparation = []
        for p in sorted((root / "specs/tasks").glob("*.json")):
            saved = checked_read(p)
            preparation.append({"query_id": saved["query_id"], "status": saved["status"],
                                "reason": saved.get("reason", "")})
        context = self._context(root)
        query_map = {q["query_id"]: q["query_text"] for q in context["queries"]}
        for record in preparation:
            record["query_text"] = query_map.get(record["query_id"], "")
        comparison = root / "evaluation/model_comparison.json"
        failures, failure_cache = {}, {}
        for row in rows:
            info = self._failure_info(root, row, failure_cache)
            if info:
                entry = failures.setdefault(info["code"], {**info, "records": 0, "tasks": set()})
                entry["records"] += 1
                entry["tasks"].add(row["task_id"])
        return {
            "job": job, "metrics": metrics, "records": len(rows),
            "ranking": ranking_summary(self.ranking(job_id, rows=rows)),
            "answerability_distributions": answerability_distributions(
                rows, job, sum(len(r["results"]) for r in context["responses"])),
            "calibration": compare_with_human(
                rows, context.get("human_references", {}),
                [f"{scheme}-{model}" for scheme in job["schemes"]
                 if scheme in ("answerability_only_v1", "legacy_five_v1")
                 for model in job["models"]] if job.get("mode") == "api_only" else [], job["repeats"]),
            "block_needs_met_distributions": block_distributions(rows, job,
                sum(len(r["results"]) for r in context["responses"])),
            "statuses": dict(Counter(r["judgment_status"] for r in rows)),
            "failures": [{**item, "tasks": len(item["tasks"])} for item in failures.values()],
            "auto_recovery": recovery_summary(list({r["task_id"]: {**r, "status": r["task_status"]} for r in rows}.values())),
            "evidence_repairs": {
                "records": sum(bool(r.get("evidence_repairs")) for r in rows),
                "count": sum(len(r.get("evidence_repairs", [])) for r in rows),
            },
            "preparation": preparation,
            "query_executions": [{"query_text": query_map[r["query_id"]], "status": r["status"],
                                  "result_count": r["actual_result_count"]}
                                 for r in context["responses"]],
            "summary": read_json(root / "summary.json") if (root / "summary.json").is_file() else None,
            "comparison": read_json(comparison) if comparison.is_file() else None,
        }

    def ranking(self, job_id, rows=None):
        root = self.root_for(job_id)
        job = read_json(root / "job.json")
        if not (root / "config.json").is_file():
            return {"version": RANKING_VERSION, "job_id": job_id, "groups": [],
                    "unavailable": "缺少运行时的冻结配置，不能计算排序指标。"}
        paths = [root / name for name in ("job.json", "context.json", "config.json", "evaluation/judgments.jsonl")]
        stamp = tuple(p.stat().st_mtime_ns if p.exists() else None for p in paths)
        cached = self._ranking_cache.get(job_id)
        if job.get("status") not in ACTIVE and cached and cached[0] == stamp:
            return cached[1]
        report = build_rankings(self._rows(root) if rows is None else rows,
                                self._context(root), job, read_json(root / "config.json"))
        if job.get("status") not in ACTIVE:
            self._ranking_cache[job_id] = (stamp, report)
            atomic_json(root / "ranking_metrics.json", report)
        return report

    def ranking_comparison(self, job_id, filters):
        baseline_id = filters.get("baseline")
        if baseline_id == job_id:
            raise InputError("请选择另一条运行记录作为基准。")
        current_root, baseline_root = self.root_for(job_id), self.root_for(baseline_id)
        if any(read_json(p / "job.json")["status"] in ACTIVE for p in (current_root, baseline_root)):
            raise InputError("请等待两次评测结束后再比较排序指标。")
        current, baseline = self.ranking(job_id), self.ranking(baseline_id)
        def selected(report, name):
            group_id = filters.get(name)
            groups = report["groups"]
            if not group_id and len(groups) == 1:
                return groups[0]
            match = next((g for g in groups if g["id"] == group_id), None)
            if not match:
                raise InputError("请选择要对比的评测分组。")
            return match
        result = compare_groups(selected(current, "current_group"), selected(baseline, "baseline_group"))
        return {**result, "current_job_id": job_id, "baseline_job_id": baseline_id,
                "current_dataset": current["dataset_path"], "baseline_dataset": baseline["dataset_path"]}

    def results(self, job_id, filters):
        root = self.root_for(job_id)
        context = self._context(root)
        queries = {q["query_id"]: q["query_text"] for q in context["queries"]}
        sources = {r["result_id"]: r for response in context["responses"] for r in response["results"]}
        grouped = {}
        for row in self._rows(root):
            key = (row["result_id"], row["experiment_id"], row["repeat"])
            if key not in grouped:
                source = sources.get(row["result_id"], {})
                grouped[key] = {
                    "result_id": row["result_id"], "experiment_id": row["experiment_id"],
                    "repeat": row["repeat"], "scheme": row["scheme"], "model": row["model_profile"],
                    "model_id": row.get("model_id"),
                    "query": queries.get(row["query_id"], ""), "query_id": row["query_id"],
                    "title": source.get("title") or "无标题", "url": source.get("url") or "",
                    "rank": source.get("rank"), "simulated": row["simulated"], "dimensions": {},
                    "query_understanding": row.get("query_understanding", "frozen"),
                }
            grouped[key]["dimensions"][row["dimension"]] = row
        rows = sorted(grouped.values(), key=lambda r: (r["query_id"], r["rank"] or 0,
                                                       r["experiment_id"], r["repeat"]))
        text = filters.get("q", "").strip().lower()
        for name in ("scheme", "model"):
            if filters.get(name):
                rows = [r for r in rows if r[name] == filters[name]]
        if text:
            rows = [r for r in rows if text in (r["query"] + " " + r["title"] + " " + r["url"]).lower()]
        dimension = filters.get("dimension")
        status = filters.get("status")
        if dimension or status:
            rows = [r for r in rows if any(
                (not dimension or key == dimension) and (not status or item["judgment_status"] == status)
                for key, item in r["dimensions"].items())]
        try:
            page = max(1, int(filters.get("page", "1")))
        except ValueError:
            raise InputError("无效的页码。") from None
        count = len(rows)
        page = min(page, max(1, (count + 19) // 20))
        selected = copy.deepcopy(rows[(page - 1) * 20:page * 20])
        specs = {r["query_id"]: r.get("spec") for r in json_rows(root / "specs/query_specs.jsonl")}
        failure_cache = {}
        for row in selected:
            row["dimensions"] = {key: row["dimensions"][key] for key in DIMENSION_LABELS
                                 if key in row["dimensions"]}
            for measurement in row["dimensions"].values():
                measurement["failure"] = self._failure_info(root, measurement, failure_cache)
            source = sources.get(row["result_id"], {})
            # Complete visible evidence stays available in the detail drawer.
            row["source"] = {k: source.get(k) for k in ("snippet", "text", "published_at")}
            row["source"]["page_text"] = context.get("pages", {}).get(source.get("page_snapshot_id"))
            row["query_spec"] = specs.get(row["query_id"])
            row["human_reference"] = context.get("human_references", {}).get(row["result_id"])
            answerability = row["dimensions"].get("answerability")
            comparable = (row["human_reference"] and answerability and not row["simulated"]
                          and answerability.get("mode") == "api_only"
                          and answerability["judgment_status"] == "rated"
                          and valid_grade(answerability["value"]))
            row["human_agreement"] = (answerability["value"] == row["human_reference"]["grade"]
                                      if comparable else None)
        return {"items": selected, "total": count, "page": page, "page_size": 20}

    def download(self, job_id, name):
        root = self.root_for(job_id)
        if name == "ranking_metrics.json":
            return json.dumps(self.ranking(job_id), ensure_ascii=False, indent=2).encode()
        if name == "calibration.json":
            return json.dumps(self.detail(job_id)["calibration"], ensure_ascii=False, indent=2).encode()
        if name == "judgments.jsonl":
            return "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in self._rows(root)).encode()
        if name == "block_content_needs_met.jsonl":
            outcomes = (checked_read(p) for p in (root / "evaluation/tasks").glob("*.json"))
            return "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in block.native_records(outcomes)).encode()
        paths = {"summary.json": root / "summary.json",
                 "model_comparison.json": root / "evaluation/model_comparison.json",
                 "query_specs.jsonl": root / "specs/query_specs.jsonl"}
        if name not in paths or not paths[name].is_file():
            raise FileNotFoundError("该报告尚未生成。")
        return paths[name].read_bytes()
