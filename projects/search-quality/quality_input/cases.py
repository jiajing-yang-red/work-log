"""Split saved case reports into benchmark × retrieval-condition input bundles.

Run with python3 -m quality_input.cases --source DIR --output NEW_DIR.
This converts local data only; it never invokes a scoring model.
"""

import argparse
import hashlib
import re
from pathlib import Path

from .common import InputError, parse_json, write_json
from .pipeline import import_dataset, load_dataset, utc_now


BENCHMARKS = {"browsecomp": "BrowseComp", "frames": "FRAMES", "simpleqa": "SimpleQA"}
CONDITIONS = {"off": "不重排", "bge30": "BGE Top30 重排", "bge50": "BGE Top50 重排",
              "qwen30": "Qwen Top30 重排", "qwen50": "Qwen Top50 重排"}
VERSION = "case_directory_with_summaries_v1"


def read_case(path):
    raw = path.read_bytes()
    case = parse_json(raw.decode("utf-8-sig"))
    match = re.fullmatch(r"(browsecomp|frames|simpleqa)-([1-9][0-9]*)", path.stem)
    if not match or not isinstance(case, dict) or case.get("key") != path.stem:
        raise InputError(f"Case ID must match its benchmark filename: {path.name}")
    for field in ("query", "正确答案"):
        if not isinstance(case.get(field), str) or not case[field].strip():
            raise InputError(f"Missing {field}: {path.name}")
    for condition in CONDITIONS:
        branch = case.get(condition)
        if (not isinstance(branch, dict) or not isinstance(branch.get("urls"), list)
                or not isinstance(branch.get("reply"), str)):
            raise InputError(f"Missing urls/reply for {path.name}: {condition}")
        for rank, result in enumerate(branch["urls"], 1):
            if (not isinstance(result, dict) or
                    any(not isinstance(result.get(k), str) for k in ("url", "title", "summary"))
                    or not result["url"].strip()):
                raise InputError(f"Expected url/title/summary object: {path.name}, {condition}, rank {rank}")
    return case, hashlib.sha256(raw).hexdigest(), match[1]


def canonical_cases(cases, source, benchmark, condition):
    run_id = f"cases-{benchmark}-{condition}"
    return {
        "queries": [{"query_id": c["key"], "query_text": c["query"], "category": benchmark}
                    for c, _ in cases],
        "search_runs": [{"search_run_id": run_id, "provider": "not_recorded",
                         "requested_top_k": 10,
                         "request_parameters": {"condition": condition, "description": CONDITIONS[condition]}}],
        "responses": [{
            "query_id": c["key"], "search_run_id": run_id,
            # This records delivered-list presence, not the unknown collection/abstention cause.
            "status": "ok" if c[condition]["urls"] else "empty",
            "results": [{"rank": rank, "url": r["url"], "title": r["title"], "snippet": r["summary"]}
                        for rank, r in enumerate(c[condition]["urls"], 1)],
        } for c, _ in cases],
        "page_snapshots": [], "reference_labels": [],
        # Auxiliary fields remain in raw/source.json. The canonical input adapter does not
        # copy them into normalized queries, results, labels, or model request payloads.
        "case_answers": {c["key"]: {"reply": c[condition]["reply"], "gold_answer": c["正确答案"]}
                         for c, _ in cases},
        "case_provenance": {
            "converter": VERSION, "original_directory": str(source),
            "benchmark": benchmark, "condition": condition,
            "summary_mapping": "urls[].summary -> results[].snippet, unchanged",
            "status_policy": "Presence of delivered results only; collection and abstention causes are unknown.",
            "source_files": {c["key"] + ".json": {"sha256": sha} for c, sha in cases},
        },
    }


def split_case_directory(source, output, progress=None):
    source, output = Path(source).resolve(), Path(output).resolve()
    if not source.is_dir():
        raise InputError(f"Source directory does not exist: {source}")
    if output.exists():
        raise InputError(f"Output already exists; choose a new directory: {output}")
    grouped = {name: [] for name in BENCHMARKS}
    source_files = {}
    # Validate every input before publishing any bundles; never silently omit a broken case.
    for path in sorted(source.glob("*.json")):
        case, sha, benchmark = read_case(path)
        grouped[benchmark].append(path)
        source_files[path.name] = sha
    if any(not paths for paths in grouped.values()):
        raise InputError("Source must include BrowseComp, FRAMES, and SimpleQA cases")
    output.mkdir(parents=True)
    (output / "inputs").mkdir()
    (output / "datasets").mkdir()
    index = {"converter": VERSION, "created_at": utc_now(), "source_directory": str(source),
             "source_files": source_files, "datasets": []}
    for benchmark, paths in grouped.items():
        cases = []
        for path in sorted(paths, key=lambda p: int(p.stem.split("-")[-1])):
            case, sha, _ = read_case(path)
            if sha != source_files[path.name]:
                raise InputError(f"Source changed during conversion: {path}")
            cases.append((case, sha))
        for order, (condition, label) in enumerate(CONDITIONS.items(), 1):
            slug = f"{benchmark}-{order:02d}-{condition}"
            canonical_path = output / "inputs" / f"{slug}.json"
            bundle = output / "datasets" / slug
            name = f"{BENCHMARKS[benchmark]} · {condition}（{label}）"
            write_json(canonical_path, canonical_cases(cases, source, benchmark, condition))
            report = import_dataset(canonical_path, bundle, dataset_id=name, use_collection_time=False)
            if report["status"] != "ready":
                raise InputError(f"Import blocked; inspect {bundle / 'input_report.json'}")
            load_dataset(bundle)  # Check the completed bundle and every frozen-file checksum.
            entry = {"name": name, "benchmark": benchmark, "condition": condition,
                     "path": str(bundle), "input_path": str(canonical_path),
                     "counts": report["counts"], "issue_counts": report["issue_counts"]}
            index["datasets"].append(entry)
            if progress:
                progress(entry)
    write_json(output / "datasets_index.json", index)
    return index


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--output", required=True, help="New directory for canonical inputs and 15 normalized bundles")
    args = parser.parse_args()
    def progress(entry):
        print(f"Ready: {entry['name']} — {entry['counts']['queries']} queries / {entry['counts']['results']} results", flush=True)
    try:
        index = split_case_directory(args.source, args.output, progress)
    except (InputError, OSError, UnicodeError) as exc:
        parser.exit(1, f"{exc}\n")
    print(f"Completed {len(index['datasets'])} datasets: {Path(args.output).resolve() / 'datasets_index.json'}", flush=True)


if __name__ == "__main__":
    main()
