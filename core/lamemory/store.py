"""SQLite 事实来源；事务由服务控制，模型调用不进入此模块。"""
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def utc_now():
    return datetime.now(timezone.utc).isoformat()


class SQLiteStore:
    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, timeout=5)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.connection.execute("PRAGMA journal_mode = WAL")
        schema = Path(__file__).parent / "migrations/001_initial.sql"
        # The migration and version marker are committed together, including on restart.
        self.connection.executescript("BEGIN IMMEDIATE;\n" + schema.read_text(encoding="utf-8") +
                                      "\nINSERT OR IGNORE INTO lamemory_migrations VALUES (1, datetime('now'));\nCOMMIT;")

    def execute(self, sql, params=()):
        return self.connection.execute(sql, params)

    def rows(self, sql, params=()):
        return [dict(row) for row in self.execute(sql, params).fetchall()]

    def one(self, sql, params=()):
        row = self.execute(sql, params).fetchone()
        return dict(row) if row else None

    def messages(self, user_id, session_id, turn_id=None):
        sql = "SELECT * FROM lamemory_messages WHERE user_id=? AND session_id=?"
        params = [user_id, session_id]
        if turn_id is not None:
            sql += " AND turn_id=?"
            params.append(turn_id)
        rows = self.rows(sql + " ORDER BY seq", params)
        for row in rows:
            row["payload"] = json.loads(row.pop("payload_json"))
        return rows

    def close(self):
        self.connection.close()
