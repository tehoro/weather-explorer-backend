"""The OpenAI sweep path (openai_agent.py) and the provider switch (agent.py), with fakes: no API calls."""
import contextlib
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import openai_agent as engine


class Sweep(unittest.TestCase):
    """openai_agent.sweep: publishes with the real publisher only after everything checked out."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.feed = self.root / "highlights" / "latest.json"
        self.feed.parent.mkdir()
        self.feed.write_text(json.dumps({"generated_utc": "2026-11-01T03:40Z", "items": []}))
        (self.feed.parent / "explainer_history.json").write_text("[]")
        self.runs = self.root / "runs"
        self.start = Mock(return_value="sess_1")
        self.finish = Mock(side_effect=self.fake_finish)
        self.outcome = {"success": True, "outcome": "ok", "validation": {"ok": True}}
        self.publish = Mock(return_value=subprocess.CompletedProcess([], 0, "published 15 highlights", ""))
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        for name, value in {"RUNS": self.runs, "FEED": self.feed, "client": Mock(), "start": self.start,
                            "finish": self.finish, "publisher": self.publish}.items():
            stack.enter_context(patch.object(engine, name, value))

    def fake_finish(self, c, run, minutes, python, log, keep_session=False):
        (run / "artifacts/highlights").mkdir(parents=True, exist_ok=True)
        (run / "artifacts/highlights/draft.json").write_text("{}")
        (run / "artifacts/report.txt").write_text("COMPLETE")
        (run / "result.json").write_text(json.dumps(self.outcome))
        return self.outcome

    def new_run_started(self):
        self.start.side_effect = lambda c, run, *a: (run / "session.json").write_text(
            json.dumps({"id": "sess_1", "started_unix": 0}))

    def test_success_publishes_the_draft_with_the_real_publisher(self):
        self.new_run_started()
        report = engine.sweep(print)
        self.assertEqual(report, "COMPLETE")
        run = next(self.runs.iterdir())
        self.assertEqual(json.loads((run / "inputs/latest.json").read_text())["generated_utc"], "2026-11-01T03:40Z")
        self.publish.assert_called_once()
        self.assertEqual(self.publish.call_args.args[1], run / "artifacts/highlights/draft.json")

    def test_failed_run_raises_and_publishes_nothing(self):
        self.new_run_started()
        self.outcome = {"success": False, "outcome": "session failed", "validation": {"ok": False, "reason": "no draft"}}
        with self.assertRaisesRegex(RuntimeError, "not published"):
            engine.sweep(print)
        self.publish.assert_not_called()

    def test_live_publisher_refusing_raises(self):
        self.new_run_started()
        self.publish.return_value = subprocess.CompletedProcess([], 1, "NOT PUBLISHED", "")
        with self.assertRaisesRegex(RuntimeError, "live publisher refused"):
            engine.sweep(print)

    def test_daily_limit_stops_before_creating_a_session(self):
        today = engine.datetime.now(engine.timezone.utc).strftime("%Y%m%d")
        for k in range(engine.RUNS_PER_DAY):
            run = self.runs / f"{today}T00000{k}Z"
            run.mkdir(parents=True)
            (run / "session.json").write_text("{}")
            (run / "result.json").write_text("{}")
        with self.assertRaisesRegex(RuntimeError, "daily limit"):
            engine.sweep(print)
        self.start.assert_not_called()

    def test_unfinished_run_is_resumed_not_paid_for_again(self):
        run = self.runs / "20261101T050000Z"
        (run / "inputs").mkdir(parents=True)
        (run / "session.json").write_text(json.dumps({"id": "sess_0", "started_unix": engine.time.time() - 600}))
        engine.sweep(print)
        self.start.assert_not_called()
        self.assertEqual(self.finish.call_args.args[1], run)

    def test_run_unfinished_for_hours_is_abandoned_and_a_new_one_started(self):
        old = self.runs / "20261101T050000Z"
        (old / "inputs").mkdir(parents=True)
        (old / "session.json").write_text(json.dumps({"id": "sess_0", "started_unix": engine.time.time() - 4 * 3600}))
        self.new_run_started()
        engine.sweep(print)
        self.start.assert_called_once()
        self.assertIn("abandoned", json.loads((old / "result.json").read_text())["outcome"])


class Request(unittest.TestCase):
    """openai_agent.research: a requested highlight, published with the publisher's --request mode."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.feed = self.root / "highlights" / "latest.json"
        self.feed.parent.mkdir()
        self.feed.write_text(json.dumps({"items": []}))
        self.out = self.root / "requests" / "abc.json"
        self.found = True
        self.start = Mock(side_effect=lambda c, run, *a: (run / "session.json").write_text(
            json.dumps({"id": "sess_r", "started_unix": engine.time.time()})))
        self.publish = Mock(return_value=subprocess.CompletedProcess([], 0, "published the requested highlight", ""))
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        for name, value in {"RUNS": self.root / "runs", "REQUEST_RUNS": self.root / "runs" / "requests",
                            "FEED": self.feed, "client": Mock(), "start": self.start,
                            "finish": Mock(side_effect=self.fake_finish), "publisher": self.publish}.items():
            stack.enter_context(patch.object(engine, name, value))

    def fake_finish(self, c, run, minutes, python, log, keep_session=False, request=False):
        self.assertTrue(request)
        (run / "artifacts").mkdir(parents=True, exist_ok=True)
        if self.found:
            (run / "artifacts/requests").mkdir()
            (run / "artifacts/requests/draft.json").write_text("{}")
            (run / "artifacts/report.txt").write_text("Found it.\nRESULT: published")
            return {"success": True, "outcome": "ok", "validation": {"ok": True}}
        (run / "artifacts/report.txt").write_text("RESULT: none - no storm by that name this week")
        return {"success": False, "outcome": "ok", "validation": {"ok": False, "reason": "no draft.json"}}

    def test_found_event_is_published_to_the_requested_path(self):
        report = engine.research("Storm over Italy", self.out, print)
        self.assertTrue(report.endswith("RESULT: published"))
        self.assertEqual(self.publish.call_args.kwargs["request_out"], self.out)
        self.assertIn("<request>Storm over Italy</request>", self.start.call_args.args[6])

    def test_no_such_event_returns_the_reason_and_publishes_nothing(self):
        self.found = False
        report = engine.research("Snow in the Sahara", self.out, print)
        self.assertIn("RESULT: none - no storm", report)
        self.publish.assert_not_called()

    def test_failed_request_raises(self):
        with patch.object(engine, "finish", return_value={"success": False, "outcome": "session failed",
                                                          "validation": {"ok": False, "reason": "no draft"}}):
            with self.assertRaisesRegex(RuntimeError, "not published"):
                engine.research("Storm over Italy", self.out, print)
        self.publish.assert_not_called()


class Download(unittest.TestCase):
    """Only allowlisted files from /workspace/outputs, at safe paths, within the size limits."""

    def test_unsafe_unexpected_and_oversized_files_are_skipped(self):
        def art(path, size=10):
            return Mock(id=path, path=path, size_bytes=size, to_dict=lambda: {"path": path})
        arts = [art("/workspace/outputs/highlights/draft.json"), art("/workspace/outputs/../../etc/passwd.json"),
                art("/workspace/backend/.env"), art("/workspace/outputs/run.sh"),
                art("/workspace/outputs/big.json", engine.MAX_FILE + 1)]
        c = MagicMock()
        c.beta.agents.sessions.artifacts.list.return_value = arts
        stream = c.beta.agents.sessions.artifacts.with_streaming_response.content.return_value.__enter__.return_value
        stream.stream_to_file.side_effect = lambda dest: Path(dest).write_bytes(b"x" * 10)
        with tempfile.TemporaryDirectory() as tmp:
            got = engine.download(c, "sess", Path(tmp), print)
            self.assertEqual(got["downloaded"], ["highlights/draft.json"])
            self.assertEqual(len(got["skipped"]), 4)
            self.assertFalse(any(Path(tmp).rglob("passwd*")))


class Provider(unittest.IsolatedAsyncioTestCase):
    """agent.py: Claude unless WEATHER_AGENT_PROVIDER=openai; no fallback between them."""

    def tearDown(self):
        self.load(None)   # leave agent.py as the other tests expect it

    def load(self, value):
        with patch.dict(os.environ, {"WEATHER_AGENT_PROVIDER": value} if value else {}, clear=False):
            if not value:
                os.environ.pop("WEATHER_AGENT_PROVIDER", None)
            import agent
            return importlib.reload(agent)

    async def test_default_is_claude(self):
        agent = self.load(None)
        with patch.object(agent, "_run", new_callable=AsyncMock, return_value="claude report") as claude, \
             patch.object(agent.openai_agent, "run_refresh", new_callable=AsyncMock) as openai:
            self.assertEqual(await agent.run_refresh(print), "claude report")
        claude.assert_awaited_once()
        openai.assert_not_awaited()
        self.assertEqual(agent.describe()["provider"], "claude")

    async def test_openai_when_chosen_and_no_claude_fallback(self):
        agent = self.load("openai")
        with patch.object(agent, "_run", new_callable=AsyncMock) as claude, \
             patch.object(agent.openai_agent, "run_refresh", new_callable=AsyncMock, side_effect=RuntimeError("API down")):
            with self.assertRaises(RuntimeError):
                await agent.run_refresh(print)
        claude.assert_not_awaited()
        self.assertEqual(agent.describe()["provider"], "openai")

    async def test_requests_follow_the_provider(self):
        agent = self.load("openai")
        with patch.object(agent, "_run", new_callable=AsyncMock) as claude, \
             patch.object(agent.openai_agent, "run_request", new_callable=AsyncMock, return_value="RESULT: published") as openai:
            self.assertEqual(await agent.run_request("Storm", Path("d.json"), Path("o.json"), print), "RESULT: published")
        claude.assert_not_awaited()
        openai.assert_awaited_once_with("Storm", Path("o.json"), print)
        agent = self.load(None)
        with patch.object(agent, "_run", new_callable=AsyncMock, return_value="RESULT: none - x") as claude:
            await agent.run_request("Storm", Path("d.json"), Path("o.json"), print)
        claude.assert_awaited_once()

    def test_unknown_provider_is_refused(self):
        with self.assertRaises(SystemExit):
            self.load("gemini")


if __name__ == "__main__":
    unittest.main()
