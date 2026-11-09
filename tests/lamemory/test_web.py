"""Web 工作台的隔离测试：临时数据库、替代模型，不访问真实会话。"""
from concurrent.futures import CancelledError
from dataclasses import replace
import json
import http.client
import threading
import time
from unittest.mock import Mock

from common import MemoryTestCase
from Agent.chatBot_with_memory.runner import ChatRunner
from Agent.chatBot_with_memory.web import APIError, WebApp, make_server
from core.providers import ModelConfig


class WebCases(MemoryTestCase):
    def setUp(self):
        super().setUp()
        self.app = WebApp(config=replace(self.memory.config, auto_process_jobs=False), user_id="alice", demo=True)

    def create(self):
        return self.app.mutate("/api/sessions", {})["session_id"]

    def wait_run(self, run_id):
        run = self.app.runs[run_id]
        deadline = time.monotonic() + 3
        with run.condition:
            while not run.done and time.monotonic() < deadline:
                run.condition.wait(timeout=.05)
        self.assertTrue(run.done)
        # The done event is visible just before the worker releases the mutation lock.
        self.assertTrue(self.app.run_lock.acquire(timeout=1))
        self.app.run_lock.release()
        return run

    def test_sessions_restore_raw_history_and_bound_user(self):
        session_id = self.create()
        run = self.wait_run(self.app.start(session_id, "你好")["run_id"])
        self.assertEqual(run.events[-1][1]["status"], "completed")
        history = self.app.session(session_id)["messages"]
        self.assertEqual([m["role"] for m in history], ["user", "assistant"])
        self.assertEqual(self.app.sessions()[0]["title"], "你好")
        self.app.mutate("/api/rename", {"session_id": session_id, "title": "测试会话"})
        restored = WebApp(config=self.app.config, user_id="alice", demo=True)
        self.assertEqual(restored.sessions()[0]["title"], "测试会话")
        self.assertEqual(restored.session(session_id)["messages"], history)
        other = WebApp(config=self.app.config, user_id="bob", demo=True)
        self.assertEqual(other.sessions(), [])
        with self.assertRaises(APIError):
            other.session(session_id)

    def test_tool_events_memory_provenance_correction_forgetting(self):
        session_id = self.create()
        run = self.wait_run(self.app.start(session_id, "记住：我喜欢中文回答")["run_id"])
        events = [event for event, data in run.events]
        self.assertIn("context", events)
        self.assertLess(events.index("tool_start"), events.index("tool_end"))
        memories = self.app.memories(session_id, "中文")
        self.assertEqual(len(memories), 1)
        memory_id = memories[0]["memory_id"]
        with self.app.memory(session_id) as memory:
            evidence = memory.inspect(memory_id)
        self.assertEqual(evidence["sources"][0]["payload"]["content"], "记住：我喜欢中文回答")
        changed = self.app.mutate("/api/memory/update", {"session_id": session_id, "memory_id": memory_id,
                                                       "content": "我喜欢英文回答"})
        self.assertEqual(changed["status"], "saved")
        new_id = changed["item"]["memory_id"]
        with self.app.memory(session_id) as memory:
            self.assertEqual(len(memory.inspect(new_id)["versions"]), 2)
        self.app.mutate("/api/memory/forget", {"session_id": session_id, "memory_id": new_id})
        self.assertEqual(self.app.memories(session_id), [])
        self.assertEqual(len(self.app.session(session_id)["messages"]), 4)
        self.assertTrue(self.app.session(session_id)["diagnostics"]["estimated_tokens"] > 0)

    def test_model_configuration_never_returns_or_reuses_key_for_new_route(self):
        data = {"provider": "deepseek", "model": "test-model", "api_key": "secret-test-key"}
        result = self.app.save_model(data)
        self.assertTrue(result["has_key"])
        self.assertNotIn("secret-test-key", json.dumps(self.app.bootstrap()))
        self.app.save_model({**data, "api_key": ""})
        self.assertEqual(self.app.model.api_key, "secret-test-key")
        result = self.app.save_model({**data, "api_key": "", "base_url": "http://localhost:8000/v1"})
        self.assertFalse(result["has_key"])
        with self.assertRaises(ValueError):
            self.app.save_model({**data, "context_window": 1024, "max_tokens": 4096})

    def test_missing_configuration_does_not_append_user_message(self):
        app = WebApp(config=self.app.config, user_id="alice")
        session_id = self.create()
        with self.assertRaises(APIError):
            app.start(session_id, "你好")
        self.assertEqual(app.session(session_id)["messages"], [])

    def test_cancellation_and_parallel_requests_leave_recoverable_history(self):
        entered, release = threading.Event(), threading.Event()
        def chat(**kwargs):
            entered.set()
            if not release.wait(timeout=2):
                raise TimeoutError("test release timeout")
            return {"role": "assistant", "content": "", "tool_calls": [{"id": "not-executed",
                    "type": "function", "function": {"name": "lamemory_store",
                    "arguments": '{"content":"不应保存"}'}}]}
        self.app.chat_override = chat
        first, second = self.create(), self.create()
        run_id = self.app.start(first, "停止测试")["run_id"]
        self.assertTrue(entered.wait(timeout=1))
        try:
            with self.assertRaises(APIError) as caught:
                self.app.start(second, "并发测试")
            self.assertEqual(caught.exception.status, 409)
            self.app.mutate("/api/stop", {"run_id": run_id})
        finally:
            release.set()
        run = self.wait_run(run_id)
        self.assertEqual(run.events[-1][1]["status"], "cancelled")
        self.assertEqual(self.app.memories(first), [])
        self.assertEqual(len(self.app.session(first)["messages"]), 2)
        self.app.chat_override = lambda **kwargs: {"role": "assistant", "content": "可继续对话"}
        self.wait_run(self.app.start(first, "继续")["run_id"])
        self.assertEqual(self.app.session(first)["messages"][-1]["payload"]["content"], "可继续对话")

    def test_error_is_visible_and_credentials_are_redacted(self):
        self.app.model = ModelConfig(model="fake", api_key="test-secret")
        self.app.chat_override = Mock(side_effect=RuntimeError("failed test-secret"))
        session_id = self.create()
        run = self.wait_run(self.app.start(session_id, "触发失败")["run_id"])
        self.assertEqual(run.events[-1][1]["status"], "failed")
        self.assertIn("[redacted]", json.dumps(run.events))
        self.assertNotIn("test-secret", json.dumps(run.events))
        self.assertEqual(len(self.app.session(session_id)["messages"]), 1)

    def test_runner_cancel_before_first_message_has_no_side_effect(self):
        chat = Mock()
        with self.assertRaises(CancelledError):
            ChatRunner(self.memory, chat=chat).send("不要发送", cancelled=lambda: True)
        chat.assert_not_called()
        self.assertEqual(self.memory.history(), [])

    def test_other_session_memory_is_not_visible(self):
        first, second = self.create(), self.create()
        with self.app.memory(first) as memory:
            memory.store("仅在当前会话可见", scope_kind="session")
        self.assertEqual(len(self.app.memories(first)), 1)
        self.assertEqual(self.app.memories(second), [])


class WebHTTPCases(MemoryTestCase):
    """本机 HTTP 集成测试；需要环境允许绑定 loopback 端口。"""
    def setUp(self):
        super().setUp()
        self.app = WebApp(config=replace(self.memory.config, auto_process_jobs=False), user_id="alice", demo=True)
        self.server = make_server(self.app, port=0)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        def close():
            self.server.shutdown()
            self.server.server_close()
            thread.join(timeout=2)
        self.addCleanup(close)

    def request(self, method, path, data=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        self.addCleanup(connection.close)
        body = json.dumps(data) if data is not None else None
        selected = {"Content-Type": "application/json", "X-MyClaude-Token": self.app.csrf}
        selected.update(headers or {})
        connection.request(method, path, body=body, headers=selected)
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read().decode()

    def test_static_bootstrap_and_no_external_runtime_assets(self):
        status, headers, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn('src="/app.js"', body)
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        status, headers, body = self.request("GET", "/api/bootstrap")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["csrf_token"], self.app.csrf)
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        self.assertEqual(self.request("GET", "/../core/.env")[0], 404)

    def test_mutation_requires_csrf_and_same_origin_host(self):
        self.assertEqual(self.request("POST", "/api/sessions", {}, {"X-MyClaude-Token": "wrong"})[0], 403)
        self.assertEqual(self.request("POST", "/api/sessions", {}, {"Origin": "https://example.invalid"})[0], 403)
        self.assertEqual(self.request("GET", "/api/bootstrap", headers={"Host": "example.invalid"})[0], 403)
        self.assertEqual(self.request("POST", "/api/sessions", {}, {"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.request("POST", "/api/sessions", [1])[0], 400)
        self.assertEqual(self.request("POST", "/api/sessions", {"large": "x" * 150001})[0], 413)
        self.assertEqual([s["session_id"] for s in self.app.sessions()], ["first"])

    def test_http_chat_sse_replay_and_saved_history(self):
        status, _, body = self.request("POST", "/api/sessions", {})
        self.assertEqual(status, 200)
        session_id = json.loads(body)["session_id"]
        status, _, body = self.request("POST", "/api/messages", {"session_id": session_id, "content": "记住：中文"})
        run_id = json.loads(body)["run_id"]
        self.assertEqual(status, 200)
        status, headers, body = self.request("GET", "/api/events?run_id=" + run_id)
        self.assertEqual(status, 200)
        self.assertIn("text/event-stream", headers["Content-Type"])
        self.assertIn("event: tool_start", body)
        self.assertIn("event: tool_end", body)
        self.assertIn('"status": "completed"', body)
        self.assertIn("event: done", body)
        event_count = len(self.app.runs[run_id].events)
        _, _, replay = self.request("GET", "/api/events?run_id=" + run_id, headers={"Last-Event-ID": str(event_count - 1)})
        self.assertIn("event: done", replay)
        self.assertNotIn("event: tool_start", replay)
        _, _, body = self.request("GET", "/api/session?session_id=" + session_id)
        self.assertEqual(len(json.loads(body)["messages"]), 4)
