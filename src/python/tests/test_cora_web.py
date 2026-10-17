"""HTTP checks without Azure calls: python -m unittest discover -s tests -v."""

import importlib.util
import asyncio
import json
import sys
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import patch

from agent_framework import AgentResponseUpdate, AgentSession, Content
from agent_framework.exceptions import ChatClientException
from azure.core.exceptions import ClientAuthenticationError
from fastapi.testclient import TestClient
import httpx

SOURCE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("cora_web", SOURCE / "cora-web.py")
web = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = web
spec.loader.exec_module(web)


class FakeAgent:
    def __init__(self):
        self.inputs = []

    def create_session(self):
        return AgentSession()

    async def run(self, message, *, session, stream):
        self.inputs.append(message)
        history = session.state.setdefault("history", [])
        history.append(message.text)
        if message.text == "fail":
            raise RuntimeError("private credential information")
        if message.text == "timeout":
            raise TimeoutError()
        if message.text == "signin":
            raise ClientAuthenticationError("private credential information")
        if message.text == "signin_wrapped":
            raise ChatClientException("provider failed") from ClientAuthenticationError("private credential information")
        if message.text == "stream_snapshot":
            yield AgentResponseUpdate(contents=[Content.from_text("Hello", merge_mode="append")])
            yield AgentResponseUpdate(contents=[Content.from_text("Hello world", merge_mode="replace")])
            return
        yield AgentResponseUpdate(contents=[Content.from_text(" | ".join(history))])


class ConfigurationTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_or_placeholder_endpoint_fails_before_connecting(self):
        for endpoint in ("", "https://YOUR-RESOURCE.openai.azure.com"):
            with patch.object(web.cora, "ENDPOINT", endpoint):
                with self.assertRaisesRegex(ValueError, "AZURE_OPENAI_ENDPOINT"):
                    async with web.cora.create_agent():
                        self.fail("An unconfigured agent must not connect")


class CoraWebTests(unittest.TestCase):
    def setUp(self):
        self.agent = FakeAgent()

        @asynccontextmanager
        async def fake_agent():
            yield self.agent

        self.factory = patch.object(web.cora, "create_agent", fake_agent)
        self.factory.start()
        self.client = TestClient(web.app)
        self.client.__enter__()
        self.conversation = self.new_conversation()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.factory.stop()

    def new_conversation(self):
        response = self.client.post("/api/conversations")
        self.assertEqual(response.status_code, 201)
        return response.json()["id"]

    def send(self, message, conversation=None, **kwargs):
        return self.client.post(
            f"/api/conversations/{conversation or self.conversation}/messages",
            data={"message": message}, **kwargs,
        )

    def events(self, response):
        self.assertEqual(response.status_code, 200)
        return [json.loads(line) for line in response.text.splitlines()]

    def test_serves_ui(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Cora", response.text)
        self.assertIn("text/html", response.headers["content-type"])

    def test_follow_up_keeps_history_and_other_conversations_are_isolated(self):
        self.events(self.send("I need paint"))
        follow_up = self.events(self.send("How much?"))
        self.assertIn({"type": "text", "text": "I need paint | How much?"}, follow_up)
        self.assertEqual(follow_up[-1], {"type": "done"})
        other = self.new_conversation()
        self.assertIn({"type": "text", "text": "Hello"}, self.events(self.send("Hello", other)))

    def test_reset_deletes_old_conversation(self):
        self.events(self.send("Old conversation"))
        self.assertEqual(self.client.delete(f"/api/conversations/{self.conversation}").status_code, 204)
        self.assertEqual(self.send("Follow up").status_code, 404)

    def test_rejects_blank_or_oversized_message_without_model_call(self):
        for message in ("   ", "x" * 4001):
            self.assertEqual(self.send(message).status_code, 422)
        self.assertEqual(self.agent.inputs, [])

    def test_rejects_invalid_image_without_model_call(self):
        for name, data, media_type in (
            ("test.svg", b"<svg></svg>", "image/svg+xml"),
            ("fake.png", b"not an image", "image/png"),
            ("big.png", b"\x89PNG\r\n\x1a\n" + b"0" * (5 * 1024 * 1024), "image/png"),
        ):
            self.assertIn(self.send("Look", files={"image": (name, data, media_type)}).status_code, (413, 415))
        self.assertEqual(self.agent.inputs, [])

    def test_image_is_forwarded_as_multimodal_content(self):
        import base64

        image = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aZ1sAAAAASUVORK5CYII=")
        self.events(self.send("Look at my room", files={"image": ("room.png", image, "image/png")}))
        contents = self.agent.inputs[-1].contents
        self.assertEqual(contents[0].text, "Look at my room")
        self.assertEqual(contents[1].type, "data")
        self.assertEqual(contents[1].media_type, "image/png")

    def test_failed_turn_does_not_pollute_history_or_expose_internal_error(self):
        self.events(self.send("Hello"))
        with self.assertLogs(web.logger, level="ERROR"):
            failed = self.events(self.send("fail"))
        self.assertEqual(failed[-1]["type"], "error")
        self.assertNotIn("private credential", json.dumps(failed))
        self.assertIn({"type": "text", "text": "Hello | Again"}, self.events(self.send("Again")))

    def test_timeout_is_reported_and_next_turn_can_run(self):
        with self.assertLogs(web.logger, level="ERROR"):
            timed_out = self.events(self.send("timeout"))
        self.assertEqual(timed_out[-1]["type"], "error")
        self.assertIn({"type": "text", "text": "Again"}, self.events(self.send("Again")))

    def test_missing_azure_login_gives_actionable_message(self):
        for message in ("signin", "signin_wrapped"):
            with self.assertLogs(web.logger, level="ERROR"):
                response = self.events(self.send(message))
            self.assertEqual(response[-1]["type"], "error")
            self.assertIn("az login", response[-1]["text"])
            self.assertNotIn("private credential", json.dumps(response))

    def test_sdk_final_snapshot_replaces_instead_of_duplicating_streamed_text(self):
        events = self.events(self.send("stream_snapshot"))
        self.assertIn({"type": "text", "text": "Hello"}, events)
        self.assertIn({"type": "replace", "text": "Hello world"}, events)
        self.assertEqual(events[-1], {"type": "done"})

    def test_expired_conversation_requires_new_chat(self):
        web.app.state.conversations[self.conversation].last_used = web.monotonic() - web.SESSION_TTL - 1
        self.assertEqual(self.send("Hello").status_code, 404)

    def test_concurrent_send_and_reset_are_rejected_and_turn_limit_is_enforced(self):
        async def check():
            started, release = asyncio.Event(), asyncio.Event()

            async def slow_reply(message, *, session, stream):
                started.set()
                await release.wait()
                yield AgentResponseUpdate(contents=[Content.from_text("Finished")])

            self.agent.run = slow_reply
            web.app.state.conversations[self.conversation].turns = 39
            url = f"/api/conversations/{self.conversation}"
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web.app), base_url="http://test") as client:
                pending = asyncio.create_task(client.post(f"{url}/messages", data={"message": "First"}))
                try:
                    await asyncio.wait_for(started.wait(), timeout=2)
                    self.assertEqual((await client.post(f"{url}/messages", data={"message": "Second"})).status_code, 409)
                    self.assertEqual((await client.delete(url)).status_code, 409)
                finally:
                    release.set()
                    await pending
                self.assertEqual(web.app.state.conversations[self.conversation].turns, 40)
                self.assertEqual((await client.post(f"{url}/messages", data={"message": "Over limit"})).status_code, 409)

        asyncio.run(check())


if __name__ == "__main__":
    unittest.main()
