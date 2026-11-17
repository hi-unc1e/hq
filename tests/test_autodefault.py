from concurrent.futures import ThreadPoolExecutor
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hqlib import autodefault, hookinstall  # noqa: E402

QUESTION = "选哪种方案？\nA. 保留布局\nB. 简化布局（最推荐）"


class AutoDefaultContract(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.transcript = self.root / "chat.jsonl"
        self.transcript.write_text("{}\n", encoding="utf-8")
        state_patch = patch.object(autodefault, "STATE_DIR", self.root / "state")
        state_patch.start()
        self.addCleanup(state_patch.stop)
        self.payload = {"session_id": "s1", "hook_event_name": "Stop",
                        "transcript_path": str(self.transcript),
                        "last_assistant_message": QUESTION}

    def test_default_is_applied_at_most_once(self):
        result = autodefault.evaluate(self.payload, wait_seconds=0)
        self.assertEqual(result["decision"], "block")
        self.assertEqual(autodefault.evaluate(self.payload, wait_seconds=0), {})

    def test_timer_does_not_close_a_reused_file_descriptor(self):
        held = []
        real_close = os.close

        def reuse_descriptor(fd):
            real_close(fd)
            if not held:
                held.append(os.open(self.transcript, os.O_RDONLY))

        try:
            with patch.object(autodefault.os, "close", side_effect=reuse_descriptor):
                self.assertEqual(autodefault.evaluate(self.payload, wait_seconds=0)["decision"], "block")
            os.fstat(held[0])
        finally:
            for fd in held:
                try:
                    real_close(fd)
                except OSError:
                    pass

    def test_missing_identity_transcript_and_recursive_stop_do_not_arm(self):
        for overrides in ({"session_id": ""}, {"transcript_path": "missing"},
                          {"stop_hook_active": True}):
            with self.subTest(overrides=overrides):
                self.assertEqual(autodefault.evaluate(self.payload | overrides, wait_seconds=0), {})

    def test_new_user_formats_are_detected(self):
        rows = [
            {"type": "event_msg", "payload": {"type": "user_message", "message": "yes"}},
            {"type": "response_item", "payload": {"type": "message", "role": "user"}},
            {"type": "user", "message": {"role": "user", "content": "yes"}},
            {"role": "user", "content": "yes"},
            {"message": {"role": "user", "content": "yes"}},
        ]
        for row in rows:
            with self.subTest(row=row):
                self.transcript.write_text(json.dumps(row) + "\n", encoding="utf-8")
                self.assertTrue(autodefault._has_new_user_message(self.transcript, 0))

    def test_unreadable_truncated_and_partial_transcript_cancel(self):
        self.assertIsNone(autodefault._has_new_user_message(self.root / "missing", 0))
        self.assertIsNone(autodefault._has_new_user_message(self.transcript, 999))
        self.transcript.write_bytes(b'{"type":')
        self.assertIsNone(autodefault._has_new_user_message(self.transcript, 0))

    def test_snapshot_client_user_prompt_cancels_without_file_append(self):
        result = []
        worker = threading.Thread(target=lambda: result.append(
            autodefault.evaluate(self.payload, wait_seconds=1, poll_seconds=0.01)))
        worker.start()
        deadline = time.monotonic() + 2
        while not list(autodefault.STATE_DIR.glob("*.lock")) and time.monotonic() < deadline:
            time.sleep(0.01)
        autodefault.evaluate({"hookEventName": "UserPromptSubmit", "sessionId": "s1"})
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result, [{}])

    def test_zcode_camel_case(self):
        payload = {"sessionId": "z1", "hookEventName": "Stop",
                   "lastAssistantMessage": QUESTION, "transcriptPath": str(self.transcript)}
        self.assertEqual(autodefault.evaluate(payload, wait_seconds=0)["decision"], "block")

    def test_risky_and_ambiguous_questions_never_arm(self):
        for word in ("删除", "购买", "发布", "权限", "凭据", "production", "overwrite"):
            self.assertIsNone(autodefault.parse_default(QUESTION.replace("简化布局", word)))
        self.assertIsNone(autodefault.parse_default(QUESTION + "\nB. 另一项"))
        self.assertIsNone(autodefault.parse_default(QUESTION.replace("保留布局", "保留布局（最推荐）")))
        self.assertEqual(autodefault.WAIT_SECONDS, 300)


class HookInstallation(unittest.TestCase):
    def test_all_agents_preserve_settings_upgrade_and_install_idempotently(self):
        for agent in hookinstall.AGENTS:
            with self.subTest(agent=agent), tempfile.TemporaryDirectory() as d:
                home = Path(d)
                path = hookinstall.config_path(agent, home)
                path.parent.mkdir(parents=True)
                other = {"type": "command", "command": "other-hook"}
                events = {"Stop": [{"matcher": "*", "hooks": [
                    other, {"type": "command", "command": "hq auto-default", "timeout": 30}]}]}
                config = {"unrelated": {"preserve": True},
                          "hooks": {"enabled": True, "events": events} if agent == "zcode" else events}
                path.write_text(json.dumps(config), encoding="utf-8")
                before = path.read_bytes()
                self.assertFalse(hookinstall.install(agent, check=True, home=home)["current"])
                self.assertEqual(path.read_bytes(), before)
                self.assertTrue(hookinstall.install(agent, home=home)["changed"])
                self.assertFalse(hookinstall.install(agent, home=home)["changed"])
                self.assertTrue(hookinstall.install(agent, check=True, home=home)["current"])
                with patch.object(hookinstall.sys, "executable", "/another/python"):
                    self.assertTrue(hookinstall.install(agent, check=True, home=home)["current"])
                after = json.loads(path.read_text())
                self.assertEqual(after["unrelated"], config["unrelated"])
                hooks = after["hooks"]["events"] if agent == "zcode" else after["hooks"]
                self.assertEqual(hooks["Stop"][0], {"matcher": "*", "hooks": [other]})
                self.assertEqual(len(hooks["Stop"]), 2)
                self.assertEqual(path.with_name(path.name + ".hq-backup").read_bytes(), before)
                spec = hooks["Stop"][1]["hooks"][0]
                if agent == "zcode":
                    self.assertEqual(spec["timeoutMs"], 330000)
                    command = [spec["command"], *spec["args"]]
                else:
                    self.assertEqual(spec["timeout"], 330)
                    command = spec["command"]
                # Execute exactly the installed handler outside the repository.
                out = subprocess.run(command, shell=agent != "zcode", cwd=home,
                                     input='{"hook_event_name":"Unknown"}', text=True,
                                     capture_output=True, timeout=10)
                self.assertEqual(out.returncode, 0, out.stderr)
                self.assertEqual(json.loads(out.stdout), {})

    def test_bad_json_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            path = hookinstall.config_path("qoder", home)
            path.parent.mkdir(parents=True)
            path.write_text("not json", encoding="utf-8")
            with self.assertRaises(ValueError):
                hookinstall.install("qoder", home=home)
            self.assertEqual(path.read_text(), "not json")

    def test_disabled_zcode_hooks_are_not_silently_enabled(self):
        with self.assertRaises(ValueError):
            hookinstall.desired_config({"hooks": {"enabled": False}}, "zcode")


@unittest.skipUnless(os.environ.get("HQ_REALTIME_TEST") == "1", "optional real five-minute wait")
class RealtimeTimer(unittest.TestCase):
    def test_three_clients_wait_full_five_minutes(self):
        with tempfile.TemporaryDirectory() as d, patch.object(autodefault, "STATE_DIR", Path(d) / "state"):
            transcript = Path(d) / "chat.jsonl"
            transcript.write_text("{}\n", encoding="utf-8")
            payloads = [
                {"session_id": "codex-test", "turn_id": "1"},
                {"session_id": "qoder-test", "request_set_id": "1"},
                {"sessionId": "zcode-test"},
            ]
            for payload in payloads:
                payload.update(hook_event_name="Stop", transcript_path=str(transcript),
                               last_assistant_message=QUESTION)
            start = time.monotonic()
            with ThreadPoolExecutor(max_workers=3) as pool:
                results = list(pool.map(autodefault.evaluate, payloads))
            self.assertGreaterEqual(time.monotonic() - start, 300)
            self.assertLess(time.monotonic() - start, 330)
            for result in results:
                self.assertEqual(result["decision"], "block")


if __name__ == "__main__":
    unittest.main()
