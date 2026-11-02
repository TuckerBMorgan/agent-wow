import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location("client_runner", Path(__file__).with_name("client_runner.py"))
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class GrokHarnessTests(unittest.TestCase):
    def test_subscription_auth_and_prompt_are_loaded_without_api_fallback(self):
        auth = {"issuer::client": {"auth_mode": "oidc", "key": "test-access", "refresh_token": "test-refresh"}}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            (workspace / "tmp").mkdir(parents=True)
            home = root / "home"
            home.mkdir()
            params = {"harness": "grok", "model": "grok-4.7", "effort": "medium", "prompt": "create a human warrior"}
            with patch.object(runner, "WORKSPACE", workspace), \
                    patch.object(runner, "agent_env", return_value={"HOME": str(home)}), \
                    patch.object(runner.subprocess, "run"):
                command, env = runner.harness(params, json.dumps(auth), "example.invalid")
            cache = home / ".grok/auth.json"
            self.assertEqual(json.loads(cache.read_text()), auth)
            self.assertEqual(cache.stat().st_mode & 0o777, 0o600)
            prompt = Path(command[command.index("--prompt-file") + 1])
            self.assertEqual(prompt.read_text(), params["prompt"])
            self.assertEqual(env["GROK_HOME"], str(home / ".grok"))
            self.assertEqual(env["GROK_DISABLE_AUTOUPDATER"], "1")
            self.assertNotIn("XAI_API_KEY", env)

    def test_api_key_only_credentials_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "subscription login"):
            runner.harness({"harness": "grok"}, '{"api_key":"test-only"}', "example.invalid")

    def test_inherited_provider_settings_do_not_reach_other_harnesses(self):
        with patch.dict(os.environ, {"XAI_API_KEY": "test-only", "GROK_HOME": "/untrusted", "GROK_MODELS_BASE_URL": "untrusted"}):
            env = runner.agent_env("example.invalid")
        self.assertFalse(any(key.startswith("GROK_") for key in env))
        self.assertNotIn("XAI_API_KEY", env)

    def test_fragmented_replies_and_cumulative_tool_output_are_preserved(self):
        events = [
            {"type": "text", "data": "Checking now."},
            {"type": "tool_call", "toolCallId": "tool-1", "rawInput": {"command": "printf ready"}},
            {"type": "tool_call_update", "toolCallId": "tool-1", "rawOutput": {"output": "ready"}},
            {"type": "tool_call_update", "toolCallId": "tool-1", "rawOutput": {"output": "ready"}},
            {"type": "text", "data": "TOOL"},
            {"type": "text", "data": "_OK"},
            {"type": "end", "stopReason": "end_turn"},
        ]
        log, transcript, messages = io.StringIO(), io.StringIO(), io.StringIO()
        with tempfile.TemporaryDirectory() as directory, patch.object(runner, "LOGS", Path(directory)):
            runner.record_output(io.StringIO("\n".join(json.dumps(event) for event in events)), log, transcript, messages)
            self.assertEqual((Path(directory) / "final-report.txt").read_text(), "TOOL_OK\n")
        self.assertEqual([json.loads(line) for line in transcript.getvalue().splitlines()], events)
        self.assertEqual(messages.getvalue(), "Assistant:\nChecking now.\n\nAssistant:\nTOOL_OK\n\n")
        self.assertEqual(log.getvalue().count("\nready\n"), 1)

    def test_existing_codex_and_claude_messages_are_unchanged(self):
        events = [
            {"type": "item.completed", "item": {"type": "agent_message", "text": "Codex reply"}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "Claude reply"}]}},
        ]
        messages = io.StringIO()
        runner.record_output(io.StringIO("\n".join(json.dumps(event) for event in events)), io.StringIO(), io.StringIO(), messages)
        self.assertEqual(messages.getvalue(), "Assistant:\nCodex reply\n\nAssistant:\nClaude reply\n\n")


if __name__ == "__main__":
    unittest.main()
