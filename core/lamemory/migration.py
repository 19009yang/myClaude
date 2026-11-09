"""旧数据只读预览、备份和幂等导入；不修改任何源文件。"""
import hashlib
import json
from pathlib import Path
import shutil

from .extraction import validate_candidate
from .store import utc_now


def discover_legacy_paths(project_root):
    root = Path(project_root)
    # POSIX may contain literal backslashes from the old Windows-style constants.
    return [str(path) for path in (root / "core/chat_memory/session.jsonl", root / "core/chat_memory/MEMORY.md",
                                  root / r"core\chat_memory\session.jsonl", root / r"core\chat_memory\MEMORY.md") if path.is_file()]


def preview_legacy(*, session_path=None, memory_path=None, user_id="local-user", session_id="legacy-session"):
    files = []
    messages, invalid, entries = [], [], []
    for kind, supplied in (("session", session_path), ("memory", memory_path)):
        if supplied is None:
            continue
        path = Path(supplied).expanduser().resolve()
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        files.append({"kind": kind, "path": str(path), "sha256": digest})
        text = raw.decode("utf-8")
        if kind == "session":
            for index, line in enumerate(text.splitlines(), 1):
                if not line.strip():
                    continue
                try:
                    message = json.loads(line)
                    if not isinstance(message, dict) or message.get("role") not in {"user", "assistant", "tool", "system"}:
                        raise ValueError("无效消息角色或对象")
                    if message.get("content") is not None and not isinstance(message["content"], str):
                        raise ValueError("初版仅支持文本内容")
                    messages.append({"line": index, "payload": message})
                except (ValueError, TypeError) as exc:
                    invalid.append({"line": index, "error": str(exc)})
        else:
            for line in text.splitlines():
                if line.lstrip().startswith(("- ", "* ")):
                    content = line.lstrip()[2:].strip()
                    if content:
                        entries.append(content)
    hashes = [(f["kind"], f["sha256"]) for f in files]
    batch_id = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
    return {"batch_id": batch_id, "user_id": user_id, "session_id": session_id, "files": files,
            "message_count": len(messages), "invalid_lines": invalid, "memory_count": len(entries),
            "messages": messages, "entries": entries}


def import_legacy(service, *, session_path=None, memory_path=None):
    preview = preview_legacy(session_path=session_path, memory_path=memory_path,
                             user_id=service.user_id, session_id=service.session_id)
    batch = preview["batch_id"]
    params = (service.user_id, service.session_id, batch)
    if service.db.one("SELECT 1 FROM lamemory_imports WHERE user_id=? AND session_id=? AND batch_id=?", params):
        return {"status": "already_imported", "batch_id": batch}
    candidates = [validate_candidate({"content": entry}, service.config) for entry in preview["entries"]]
    backup_dir = service.config.database_path.parent / "backups" / batch
    backup_dir.mkdir(parents=True, exist_ok=True)
    for entry in preview["files"]:
        target = backup_dir / (entry["kind"] + (".jsonl" if entry["kind"] == "session" else ".md"))
        shutil.copy2(entry["path"], target)
        if hashlib.sha256(target.read_bytes()).hexdigest() != entry["sha256"]:
            raise ValueError("导入源在预览后发生变化，请重新执行导入")
    manifest = {k: v for k, v in preview.items() if k not in {"messages", "entries"}}
    (backup_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    now = utc_now()
    with service.db.connection:
        seq = service.db.one("SELECT COALESCE(MAX(seq),0) AS n FROM lamemory_messages WHERE user_id=? AND session_id=?",
                              (service.user_id, service.session_id))["n"]
        turn = "import_" + batch + "_preamble"
        for index, entry in enumerate(preview["messages"]):
            payload = entry["payload"]
            if payload["role"] == "user":
                turn = "import_" + batch + "_" + str(entry["line"])
            service.db.execute("INSERT OR IGNORE INTO lamemory_turns VALUES (?,?,?,NULL)",
                               (service.user_id, service.session_id, turn))
            source = hashlib.sha256(f"{service.user_id}:{service.session_id}:{batch}:{entry['line']}".encode()).hexdigest()
            service.db.execute("INSERT INTO lamemory_messages VALUES (?,?,?,?,?,?,?,?)",
                               ("msg_" + source, service.user_id, service.session_id, turn, seq + index + 1,
                                payload["role"], json.dumps(payload, ensure_ascii=False), now))
        for candidate in candidates:
            service._save(candidate, "legacy_import")
        service.db.execute("INSERT INTO lamemory_imports VALUES (?,?,?,?,?)", (*params, json.dumps(manifest, ensure_ascii=False), now))
    if preview["messages"]:
        service.current_turn_id = turn
    service._retrieval_cache = None
    return {"status": "imported", **manifest, "backup_directory": str(backup_dir)}
