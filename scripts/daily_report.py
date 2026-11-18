"""Build Chinese workday reports from the checked-out main branch."""
import argparse
import html
import os
import re
import subprocess
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

BEIJING = ZoneInfo("Asia/Shanghai")
START = date(2026, 11, 18)
DAY = timedelta(days=1)
BOT = "41898282+github-actions[bot]@users.noreply.github.com"
AUTO_SUBJECT = "日报：自动汇总工作进展"
EMPTY = "当天 main 分支没有新的工作提交。"


def git(*args):
    return subprocess.check_output(["git", *args]).decode("utf-8", errors="replace")


def cutoff(run_time):
    scheduled_day = run_time.astimezone(BEIJING).date()
    while scheduled_day.weekday() >= 5:
        scheduled_day -= DAY
    report_day = scheduled_day - DAY
    while report_day.weekday() >= 5:
        report_day -= DAY
    return report_day


def workdays(start, end):
    while start <= end:
        if start.weekday() < 5:
            yield start
        start += DAY


def history(end):
    by_day = defaultdict(list)
    for line in git("log", "--reverse", "--topo-order", "--format=%H%x09%ct%x09%ae%x09%s", "HEAD").splitlines():
        sha, timestamp, email, subject = line.split("\t", 3)
        if email == BOT and subject == AUTO_SUBJECT:
            continue
        day = datetime.fromtimestamp(int(timestamp), BEIJING).date()
        if START <= day <= end and day.weekday() < 5:
            by_day[day].append((sha, subject))
    return by_day


def project_for(path):
    parts = path.split("/")
    if parts[0] == "projects" and len(parts) >= 2:
        return {"search-eval": "离线排序评测", "search-quality": "搜索结果质量评测"}.get(parts[1], parts[1])
    if parts[0] == "skills":
        return "Skills"
    if parts[0] == "daily" or path in {".github/workflows/daily-report.yml", "scripts/daily_report.py",
                                        "scripts/test_daily_report.py"}:
        return "日报"
    return "仓库维护"


def safe_text(text):
    text = html.escape(" ".join(text.split()), quote=False)
    return re.sub(r"([\\`*_\[\]~|])", r"\\\1", text).replace("@", "&#64;")


def progress_note(sha, paths):
    notes = [path for status, path in paths if status != "D" and path.endswith("/PROGRESS.md")]
    if not notes:
        return ""
    patch = git("show", "--format=", "--diff-merges=first-parent", "--unified=0", sha, "--", *notes)
    for line in patch.splitlines():
        if not line.startswith("+") or line.startswith(("+++", "+#", "+|", "+```")):
            continue
        paragraph = re.sub(r"\[([^\]]+)\]\([^)]+\)", "", line[1:]).strip(" -")
        if len(paragraph) >= 16:
            return safe_text(paragraph[:220]) + ("…" if len(paragraph) > 220 else "")
    return ""


def summarize(sha):
    # NUL delimiters also preserve spaces, quotes and non-ASCII filenames.
    fields = git("diff-tree", "--root", "--diff-merges=first-parent", "--no-commit-id",
                 "--no-renames", "--name-status", "-r", "-z", sha).split("\0")
    paths = list(zip(fields[::2], fields[1::2]))
    groups = list(dict.fromkeys(project_for(path) for _, path in paths))
    counts = Counter(status for status, _ in paths)
    actions = "、".join(f"{label} {counts[code]}" for code, label in
                       [("A", "新增"), ("M", "修改"), ("D", "删除"), ("T", "变更类型")] if counts[code])
    # Prefer a report or a progress document as evidence over incidental files.
    ordered = sorted(paths, key=lambda item: ("/reports/" not in item[1],
                                              not item[1].endswith("PROGRESS.md"), item[1]))
    names = [safe_text(path) for _, path in ordered[:2]]
    detail = actions + " 个文件" if actions else ""
    if names:
        detail += "（" + "、".join(names) + (" 等" if len(paths) > 2 else "") + "）"
    note = progress_note(sha, paths)
    if note:
        detail = note + " " + detail
    return "、".join(groups) or "仓库维护", detail


def report(day, commits, repo):
    path = Path("daily") / day.strftime("%Y/%m/%Y-%m-%d.md")
    old = path.read_text() if path.exists() else ""
    # A hand-written entry may use one release commit as evidence for several bullets.
    known = set(re.findall(re.escape(repo) + r"/commit/([0-9a-f]{7,40})\b", old))
    groups = defaultdict(dict)
    for sha, subject in commits:
        if any(sha.startswith(prefix) for prefix in known):
            continue
        group, detail = summarize(sha)
        links = groups[group].setdefault((subject, detail), [])
        links.append(f"[{sha[:7]}]({repo}/commit/{sha})")
    if old and not groups:
        return False

    bullets = []
    for group, items in groups.items():
        rows = []
        for (subject, detail), links in items.items():
            item = safe_text(subject)
            if detail:
                item += "。" + detail
            rows.append(item + " " + "、".join(links))
        bullets.append(f"- **{safe_text(group)}**　" + "；".join(rows))
    title = safe_text(commits[0][1]) if commits else "当日提交记录"
    if not old:
        new = f"# {day} · {title}\n\n## 今日进展\n\n" + ("\n".join(bullets) or EMPTY) + "\n"
    else:
        # Replace only our empty-day placeholder; insert before the next section.
        old = re.sub("^" + re.escape(EMPTY) + "$", "", old, flags=re.M)
        heading = re.search(r"^## 今日进展[ \t]*$", old, re.M)
        following = re.search(r"^#{1,2} ", old[heading.end():], re.M) if heading else None
        pos = heading.end() + following.start() if following else len(old)
        prefix = old[:pos].rstrip() + "\n\n"
        if not heading:
            prefix += "## 今日进展\n\n"
        new = prefix + "\n".join(bullets) + "\n"
        if old[pos:]:
            new += "\n" + old[pos:]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(new)
    return True


def update_index(days):
    path = Path("daily/README.md")
    old = path.read_text()
    text = old
    for year, month in sorted({(d.year, d.month) for d in days}):
        target = f"{year}/{month:02}/"
        if f"]({target})" in text:
            continue
        link = f"[{month} 月]({target})"
        row = re.search(rf"^\| {year} \|.*?\|[ \t]*$", text, re.M)
        if row:
            text = text[:row.start()] + row.group().rstrip(" |") + " · " + link + " |" + text[row.end():]
        else:
            marker = "| --- | --- |"
            section = text.index("## 按月查看")
            pos = text.index(marker, section) + len(marker)
            text = text[:pos] + f"\n| {year} | {link} |" + text[pos:]
    if text != old:
        path.write_text(text)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-at", required=True, help="Workflow creation timestamp including timezone")
    args = parser.parse_args()
    run_time = datetime.fromisoformat(args.run_at.replace("Z", "+00:00"))
    if run_time.tzinfo is None:
        parser.error("--run-at requires a timezone")
    if git("rev-parse", "--is-shallow-repository").strip() != "false":
        raise RuntimeError("Full Git history is required")
    end = cutoff(run_time)
    commits = history(end)
    days = list(workdays(START, end))
    repo = "https://github.com/" + os.environ["GITHUB_REPOSITORY"]
    changed = [str(day) for day in days if report(day, commits[day], repo)]
    update_index(days)
    print(f"截至 {end}，已检查 {len(days)} 个工作日，更新 {len(changed)} 篇日报。", *changed)


if __name__ == "__main__":
    main()
