import argparse
import json
import sys
import time

from quality_input.common import InputError
from quality_llm import LLMError
from .config import load_config
from .query_specs import prepare_specs
from .runner import plan, run


def main(argv=None):
    parser = argparse.ArgumentParser(description="Search evaluation schemes × model experiments")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("prepare", "plan", "run"):
        command = commands.add_parser(name)
        command.add_argument("--dataset", required=True)
        command.add_argument("--config", required=True)
        command.add_argument("--limit-queries", type=int)
        if name in ("prepare", "run"):
            command.add_argument("--output", required=True)
            command.add_argument("--resume", action="store_true")
            command.add_argument("--retry-failed", action="store_true",
                                 help="Explicitly resample failed/invalid tasks; previous attempts archived")
        if name in ("plan", "run"):
            command.add_argument("--specs", help="Frozen query specs; omit when query_understanding=direct")
        if name == "prepare":
            command.add_argument("--reference-answers")
    args = parser.parse_args(argv)
    last = [0.0]

    def progress(done, total):
        now = time.monotonic()
        if now - last[0] >= 5 or done == total:
            print(f"Tasks: {done}/{total}", file=sys.stderr, flush=True)
            last[0] = now
    try:
        config = load_config(args.config)
        if args.command == "prepare":
            result = prepare_specs(args.dataset, config, args.output, limit=args.limit_queries,
                                   resume=args.resume, retry_failed=args.retry_failed,
                                   references_path=args.reference_answers, progress=progress)
        elif args.command == "plan":
            result = plan(args.dataset, config, args.specs, args.limit_queries)
        else:
            if config["query_understanding"] == "frozen" and not args.specs:
                raise InputError("Frozen query understanding requires --specs")
            result = run(args.dataset, config, args.specs, args.output, limit=args.limit_queries,
                         resume=args.resume, retry_failed=args.retry_failed, progress=progress)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2 if result.get("status") == "partial" else 0
    except (InputError, LLMError, OSError, ValueError, KeyError) as exc:
        print(f"Evaluation error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
