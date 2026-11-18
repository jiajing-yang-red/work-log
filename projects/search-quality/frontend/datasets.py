"""Local file selection and validated input-layer imports for the dashboard."""

import re
import threading
import uuid
from pathlib import Path

from quality_eval.storage import atomic_json
from quality_input.common import InputError, digest, read_json
from quality_input.pipeline import import_dataset, load_dataset


EXTENSIONS = {".sqlite", ".sqlite3", ".db", ".json", ".jsonl", ".ndjson", ".csv"}


def local_path(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 4096 or "\0" in value:
        raise InputError("请输入有效的本机绝对路径。")
    value = value.strip()
    if len(value) > 1 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]  # Finder's “Copy as Pathname” may include quotes.
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise InputError("请填写完整路径，例如 /Users/你的用户名/Downloads/data.sqlite。")
    return path.resolve()


class DatasetStore:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.registry = self.root / ".frontend-datasets.json"
        self.lock = threading.Lock()

    def registered_paths(self):
        if not self.registry.exists():
            return []
        data = read_json(self.registry)
        if (not isinstance(data, dict) or data.get("version") != 1
                or not isinstance(data.get("paths"), list)
                or any(not isinstance(p, str) or not Path(p).is_absolute() for p in data["paths"])):
            raise InputError("已添加数据集的目录记录格式无效。")
        return data["paths"]

    def entry(self, path):
        path = Path(path).resolve()
        manifest = read_json(path / "manifest.json")
        report = read_json(path / "input_report.json")
        if "dataset_version" not in manifest:
            raise InputError("该目录不是标准化数据集。")
        counts = report["counts"]
        external = path.parent != self.root
        return {
            "id": "external_" + digest(str(path))[:24] if external else path.name,
            "name": manifest.get("dataset_id", path.name), "path": str(path),
            "status": manifest["status"], "queries": counts["queries"], "results": counts["results"],
            "pages": counts["page_snapshots"], "empty_queries": counts["response_status"].get("empty", 0),
            "reference_labels": counts.get("reference_labels", 0),
            "reference_labels_by_source": counts.get("reference_labels_by_source", {}),
            "as_of": manifest.get("evaluation_as_of"),
            "source_path": next((s["original_path"] for s in manifest.get("sources", [])
                                 if s.get("original_path")), None),
            "external": external,
        }

    def list(self):
        paths = [p.parent for p in sorted(self.root.glob("*/manifest.json")) if not p.parent.is_symlink()]
        paths += [Path(p) for p in self.registered_paths()]
        items, seen = [], set()
        for path in paths:
            try:
                path = path.resolve()
                if path in seen:
                    continue
                seen.add(path)
                item = self.entry(path)
                if item["status"] == "ready":
                    items.append(item)
            except (OSError, ValueError, KeyError, TypeError):
                continue
        return items

    def browse(self, request):
        if not isinstance(request, dict) or set(request) - {"path", "page", "filter"}:
            raise InputError("文件浏览参数无效。")
        path = local_path(request.get("path") or str(Path.home()))
        if path.is_file():
            path = path.parent
        if not path.is_dir():
            raise InputError("目录不存在，请检查路径。")
        page, text = request.get("page", 1), request.get("filter", "")
        if type(page) is not int or page < 1 or not isinstance(text, str) or len(text) > 256:
            raise InputError("文件浏览页码或过滤条件无效。")
        entries = []
        try:
            for child in path.iterdir():
                if child.name.startswith(".") or text.casefold() not in child.name.casefold():
                    continue
                try:
                    directory = child.is_dir()
                    if not directory and (not child.is_file() or child.suffix.lower() not in EXTENSIONS):
                        continue
                    entries.append({
                        "name": child.name, "path": str(child),
                        "kind": "directory" if directory else "file",
                        "bundle": directory and (child / "manifest.json").is_file(),
                    })
                except OSError:
                    continue
        except PermissionError:
            raise InputError("无法读取这个目录，请检查本机访问权限。") from None
        entries.sort(key=lambda e: (e["kind"] != "directory", e["name"].casefold()))
        pages = max(1, (len(entries) + 99) // 100)
        page = min(page, pages)
        return {
            "path": str(path), "parent": str(path.parent), "bundle": (path / "manifest.json").is_file(),
            "items": entries[(page - 1) * 100:page * 100], "page": page, "pages": pages, "total": len(entries),
        }

    def add(self, request):
        allowed = {"path", "name", "time_policy", "evaluation_as_of", "mapping_path"}
        if not isinstance(request, dict) or set(request) - allowed:
            raise InputError("数据集加载参数无效。")
        path = local_path(request.get("path"))
        if path.name == "manifest.json" and path.is_file():
            path = path.parent
        if not self.lock.acquire(blocking=False):
            raise InputError("已有文件正在导入，请等待完成后重试。")
        try:
            if path.is_dir():
                if not (path / "manifest.json").is_file():
                    raise InputError("这个目录不是标准化数据集，请进入目录选择原始数据文件。")
                load_dataset(path)
                if path.parent != self.root:
                    paths = self.registered_paths()
                    if str(path) not in paths:
                        self.root.mkdir(parents=True, exist_ok=True)
                        atomic_json(self.registry, {"version": 1, "paths": [*paths, str(path)]})
                return {"status": "ready", "dataset": self.entry(path), "datasets": self.list(), "imported": False}
            if not path.is_file():
                raise InputError("文件不存在，请检查路径。")
            if path.suffix.lower() not in EXTENSIONS:
                raise InputError("支持 SQLite、JSON、JSONL 和 CSV，或已标准化的数据集目录。")
            name = request.get("name") or path.stem
            if not isinstance(name, str) or not name.strip() or len(name) > 200:
                raise InputError("数据集名称须为 1–200 个字符。")
            policy = request.get("time_policy", "collection")
            if policy not in ("collection", "explicit", "unspecified"):
                raise InputError("请选择有效的评估时间来源。")
            as_of = request.get("evaluation_as_of")
            if policy == "explicit" and (not isinstance(as_of, str) or not as_of.strip()):
                raise InputError("请填写带时区的评估时间，例如 2026-10-20T13:36:00+08:00。")
            if policy != "explicit" and as_of:
                raise InputError("仅指定评估时间时可以填写时间值。")
            mapping = local_path(request["mapping_path"]) if request.get("mapping_path") else None
            slug = re.sub(r"[^\w.-]+", "-", path.stem)[:48].strip(".-") or "input"
            output = self.root / f"dataset-{slug}-{uuid.uuid4().hex[:12]}"
            report = import_dataset(
                path, output, dataset_id=name.strip(), mapping_path=mapping,
                evaluation_as_of=as_of if policy == "explicit" else None,
                use_collection_time=policy == "collection")
            summary = {k: report[k] for k in ("status", "counts", "issue_counts")}
            summary["issues"] = sorted(report["issues"], key=lambda i: i["severity"] != "error")[:20]
            if report["status"] != "ready":
                return {"status": "blocked", "report": summary, "report_path": str(output / "input_report.json")}
            load_dataset(output)
            return {"status": "ready", "dataset": self.entry(output), "datasets": self.list(),
                    "imported": True, "report": summary, "report_path": str(output / "input_report.json")}
        except PermissionError:
            raise InputError("无法读取文件或保存导入结果，请检查本机路径权限。") from None
        finally:
            self.lock.release()
