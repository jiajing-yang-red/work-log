"""Immutable local prompt versions. Running jobs receive copies, never live paths."""

import re
import uuid
from pathlib import Path

from quality_eval.schemes import PROMPT_KINDS, prompt_for, validate_prompt_overrides
from quality_eval.storage import checked_read, checked_write
from quality_input.common import InputError, digest
from quality_input.pipeline import utc_now


def prompt_mode(kind, mode):
    return "frozen" if kind == "query_spec" else mode


class PromptStore:
    def __init__(self, root):
        self.root = Path(root)

    def _check_root(self):
        if self.root.is_symlink():
            raise InputError("Prompt 版本目录不能是符号链接。")

    def read(self, version_id):
        self._check_root()
        if not isinstance(version_id, str) or not re.fullmatch(r"[a-f0-9]{32}", version_id):
            raise InputError("无效的 Prompt 版本编号。")
        path = self.root / (version_id + ".json")
        if path.is_symlink():
            raise InputError("Prompt 版本不能是符号链接。")
        if not path.is_file():
            raise InputError("所选 Prompt 版本不存在，请重新选择。")
        item = checked_read(path)
        if item.get("id") != version_id:
            raise InputError("Prompt 版本编号不匹配。")
        return item

    def versions(self, kind, mode):
        self._check_root()
        items = [self.read(p.stem) for p in self.root.glob("*.json")]
        return [{k: item[k] for k in ("id", "name", "created_at", "prompt_hash")}
                for item in sorted(items, key=lambda v: v["created_at"], reverse=True)
                if item["kind"] == kind and item["query_understanding"] == prompt_mode(kind, mode)]

    def save(self, request):
        if not isinstance(request, dict) or set(request) != {"kind", "query_understanding", "name", "rules"}:
            raise InputError("请提供 Prompt 类型、需求解析模式、版本名称和规则。")
        kind, mode, name, rules = (request[k] for k in ("kind", "query_understanding", "name", "rules"))
        if kind == "block_needs_met":
            raise InputError("此方案使用由完整 skill 生成的 API 提示词；请通过维护技能或提示词组装器更新。")
        if not isinstance(kind, str) or kind not in PROMPT_KINDS or mode not in ("frozen", "direct"):
            raise InputError("无效的 Prompt 类型或需求解析模式。")
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 80:
            raise InputError("版本名称不能为空，且不能超过 80 个字符。")
        validate_prompt_overrides({kind: rules})
        mode = prompt_mode(kind, mode)
        item = {"id": uuid.uuid4().hex, "name": name.strip(), "kind": kind,
                "query_understanding": mode, "rules": rules, "created_at": utc_now(),
                "prompt_hash": digest(prompt_for(kind, mode, {kind: rules}))}
        self._check_root()
        self.root.mkdir(parents=True, exist_ok=True)
        checked_write(self.root / (item["id"] + ".json"), item)
        return item

    def resolve(self, selection, kinds, mode):
        if not isinstance(selection, dict) or set(selection) - set(kinds):
            raise InputError("Prompt 版本与当前评测方案或需求解析开关不匹配。")
        overrides, versions = {}, {}
        for kind, version_id in selection.items():
            item = self.read(version_id)
            if item["kind"] != kind or item["query_understanding"] != prompt_mode(kind, mode):
                raise InputError("所选 Prompt 版本不适用于当前任务或需求解析模式。")
            overrides[kind] = item["rules"]
            versions[kind] = {k: item[k] for k in ("id", "name", "created_at", "prompt_hash")}
        return validate_prompt_overrides(overrides), versions
