import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import analyze


def record(aid, status, run, session=None, logged_at=None):
    r = dict(schema_version=2 if session is None else 3, article_id=aid,
             status=status, run_id=run, category="embedded", source="test",
             relevance=5, versions={})
    if session is not None:
        r["session"] = session
        r["cap"] = 10 if session == "morning" else 5
        r["quotas"] = {"ran": 2, "research": 2, "virt": 2} if session == "morning" else {
            "ran": 1, "research": 1, "virt": 1}
    if logged_at:
        r["logged_at"] = logged_at
    return r


def transition():
    return [record("a", "pending", "z-morning", "morning", "2026-09-14T00:00:00+00:00"),
            record("a", "delivered", "a-afternoon", "afternoon", "2026-09-14T10:00:00+00:00")]


class AnalyzeTests(unittest.TestCase):
    def test_load_old_new_and_mixed_months_excludes_ratings(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(analyze, "LOG_DIR", Path(tmp)):
            old = record("old", "delivered", "2026-08-01T00:00:00+00:00")
            new = transition()
            Path(tmp, "2026-08.jsonl").write_text(json.dumps(old) + "\n")
            Path(tmp, "2026-09.jsonl").write_text("\n".join(map(json.dumps, new)) + "\n")
            Path(tmp, "ratings.jsonl").write_text('\n'.join(map(json.dumps, [
                {"article_id": "a", "rating": 5}, {"article_id": "orphan", "rating": 1}])))
            Path(tmp, "2026-13.jsonl").write_text('{"article_id": "invalid-month"}\n')
            self.assertEqual(analyze.load_records("2026-08"), [old])
            self.assertEqual(analyze.load_records("2026-09"), new)
            self.assertEqual(analyze.load_records(), [old] + new)

    def test_month_argument_cannot_select_ratings(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(analyze, "LOG_DIR", Path(tmp)):
            for value in ("ratings", "../ratings", "*", "2026-13"):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    analyze.load_records(value)

    def test_legacy_session_not_inferred_from_clock(self):
        rs = [record("old", "delivered", "2026-09-14T22:47:00+00:00")]
        stats = analyze.session_stats(rs)
        self.assertEqual(stats["legacy/unknown"]["runs"], 1)
        self.assertEqual(stats["legacy/unknown"]["delivered"], 1)
        self.assertEqual(stats["morning"]["runs"], 0)

    def test_pending_morning_delivered_afternoon_one_outcome(self):
        rs = transition()
        with redirect_stdout(io.StringIO()):
            outcomes = analyze.dedupe(rs)
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(outcomes[0]["session"], "afternoon")
        stats = analyze.session_stats(rs)
        self.assertEqual(stats["morning"]["discovered"], 1)
        self.assertEqual(stats["morning"]["pending"], 0)
        self.assertEqual(stats["morning"]["delivered"], 0)
        self.assertEqual(stats["afternoon"]["delivered"], 1)
        self.assertEqual(stats["afternoon"]["discovered"], 0)

    def test_runs_survive_global_article_dedupe(self):
        stats = analyze.session_stats(transition())
        self.assertEqual(stats["morning"]["runs"], 1)
        self.assertEqual(stats["afternoon"]["runs"], 1)
        output = io.StringIO()
        with redirect_stdout(output):
            analyze.report(transition())
        self.assertIn("2 lần chạy", output.getvalue())
        self.assertIn("Bài (đã khử trùng lặp): 1", output.getvalue())
        self.assertIn("morning: runs=1", output.getvalue())
        self.assertIn("afternoon: runs=1", output.getvalue())

    def test_expired_candidate_share_one_run(self):
        rs = transition() + [record("b", "expired", "a-afternoon", "afternoon",
                                    "2026-09-14T10:00:01+00:00")]
        stats = analyze.session_stats(rs)
        self.assertEqual(stats["afternoon"]["runs"], 1)
        self.assertEqual(stats["afternoon"]["expired"], 1)

    def test_mixed_schema_report_and_unknown_label(self):
        rs = [record("legacy", "rejected", "2026-09-13T00:00:00+00:00")] + transition()
        output = io.StringIO()
        with redirect_stdout(output):
            analyze.report(rs)
        self.assertIn("3 lần chạy", output.getvalue())
        self.assertIn("legacy/unknown: runs=1", output.getvalue())
        self.assertIn("chưa có run-level record", output.getvalue())
        self.assertIn("không", output.getvalue())
        self.assertIn("đếm được run rỗng", output.getvalue())

    def test_empty_logs_do_not_infer_runs(self):
        self.assertEqual(sum(s["runs"] for s in analyze.session_stats([]).values()), 0)
        analyze.report([])

    def test_pending_to_expired_keeps_distinct_outcome(self):
        rs = transition()
        rs[1]["status"] = "expired"
        with redirect_stdout(io.StringIO()):
            stats = analyze._source_stats(analyze.dedupe(rs))
        self.assertEqual(stats["fetched"], 1)
        self.assertEqual(stats["expired"], 1)
        self.assertEqual(stats["rejected"], 0)

    def test_bad_json_line_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(analyze, "LOG_DIR", Path(tmp)):
            good = record("a", "pending", "2026-09-14T00:00:00+00:00")
            Path(tmp, "2026-09.jsonl").write_text("not json\n" + json.dumps(good))
            with redirect_stdout(io.StringIO()):
                self.assertEqual(analyze.load_records(), [good])

    def test_schema_one_is_supported(self):
        old = record("legacy", "pending", "2026-09-14T00:00:00+00:00")
        old["schema_version"] = 1
        stats = analyze.session_stats([old])
        self.assertEqual(stats["legacy/unknown"]["pending"], 1)
        with redirect_stdout(io.StringIO()):
            analyze.report([old])

    def test_new_run_ids_remain_compatible_with_rating_loader(self):
        import rate
        rs = [record("a", "pending", "2026-09-14T00:00:00+00:00-zzz", "morning",
                     "2026-09-14T00:00:01+00:00"),
              record("a", "delivered", "2026-09-14T10:00:00+00:00-aaa", "afternoon",
                     "2026-09-14T10:00:01+00:00")]
        with tempfile.TemporaryDirectory() as tmp, patch.object(rate, "LOG_DIR", Path(tmp)), \
                patch.object(rate, "RATINGS_FILE", Path(tmp) / "ratings.jsonl"):
            Path(tmp, "2026-09.jsonl").write_text("\n".join(map(json.dumps, rs)))
            delivered, rejected = rate.load_candidates()
        self.assertEqual(delivered, [rs[-1]])
        self.assertEqual(rejected, [])
