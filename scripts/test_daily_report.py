import os
import subprocess
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

import daily_report as report


class DailyReportTests(unittest.TestCase):
    def test_beijing_cutoffs(self):
        for run, expected in [
            ("2026-11-18T16:00:00+00:00", "2026-11-18"),
            ("2026-11-19T16:00:00+00:00", "2026-11-19"),
            ("2026-11-20T16:00:00+00:00", "2026-11-19"),  # Saturday manual run
            ("2026-11-22T16:00:00+00:00", "2026-11-20"),  # Monday midnight
            ("2026-11-23T16:00:00+00:00", "2026-11-23"),
        ]:
            self.assertEqual(report.cutoff(datetime.fromisoformat(run)), date.fromisoformat(expected))

    def test_backfill_and_rerun(self):
        with tempfile.TemporaryDirectory() as tmp:
            original = os.getcwd()
            self.addCleanup(os.chdir, original)
            os.chdir(tmp)
            Path("daily/2026/11").mkdir(parents=True)
            Path("daily/README.md").write_text("## 按月查看\n\n| 年份 | 月份 |\n| --- | --- |\n")
            draft = Path("daily/2026/11/2026-11-18.md")
            draft.write_text("# 手写标题\n\n## 今日进展\n\n今天的手写记录。\n\n## 待跟进\n\n保留这条。\n")
            report.git("init", "-q")
            report.git("config", "user.name", "Test")
            report.git("config", "user.email", "test@example.org")
            report.git("config", "commit.gpgsign", "false")

            def commit(day, subject, email="test@example.org"):
                report.git("add", ".")
                stamp = day + "+08:00"
                with patch.dict(os.environ, GIT_AUTHOR_DATE=stamp, GIT_COMMITTER_DATE=stamp,
                                GIT_AUTHOR_EMAIL=email):
                    report.git("commit", "--allow-empty", "-qm", subject)
                return report.git("rev-parse", "HEAD").strip()

            topic = Path("projects/search-quality/PROGRESS.md")
            topic.parent.mkdir(parents=True)
            topic.write_text("## 工作记录\n\n本次使用合成样例复核了排序指标的计算方法。\n")
            sha = commit("2026-11-18T23:59:59", "核对排序")
            weekend = commit("2026-11-21T00:00:00", "周末提交")
            commit("2026-11-20T00:00:00", report.AUTO_SUBJECT, report.BOT)
            with patch.dict(os.environ, GITHUB_REPOSITORY="owner/repo"), \
                 patch("sys.argv", ["daily_report.py", "--run-at", "2026-11-22T16:00:00Z"]):
                report.main()
                first = draft.read_text()
                self.assertIn(sha, first)
                self.assertIn("本次使用合成样例", first)
                self.assertIn("今天的手写记录。", first)
                self.assertIn("## 待跟进\n\n保留这条。", first)
                self.assertNotIn(weekend, first)
                self.assertLess(first.index(sha), first.index("## 待跟进"))
                for day in (19, 20):
                    self.assertIn(report.EMPTY, Path(f"daily/2026/11/2026-11-{day}.md").read_text())
                self.assertFalse(Path("daily/2026/11/2026-11-21.md").exists())
                report.main()
                self.assertEqual(draft.read_text(), first)
                self.assertEqual(Path("daily/README.md").read_text().count("2026/11/"), 1)


if __name__ == "__main__":
    unittest.main()
