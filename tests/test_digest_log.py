import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import digest_log
import main
import state
from digest_config import get_config


def entry(aid, relevance=5):
    return dict(id=aid, relevance=relevance, category="ran", source="test",
                title=aid, link=f"https://example.test/{aid}", summary_vi="summary")


class DigestLogTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.directory = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.object(digest_log, "LOG_DIR", self.directory / "logs"))
        self.stack.enter_context(redirect_stdout(io.StringIO()))
        self.stack.enter_context(patch("socket.socket.connect", side_effect=AssertionError("network forbidden")))

    def records(self):
        return [json.loads(line) for f in (self.directory / "logs").glob("*.jsonl")
                for line in f.read_text().splitlines()]

    def test_schema_policy_statuses_and_shared_run_id(self):
        for session in ("morning", "afternoon"):
            config = get_config(session)
            context = dict(run_id=f"run-{session}", session=session,
                           cap=config.cap, quotas=config.quotas)
            candidates = [entry("sent"), entry("waiting"), entry("low", 2)]
            with patch.object(digest_log, "pipeline_versions", return_value={"git_sha": "verified"}):
                digest_log.log_run(candidates, candidates[:1], 3, **context)
                digest_log.log_expired([entry("expired")], **context)
            records = [r for r in self.records() if r["run_id"] == context["run_id"]]
            self.assertEqual([r["status"] for r in records], ["delivered", "pending", "rejected", "expired"])
            for r in records:
                self.assertEqual(r["schema_version"], 3)
                self.assertEqual(r["session"], session)
                self.assertEqual(r["cap"], config.cap)
                self.assertEqual(r["quotas"], dict(config.quotas))
                self.assertIn("logged_at", r)
                self.assertEqual(r["versions"]["git_sha"], "verified")
            self.assertEqual(records[0]["digest_rank"], 1)

    @patch.dict(os.environ, {"BOT_CODE_SHA": "a" * 40, "GITHUB_SHA": "b" * 40}, clear=True)
    @patch("digest_log.subprocess.check_output", side_effect=["a" * 40 + "\n", ""])
    def test_uses_verified_bot_code_sha(self, git):
        version = digest_log.pipeline_versions()
        self.assertEqual(version["git_sha"], "a" * 40)
        self.assertEqual(version["git_sha_source"], "BOT_CODE_SHA")

    @patch.dict(os.environ, {"GITHUB_SHA": "b" * 40}, clear=True)
    @patch("digest_log.subprocess.check_output", side_effect=["a" * 40, ""])
    def test_local_falls_back_to_verified_head(self, git):
        version = digest_log.code_version()
        self.assertEqual(version["git_sha"], "a" * 40)
        self.assertEqual(version["git_sha_source"], "local_HEAD")

    @patch.dict(os.environ, {"BOT_CODE_SHA": "b" * 40}, clear=True)
    @patch("digest_log.subprocess.check_output", side_effect=["a" * 40, ""])
    def test_mismatched_sha_is_not_reported_as_executed(self, git):
        version = digest_log.code_version()
        self.assertIsNone(version["git_sha"])
        self.assertEqual(version["git_sha_source"], "BOT_CODE_SHA_mismatch")

    @patch.dict(os.environ, {}, clear=True)
    @patch("digest_log.subprocess.check_output", side_effect=["a" * 40, " M main.py\n"])
    def test_dirty_checkout_reports_base_only(self, git):
        version = digest_log.code_version()
        self.assertIsNone(version["git_sha"])
        self.assertEqual(version["git_head"], "a" * 40)
        self.assertTrue(version["git_dirty"])

    @patch.dict(os.environ, {"GITHUB_SHA": "b" * 40, "BOT_CODE_SHA": "a" * 40}, clear=True)
    def test_unverifiable_revision_is_unknown(self):
        for failure in (FileNotFoundError(), subprocess.CalledProcessError(1, "git"),
                        subprocess.TimeoutExpired("git", 5)):
            with self.subTest(failure=failure), patch("digest_log.subprocess.check_output", side_effect=failure):
                self.assertIsNone(digest_log.code_version()["git_sha"])

    def test_append_preserves_history(self):
        log_dir = self.directory / "logs"
        log_dir.mkdir()
        path = log_dir / f"{datetime.now(timezone.utc):%Y-%m}.jsonl"
        history = '{"schema_version": 2, "article_id": "old"}\n'
        path.write_text(history)
        with patch.object(digest_log, "pipeline_versions", return_value={}):
            digest_log.log_run([entry("new")], [], 3, run_id="new", session="morning", cap=10,
                               quotas=get_config().quotas)
        self.assertTrue(path.read_text().startswith(history))
        self.assertEqual(len(path.read_text().splitlines()), 2)

    def test_log_write_failures_do_not_break_pipeline_or_expiry(self):
        # A regular file at LOG_DIR forces both real logger mkdir calls to fail.
        (self.directory / "logs").write_text("not a directory")
        state_file = self.directory / "state.json"
        state_file.write_text(json.dumps({"delivered": {}, "rejected": {}, "pending": {
            "expired": {"entry": entry("expired"),
                        "first_seen": (datetime.now(timezone.utc) - timedelta(days=4)).isoformat()}}}))
        with ExitStack() as stack:
            stack.enter_context(patch.object(state, "STATE_FILE", state_file))
            stack.enter_context(patch.object(state, "LEGACY_SEEN_FILE", self.directory / "seen.json"))
            stack.enter_context(patch("main.fetch_new_entries", return_value=[entry("new")]))
            stack.enter_context(patch("main.summarize_entries", side_effect=lambda x: x))
            stack.enter_context(patch.dict(os.environ, {
                "TELEGRAM_BOT_TOKEN": "test", "TELEGRAM_CHAT_ID": "test"}, clear=True))
            post = stack.enter_context(patch("send_telegram.requests.post", return_value=Mock(ok=True)))
            main.main(["--session", "afternoon"])
            saved = state.load_state()
        post.assert_called_once()
        self.assertIn("new", saved["delivered"])
        self.assertIn("expired", saved["rejected"])
        self.assertEqual(saved["pending"], {})

    def test_open_failure_is_nonfatal_for_both_loggers(self):
        context = dict(run_id="same", session="morning", cap=10, quotas=get_config().quotas)
        with patch.object(digest_log, "pipeline_versions", return_value={}), \
                patch("builtins.open", side_effect=OSError("disk full")):
            digest_log.log_run([entry("new")], [], 3, **context)
            digest_log.log_expired([entry("expired")], **context)
