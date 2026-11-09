"""本地 Web 工作台：同源 HTTP API、运行事件和隔离的 LaMemory 会话。"""
from __future__ import annotations

import argparse
from concurrent.futures import CancelledError
from contextlib import contextmanager
from dataclasses import replace
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import tempfile
import threading
from urllib.parse import parse_qs, urlsplit
import uuid

from Agent.chatBot_with_memory.main import SYSTEM_PROMPT
from Agent.chatBot_with_memory.runner import ChatRunner
from core.lamemory import LaMemoryConfig, LaMemoryService
from core.lamemory.providers import ChatMemoryProvider
from core.llm import call_llm
from core.providers import ModelConfig, create_default_registry
from tools import get_tools, get_default_registry

STATIC = Path(__file__).with_name("web_static")


class APIError(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message


class Run:
    def __init__(self, session_id):
        self.id = uuid.uuid4().hex
        self.session_id = session_id
        self.events = []
        self.condition = threading.Condition()
        self.cancel = threading.Event()
        self.done = False

    def emit(self, event, data):
        with self.condition:
            self.events.append((event, data))
            if event == "done":
                self.done = True
            self.condition.notify_all()


class WebApp:
    def __init__(self, *, config=None, user_id="local-user", model_config=None, chat=None, demo=False):
        self.config = config or LaMemoryConfig()
        self.user_id = LaMemoryService._identity(user_id)
        self.model = model_config
        self.chat_override = chat
        self.demo = demo
        self.csrf = secrets.token_urlsafe(32)
        self.lock = threading.RLock()
        self.run_lock = threading.Lock()
        self.runs = {}
        self.diagnostics = {}
        with self.memory() as memory:
            with memory.db.connection:
                memory.db.execute("""CREATE TABLE IF NOT EXISTS lamemory_web_sessions (
                    user_id TEXT NOT NULL, session_id TEXT NOT NULL, title TEXT NOT NULL,
                    PRIMARY KEY(user_id, session_id))""")

    @contextmanager
    def memory(self, session_id=None, *, model=None):
        provider = ChatMemoryProvider(model_config=model) if model else None
        # Overview connections do not create empty sessions on every refresh.
        memory = LaMemoryService(self.config, user_id=self.user_id,
                                 session_id=session_id or "__lamemory_web_overview__", provider=provider)
        try:
            yield memory
        finally:
            memory.close()

    def require_session(self, session_id):
        with self.memory() as memory:
            row = memory.db.one("SELECT 1 FROM lamemory_sessions WHERE user_id=? AND session_id=?",
                                (self.user_id, session_id))
        if not row or session_id == "__lamemory_web_overview__":
            raise APIError(404, "会话不存在")

    def public_model(self):
        model = self.model
        return {"provider": model.provider if model else "openai-compatible",
                "model": model.model if model else "", "base_url": (model.base_url or "") if model else "",
                "has_key": bool(model and model.api_key), "ready": self.demo or bool(model and model.api_key),
                "context_window": model.context_window if model else None,
                "max_tokens": model.max_tokens if model else self.config.reserved_output}

    def bootstrap(self):
        return {"csrf_token": self.csrf, "model": self.public_model(), "demo": self.demo,
                "workspace": Path.cwd().name, "project": self.config.project_id,
                "providers": create_default_registry().list_providers(),
                "tools": [{"name": t.name, "description": t.description} for t in get_tools()],
                "skills": get_default_registry().list_skills()}

    def sessions(self):
        with self.memory() as memory:
            rows = memory.db.rows("""SELECT s.session_id, s.created_at, w.title,
                (SELECT payload_json FROM lamemory_messages m WHERE m.user_id=s.user_id
                 AND m.session_id=s.session_id AND m.role='user' ORDER BY seq LIMIT 1) AS first_message,
                COALESCE((SELECT MAX(created_at) FROM lamemory_messages m WHERE m.user_id=s.user_id
                 AND m.session_id=s.session_id), s.created_at) AS updated_at
                FROM lamemory_sessions s LEFT JOIN lamemory_web_sessions w
                ON w.user_id=s.user_id AND w.session_id=s.session_id
                WHERE s.user_id=? AND s.session_id!='__lamemory_web_overview__'
                ORDER BY updated_at DESC""", (self.user_id,))
        with self.lock:
            active = {r.session_id for r in self.runs.values() if not r.done}
        for row in rows:
            first = json.loads(row.pop("first_message") or "{}")
            row["title"] = row["title"] or (first.get("content") or "新会话")[:48]
            row["running"] = row["session_id"] in active
        return rows

    def session(self, session_id):
        self.require_session(session_id)
        with self.memory(session_id) as memory:
            result = {"session_id": session_id, "messages": memory.history(), "jobs": memory.jobs()}
        with self.lock:
            result["run_id"] = next((r.id for r in self.runs.values()
                                     if r.session_id == session_id and not r.done), None)
            result["diagnostics"] = self.diagnostics.get(session_id, {})
        return result

    def memories(self, session_id, query=""):
        self.require_session(session_id)
        with self.memory(session_id) as memory:
            if query.strip():
                return [hit.item.to_dict() for hit in memory.search(query, top_k=100)]
            rows = memory.db.rows("""SELECT * FROM lamemory_items WHERE user_id=? AND status IN ('active','pending')
                AND ((scope_kind='user' AND scope_key=?) OR (scope_kind='session' AND scope_key=?)
                OR (scope_kind='project' AND scope_key=?)) ORDER BY updated_at DESC LIMIT 100""",
                (self.user_id, self.user_id, session_id, self.config.project_id))
            return [memory._item(row).to_dict() for row in rows]

    def save_model(self, data):
        registry = create_default_registry()
        provider = data.get("provider", "openai-compatible")
        registry.get(provider)
        with self.lock:
            previous = self.model
            # Blank means retain a key only for the same route, never leak it to a new endpoint.
            base_url = data.get("base_url", "").strip() or None
            key = data.get("api_key", "").strip() or (
                previous.api_key if previous and previous.provider == provider and previous.base_url == base_url else None)
            model = ModelConfig(provider=provider, model=data.get("model", "").strip(), api_key=key,
                                base_url=base_url, context_window=int(data.get("context_window") or 32768),
                                max_tokens=int(data.get("max_tokens") or 4096))
            replace(self.config, context_window=model.context_window, reserved_output=model.max_tokens)
            self.model = model
        return self.public_model()

    def start(self, session_id, content):
        self.require_session(session_id)
        if not isinstance(content, str) or not content.strip() or len(content) > 32000:
            raise APIError(400, "请输入 1–32000 字符的消息")
        with self.lock:
            model = self.model
            if not self.demo and not self.chat_override and not (model and model.api_key):
                raise APIError(400, "请先在设置中配置模型和 API key")
            if not self.run_lock.acquire(blocking=False):
                raise APIError(409, "已有任务正在运行，请等待完成或停止后再发送")
            run = Run(session_id)
            # Keep bounded completed run metadata; raw evidence lives in SQLite.
            completed = [key for key, value in self.runs.items() if value.done]
            for key in completed[:-10]:
                del self.runs[key]
            self.runs[run.id] = run
        threading.Thread(target=self._work, args=(run, content.strip(), model), daemon=True).start()
        return {"run_id": run.id}

    def _work(self, run, content, model):
        completion = {"status": "failed"}
        try:
            config = replace(self.config, context_window=model.context_window or self.config.context_window,
                             reserved_output=model.max_tokens) if model else self.config
            chat = self.chat_override or (demo_chat if self.demo else partial(call_llm, config=model))
            with LaMemoryService(config, user_id=self.user_id, session_id=run.session_id,
                                 provider=None if self.demo else ChatMemoryProvider(model_config=model)) as memory:
                runner = ChatRunner(memory, chat=chat, tools=get_tools(), system_prompt=SYSTEM_PROMPT)

                def emit(event, data):
                    if event == "context":
                        with self.lock:
                            self.diagnostics[run.session_id] = data
                    run.emit(event, data)

                result = runner.send(content, timeout=180, on_event=emit, cancelled=run.cancel.is_set)
                for error in result["errors"]:
                    run.emit("warning", {"message": error})
                # Main reply is already visible before bounded extraction, matching the terminal flow.
                if config.auto_process_jobs and not self.demo and not run.cancel.is_set() and not result["errors"]:
                    run.emit("status", {"text": "回复已生成，正在整理记忆"})
                    try:
                        jobs = memory.process_pending(max_jobs=1)
                        for job in jobs:
                            if job["status"] != "completed":
                                run.emit("warning", {"message": "记忆任务未完成，可在记忆面板检查并重试"})
                    except Exception:
                        run.emit("warning", {"message": "记忆整理失败，原始对话已保留，可稍后重试"})
                completion = {"status": "completed", "usage": runner.usage_events}
        except CancelledError as exc:
            run.emit("warning", {"message": str(exc)})
            completion = {"status": "cancelled"}
        except Exception as exc:
            # Provider exceptions can carry request details; keep credentials out of browser events.
            message = str(exc)
            if model and model.api_key:
                message = message.replace(model.api_key, "[redacted]")
            run.emit("error", {"message": message[:1200] or "本轮未完成，原始对话已保留"})
        finally:
            self.run_lock.release()
            run.emit("done", completion)

    def mutate(self, path, data):
        if path == "/api/settings":
            return self.save_model(data)
        if path == "/api/sessions":
            with self.memory("session_" + uuid.uuid4().hex) as memory:
                return {"session_id": memory.session_id}
        if path == "/api/messages":
            return self.start(data.get("session_id"), data.get("content"))
        if path == "/api/stop":
            with self.lock:
                run = self.runs.get(data.get("run_id"))
            if not run:
                raise APIError(404, "运行记录不存在")
            run.cancel.set()
            return {"status": "stopping"}
        session_id = data.get("session_id")
        self.require_session(session_id)
        # Serialize manual mutations against all active tool/model runs.
        if not self.run_lock.acquire(blocking=False):
            raise APIError(409, "请等待当前任务完成后再修改")
        try:
            with self.memory(session_id, model=self.model) as memory:
                if path == "/api/rename":
                    title = data.get("title", "").strip()
                    if not title or len(title) > 100:
                        raise ValueError("会话名称需为 1–100 字符")
                    with memory.db.connection:
                        memory.db.execute("INSERT OR REPLACE INTO lamemory_web_sessions VALUES (?,?,?)",
                                          (self.user_id, session_id, title))
                    return {"status": "saved"}
                if path == "/api/memory/update":
                    return memory.update(data.get("memory_id"), data.get("content")).to_dict()
                if path == "/api/memory/forget":
                    return memory.forget(data.get("memory_id"))
                if path == "/api/jobs/retry":
                    if self.demo:
                        raise ValueError("离线演示不调用模型整理记忆")
                    if not self.model or not self.model.api_key:
                        raise ValueError("请先配置模型")
                    memory.retry_failed(data.get("job_id"))
                    return {"jobs": memory.process_pending(max_jobs=1)}
        finally:
            self.run_lock.release()
        raise APIError(404, "接口不存在")


def demo_chat(messages, **kwargs):
    """显式离线演示，不请求模型；仍运行真实的记忆工具和 SQLite 留档。"""
    content = next(m["content"] for m in reversed(messages) if m["role"] == "user")
    if "记住" in content and messages[-1]["role"] == "user":
        return {"role": "assistant", "content": "", "tool_calls": [{"id": uuid.uuid4().hex,
                "type": "function", "function": {"name": "lamemory_store",
                "arguments": json.dumps({"content": content.split("记住", 1)[1].lstrip("：: ") or content}, ensure_ascii=False)}}]}
    if messages[-1]["role"] == "tool":
        return {"role": "assistant", "content": "演示记忆工具已执行，真实返回结果可以在上方展开查看，记忆面板显示保存状态。"}
    return {"role": "assistant", "content": "这是 **离线演示**，未调用真实模型。\n\n你可以体验会话切换、工具执行记录和 LaMemory 面板。试试发送：\n\n> 记住：我喜欢使用中文交流\n\n退出演示后，配置模型即可使用完整的对话、文件与搜索工具。"}


def make_server(app, host="127.0.0.1", port=3080):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def send_bytes(self, status, body, content_type):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            self.end_headers()
            self.wfile.write(body)

        def json(self, status, value):
            self.send_bytes(status, json.dumps(value, ensure_ascii=False).encode(), "application/json; charset=utf-8")

        def check_origin(self, mutation=False):
            allowed = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
            if self.headers.get("Host") not in allowed:
                raise APIError(403, "仅允许本机同源访问")
            origin = self.headers.get("Origin")
            if origin and origin != "http://" + self.headers.get("Host", ""):
                raise APIError(403, "拒绝跨站请求")
            if mutation and not secrets.compare_digest(self.headers.get("X-MyClaude-Token", ""), app.csrf):
                raise APIError(403, "页面令牌已失效，请刷新页面")

        def do_GET(self):
            try:
                self.check_origin()
                url = urlsplit(self.path)
                query = parse_qs(url.query)
                session_id = query.get("session_id", [""])[0]
                if url.path == "/api/bootstrap":
                    return self.json(200, app.bootstrap())
                if url.path == "/api/sessions":
                    return self.json(200, app.sessions())
                if url.path == "/api/session":
                    return self.json(200, app.session(session_id))
                if url.path == "/api/memories":
                    return self.json(200, app.memories(session_id, query.get("q", [""])[0]))
                if url.path == "/api/memory":
                    app.require_session(session_id)
                    with app.memory(session_id) as memory:
                        return self.json(200, memory.inspect(query.get("id", [""])[0]))
                if url.path == "/api/events":
                    return self.stream(query.get("run_id", [""])[0])
                assets = {"/": ("index.html", "text/html"), "/app.js": ("app.js", "text/javascript"),
                          "/style.css": ("style.css", "text/css"), "/favicon.svg": ("favicon.svg", "image/svg+xml")}
                if url.path not in assets:
                    raise APIError(404, "页面不存在")
                name, mime = assets[url.path]
                self.send_bytes(200, (STATIC / name).read_bytes(), mime + "; charset=utf-8")
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as exc:
                self.handle_error(exc)

        def do_POST(self):
            try:
                self.check_origin(mutation=True)
                if self.headers.get("Content-Type", "").split(";", 1)[0] != "application/json":
                    raise APIError(415, "需要 JSON 请求")
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 150000:
                    raise APIError(413, "请求大小超出限制")
                data = json.loads(self.rfile.read(size))
                if not isinstance(data, dict):
                    raise APIError(400, "请求需为 JSON 对象")
                self.json(200, app.mutate(urlsplit(self.path).path, data))
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as exc:
                self.handle_error(exc)

        def handle_error(self, exc):
            if isinstance(exc, APIError):
                self.json(exc.status, {"error": exc.message})
            elif isinstance(exc, KeyError):
                self.json(404, {"error": "记录不存在或不可见"})
            elif isinstance(exc, (ValueError, TypeError, AttributeError)):
                self.json(400, {"error": "请求参数无效：" + str(exc)[:300]})
            else:
                self.json(500, {"error": "服务处理失败，请检查本地配置与数据库"})

        def stream(self, run_id):
            with app.lock:
                run = app.runs.get(run_id)
            if not run:
                raise APIError(404, "运行记录已过期，请刷新会话")
            cursor = max(0, int(self.headers.get("Last-Event-ID", "0")))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            while True:
                with run.condition:
                    if cursor >= len(run.events) and not run.done:
                        run.condition.wait(timeout=10)
                    events, done = run.events[cursor:], run.done
                for event, data in events:
                    cursor += 1
                    packet = f"id: {cursor}\nevent: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
                    self.wfile.write(packet.encode())
                self.wfile.write(b": heartbeat\n\n")
                self.wfile.flush()
                if done:
                    return

    return ThreadingHTTPServer((host, port), Handler)


def main():
    parser = argparse.ArgumentParser(description="myClaude 本地 Web 工作台")
    parser.add_argument("--port", type=int, default=3080)
    parser.add_argument("--database")
    parser.add_argument("--user", default="local-user")
    parser.add_argument("--project")
    parser.add_argument("--demo", action="store_true", help="使用临时数据库运行离线演示，不请求模型")
    args = parser.parse_args()
    temporary = tempfile.TemporaryDirectory(prefix="myclaude-web-") if args.demo and not args.database else None
    config = LaMemoryConfig(project_id=args.project)
    if args.database or temporary:
        config = replace(config, database_path=args.database or Path(temporary.name) / "lamemory.sqlite3")
    try:
        model = ModelConfig.from_env(require_key=False)
    except ValueError:
        model = None
    app = WebApp(config=config, user_id=args.user, model_config=model, demo=args.demo)
    server = make_server(app, port=args.port)
    print(f"myClaude Web → http://127.0.0.1:{server.server_port}", flush=True)
    if args.demo:
        print("离线演示：未调用模型；未指定 --database 时使用临时数据库。", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        with app.lock:
            runs = list(app.runs.values())
        for run in runs:
            run.cancel.set()
        server.server_close()
        # Workers may still be returning from bounded model/tool calls; do not remove their DB early.
        if temporary and all(run.done for run in runs):
            temporary.cleanup()


if __name__ == "__main__":
    main()
