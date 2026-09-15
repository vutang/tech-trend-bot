import copy
import io
import json
import os
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout, redirect_stderr
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import main
import state
from digest_config import get_config


def entry(i, relevance=5):
    return dict(id=str(i), link=f"https://example.test/{i}", title=f"Title {i}",
                summary_vi="Cached summary", category="embedded", relevance=relevance)


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        directory = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.state_file = directory / "state.json"
        self.stack.enter_context(patch.object(state, "STATE_FILE", self.state_file))
        self.stack.enter_context(patch.object(state, "LEGACY_SEEN_FILE", directory / "seen.json"))
        self.stack.enter_context(patch("digest_log.LOG_DIR", directory / "logs"))
        self.stack.enter_context(patch("digest_log.pipeline_versions", return_value={}))
        self.stack.enter_context(patch.dict(os.environ, {
            "TELEGRAM_BOT_TOKEN": "test", "TELEGRAM_CHAT_ID": "test"}, clear=True))
        self.stack.enter_context(redirect_stdout(io.StringIO()))
        self.fetch = self.stack.enter_context(patch("main.fetch_new_entries", return_value=[]))
        self.ai = self.stack.enter_context(patch("main.summarize_entries", side_effect=lambda x: x))
        self.post = self.stack.enter_context(patch("send_telegram.requests.post", return_value=Mock(ok=True)))
        # Fail closed if a test accidentally bypasses one of the mocks above.
        self.stack.enter_context(patch("socket.socket.connect", side_effect=AssertionError("network forbidden")))

    def test_cli_default_and_explicit_sessions(self):
        self.assertEqual(main.parse_args([]).session, "morning")
        for session in ("morning", "afternoon"):
            self.assertEqual(main.parse_args(["--session", session]).session, session)

    def test_cli_invalid_session_before_pipeline(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            main.main(["--session", "night"])
        self.assertEqual(error.exception.code, 2)
        self.fetch.assert_not_called()
        self.ai.assert_not_called()
        self.post.assert_not_called()

    def test_invalid_config_fails_before_fetch_or_ai(self):
        with patch("main.get_config", side_effect=ValueError("invalid quota")):
            with self.assertRaises(ValueError):
                main.main([])
        self.fetch.assert_not_called()
        self.ai.assert_not_called()

    def test_propagates_session_cap_quota(self):
        for session in ("morning", "afternoon"):
            with patch("main.send_digest", return_value=[]) as send:
                main.main(["--session", session])
            config = get_config(session)
            send.assert_called_once_with([], session=session, cap=config.cap, quotas=config.quotas)

    def test_morning_persist_then_afternoon_reloads_pending_without_ai(self):
        entries = [entry(i) for i in range(17)]
        self.fetch.side_effect = lambda known: [e for e in entries if e["id"] not in known]
        main.main(["--session", "morning"])
        morning = state.load_state()
        self.assertEqual(len(morning["delivered"]), 10)
        self.assertEqual(len(morning["pending"]), 7)
        self.ai.assert_called_once_with(entries)
        self.ai.reset_mock()
        first_seen = {k: v["first_seen"] for k, v in morning["pending"].items()}
        self.post.reset_mock()
        main.main(["--session", "afternoon"])
        afternoon = state.load_state()
        self.assertEqual(len(afternoon["delivered"]), 15)
        self.assertEqual(len(afternoon["pending"]), 2)
        self.ai.assert_not_called()
        self.assertEqual(self.fetch.call_args.args[0], state.known_ids(morning))
        sent = "\n".join(c.kwargs["data"]["text"] for c in self.post.call_args_list)
        for old_id in morning["delivered"]:
            self.assertNotIn(f"https://example.test/{old_id}\n", sent + "\n")
        for aid, rec in afternoon["pending"].items():
            self.assertEqual(rec["first_seen"], first_seen[aid])

    def seed_expired(self):
        old = entry("expired")
        data = {"delivered": {}, "rejected": {}, "pending": {
            old["id"]: {"first_seen": (datetime.now(timezone.utc) - timedelta(days=4)).isoformat(),
                        "entry": old}}}
        self.state_file.write_text(json.dumps(data))
        return data

    def test_empty_selection_still_rejects_and_expires(self):
        self.seed_expired()
        self.fetch.return_value = [entry("low", 2)]
        with patch("main.log_run") as log, patch("main.log_expired") as expired:
            main.main(["--session", "afternoon"])
        self.post.assert_not_called()
        saved = state.load_state()
        self.assertEqual(set(saved["rejected"]), {"low", "expired"})
        self.assertEqual(saved["pending"], {})
        self.assertEqual(saved["delivered"], {})
        self.assertEqual(log.call_args.args[1], [])
        self.assertEqual(log.call_args.kwargs, expired.call_args.kwargs)
        self.assertEqual(expired.call_args.kwargs["session"], "afternoon")

    def test_no_candidates_uses_run_context_and_cleans_pending(self):
        self.seed_expired()
        with patch("main.log_run") as log, patch("main.log_expired") as expired:
            main.main([])
        self.post.assert_not_called()
        self.ai.assert_not_called()
        self.assertEqual(state.load_state()["pending"], {})
        log.assert_called_once()
        self.assertEqual(log.call_args.args[:2], ([], []))
        self.assertEqual(log.call_args.kwargs, expired.call_args.kwargs)
        self.assertEqual(expired.call_args.kwargs["session"], "morning")

    def test_each_run_has_unique_id(self):
        with patch("main.log_run") as log:
            main.main([])
            main.main([])
        ids = [c.kwargs["run_id"] for c in log.call_args_list]
        self.assertEqual(len(set(ids)), 2)

    def test_send_failure_leaves_disk_and_memory_state_unchanged(self):
        initial = self.seed_expired()
        disk_before = self.state_file.read_bytes()
        self.fetch.return_value = [entry("new")]
        self.post.side_effect = RuntimeError("network error")
        with patch("main.load_state", return_value=initial), patch("main.log_run") as log:
            memory_before = copy.deepcopy(initial)
            with self.assertRaises(RuntimeError):
                main.main(["--session", "afternoon"])
        self.assertEqual(initial, memory_before)
        self.assertEqual(self.state_file.read_bytes(), disk_before)
        log.assert_not_called()

    def test_partial_send_does_not_mark_any_delivered(self):
        self.seed_expired()
        before = self.state_file.read_bytes()
        self.fetch.return_value = [dict(entry(i), summary_vi="x" * 1800) for i in range(5)]
        self.post.side_effect = [Mock(ok=True), RuntimeError("chunk two")]
        with self.assertRaises(RuntimeError):
            main.main(["--session", "afternoon"])
        self.assertEqual(self.post.call_count, 2)
        self.assertEqual(self.state_file.read_bytes(), before)

    def test_persist_failure_is_reported(self):
        self.fetch.return_value = [entry("new")]
        with patch("main.save_state", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                main.main([])
        self.post.assert_called_once()


class WorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import yaml
        cls.workflow = yaml.load(
            (Path(__file__).resolve().parents[1] / ".github/workflows/daily-digest.yml").read_text(),
            Loader=yaml.BaseLoader)
        cls.steps = cls.workflow["jobs"]["run-digest"]["steps"]

    def step(self, name):
        return next(s for s in self.steps if s.get("name") == name)

    def test_schedule_dispatch_concurrency_and_checkout(self):
        w = self.workflow
        self.assertEqual([s["cron"] for s in w["on"]["schedule"]], ["47 22 * * *", "47 10 * * *"])
        choice = w["on"]["workflow_dispatch"]["inputs"]["session"]
        self.assertEqual(choice["type"], "choice")
        self.assertEqual(choice["options"], ["morning", "afternoon"])
        self.assertEqual(choice["default"], "morning")
        self.assertEqual(w["concurrency"], {"group": "tech-trend-bot-production", "cancel-in-progress": "false"})
        self.assertEqual(w["jobs"]["run-digest"]["if"], "github.ref == 'refs/heads/master'")
        self.assertEqual(self.steps[0]["with"]["ref"], "master")

    def test_tests_precede_pipeline_and_do_not_receive_secrets(self):
        test_step = self.step("Unit tests (offline, no API secrets)")
        pipeline = self.step("Fetch, summarize & send")
        self.assertLess(self.steps.index(test_step), self.steps.index(pipeline))
        self.assertEqual(test_step["run"], "python -m unittest discover -s tests -v")
        self.assertEqual(pipeline["run"], 'python main.py --session "$DIGEST_SESSION"')
        self.assertNotIn("env", self.workflow)
        self.assertNotIn("env", self.workflow["jobs"]["run-digest"])
        for step in self.steps:
            if step is not pipeline:
                self.assertNotIn("secrets.", json.dumps(step))

    def test_all_shell_steps_parse(self):
        import subprocess
        for step in self.steps:
            if "run" in step:
                with self.subTest(step=step.get("name", step["run"])):
                    result = subprocess.run(["bash", "-n"], input=step["run"], text=True, capture_output=True)
                    self.assertEqual(result.returncode, 0, result.stderr)

    def test_resolver_uses_event_schedule_and_checkout_sha(self):
        import subprocess
        step = self.step("Resolve session and actual code revision")
        self.assertEqual(step["env"]["EVENT_SCHEDULE"], "${{ github.event.schedule }}")
        self.assertNotIn("date ", step["run"])
        cases = [("schedule", "47 22 * * *", "afternoon", "morning"),
                 ("schedule", "47 10 * * *", "morning", "afternoon"),
                 ("workflow_dispatch", "", "morning", "morning"),
                 ("workflow_dispatch", "", "afternoon", "afternoon"),
                 ("schedule", "unknown", "morning", None),
                 ("workflow_dispatch", "", "bad", None)]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_git = root / "git"
            fake_git.write_text('#!/bin/bash\n[[ "$*" == "rev-parse HEAD" ]] || exit 99\necho ' + "a" * 40 + '\n')
            fake_git.chmod(0o755)
            for event, schedule, manual, expected in cases:
                with self.subTest(event=event, schedule=schedule, manual=manual):
                    env_file = root / "env"
                    env_file.write_text("")
                    env = {"PATH": f"{root}:/usr/bin:/bin", "EVENT_NAME": event,
                           "EVENT_SCHEDULE": schedule, "MANUAL_SESSION": manual,
                           "GITHUB_ENV": str(env_file), "GITHUB_SHA": "b" * 40}
                    result = subprocess.run(["bash", "-c", step["run"]], env=env, cwd=root,
                                            text=True, capture_output=True)
                    if expected is None:
                        self.assertNotEqual(result.returncode, 0)
                        self.assertEqual(env_file.read_text(), "")
                    else:
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertEqual(env_file.read_text(), f"DIGEST_SESSION={expected}\nBOT_CODE_SHA={'a' * 40}\n")

    def test_persist_rebase_and_push_failures_are_not_swallowed(self):
        import subprocess
        script = self.step("Commit updated state and logs")["run"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_git = root / "git"
            fake_git.write_text('''#!/bin/bash
printf '%s\\n' "$*" >> "$GIT_CALLS"
case "$1" in
  diff) exit 1 ;;
  pull) [[ "$FAIL_AT" != pull ]] || exit 42 ;;
  push) [[ "$FAIL_AT" != push ]] || exit 43 ;;
esac
exit 0
''')
            fake_git.chmod(0o755)
            for fail_at, code in (("none", 0), ("pull", 42), ("push", 43)):
                with self.subTest(fail_at=fail_at):
                    calls = root / "calls"
                    calls.write_text("")
                    env = {"PATH": f"{root}:/usr/bin:/bin", "DIGEST_SESSION": "afternoon",
                           "FAIL_AT": fail_at, "GIT_CALLS": str(calls)}
                    result = subprocess.run(["bash", "-c", script], env=env, cwd=root,
                                            text=True, capture_output=True)
                    self.assertEqual(result.returncode, code, result.stderr)
                    commands = calls.read_text().splitlines()
                    self.assertIn("add state.json seen.json logs/", commands)
                    self.assertIn("commit -m update digest state (afternoon)", commands)
                    self.assertIn("pull --rebase origin master", commands)
                    self.assertFalse(any("reset" in c or "--force" in c for c in commands))
                    if fail_at == "pull":
                        self.assertNotIn("push origin HEAD:master", commands)
                    else:
                        self.assertIn("push origin HEAD:master", commands)
        artifact = self.step("Preserve state and logs for recovery on failure")
        self.assertIn("failure()", artifact["if"])
        self.assertIn("state.json", artifact["with"]["path"])
        self.assertIn("logs/", artifact["with"]["path"])
