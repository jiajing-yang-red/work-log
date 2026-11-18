import argparse
import json
import sys

from .common import InputError
from .pipeline import import_dataset, load_dataset


def main(argv=None):
    parser = argparse.ArgumentParser(description="Search quality evaluation: input layer")
    commands = parser.add_subparsers(dest="command", required=True)
    ingest = commands.add_parser("import", help="Freeze, normalize and validate an input file")
    ingest.add_argument("--input", required=True)
    ingest.add_argument("--output", required=True, help="A new directory; existing paths are rejected")
    ingest.add_argument("--format", default="auto", choices=("auto", "sqlite", "json", "jsonl", "csv"))
    ingest.add_argument("--mapping", help="Explicit flat-file field mapping JSON")
    ingest.add_argument("--dataset-id")
    times = ingest.add_mutually_exclusive_group()
    times.add_argument("--evaluation-as-of", help="ISO datetime with timezone")
    times.add_argument("--use-collection-time", action="store_true",
                       help="Use latest collection completion as evaluation time")
    inspect = commands.add_parser("inspect", help="Verify and summarize a normalized dataset")
    inspect.add_argument("dataset")
    args = parser.parse_args(argv)
    try:
        if args.command == "import":
            report = import_dataset(
                args.input, args.output, format=args.format, mapping_path=args.mapping,
                evaluation_as_of=args.evaluation_as_of,
                use_collection_time=args.use_collection_time, dataset_id=args.dataset_id)
        else:
            report = load_dataset(args.dataset)["input_report"]
        print(json.dumps({key: report[key] for key in
                          ("status", "counts", "capabilities", "issue_counts")},
                         ensure_ascii=False, indent=2))
        return 0 if report["status"] == "ready" else 2
    except (InputError, OSError, ValueError) as exc:
        print(f"Input error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
