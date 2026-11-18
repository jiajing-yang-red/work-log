"""One isolated evaluation process per browser run."""

import argparse
import json
import os
import threading
import time
from pathlib import Path

from quality_eval.config import load_config
from quality_eval.execution import BatchPaused
from quality_eval.query_specs import prepare_specs
from quality_eval.runner import plan, run
from quality_eval.storage import atomic_json
from quality_input.common import read_json
from quality_metrics.ranking import build_rankings
from quality_input.pipeline import utc_now


def execute(root):
    job = read_json(root / "job.json")

    def update(**values):
        job.update(values, updated_at=utc_now())
        atomic_json(root / "job.json", job)

    def progress(done, total):
        update(done=done, total=total)

    try:
        config = load_config(root / "config.json")
        specs_path = None
        prepared = {"status": "skipped", "reason": "独立需求解析已关闭；直接依据原始 query 评分。"}
        if config["query_understanding"] == "frozen":
            specs_path = root / "specs"
            update(status="running", stage="prepare", done=0, total=job["query_count"])
            prepared = prepare_specs(job["dataset_path"], config, specs_path,
                                     limit=job["limit"], progress=progress,
                                     resume=bool(job.get("resume") and (specs_path / "manifest.json").exists()))
            update(preparation=prepared)
            if prepared["ready_count"] == 0:
                update(status="partial", stage="finished", finished_at=utc_now(),
                       message="需求解析未成功。请查看需求解析记录；本次未继续发起 URL 评分。")
                return
        update(status="running", stage="plan", done=0, total=0, preparation=prepared)
        planned = plan(job["dataset_path"], config, specs_path, job["limit"])
        atomic_json(root / "plan.json", planned)
        update(stage="evaluate", done=0,
               total=sum(item["tasks"] for item in planned["experiments"].values()))
        summary = run(job["dataset_path"], config, specs_path, root / "evaluation",
                      limit=job["limit"], progress=progress,
                      retry_failed=bool(job.get("retry_failed")),
                      resume=bool(job.get("resume") and (root / "evaluation/manifest.json").exists()))
        atomic_json(root / "summary.json", summary)
        rows = [json.loads(line) for line in (root / "evaluation/judgments.jsonl").read_text().splitlines() if line.strip()]
        atomic_json(root / "ranking_metrics.json", build_rankings(rows, read_json(root / "context.json"), job, config))
        update(status="partial" if prepared["status"] == "partial" else summary["status"],
               stage="finished", finished_at=utc_now(),
               message="执行结束；无法判断、失败和模拟结果均独立保留。")
    except BatchPaused as exc:
        update(status="interrupted", stage="finished", finished_at=utc_now(), message=str(exc))
    except Exception as exc:
        # Never put a traceback or credentials into the browser response.
        message = str(exc)
        for name, value in os.environ.items():
            if name.endswith("API_KEY") and value:
                message = message.replace(value, "[redacted]")
        update(status="failed", stage="finished", finished_at=utc_now(),
               message=message[:1000] or "评测进程发生错误。")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--job", required=True)
    parser.add_argument("--parent-pid", type=int, required=True)
    args = parser.parse_args()

    def monitor_parent():
        while True:
            if os.getppid() != args.parent_pid:
                os._exit(1)
            time.sleep(1)

    threading.Thread(target=monitor_parent, daemon=True).start()
    execute(Path(args.job).resolve())
