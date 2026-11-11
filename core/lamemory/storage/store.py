"""SQLite 事实来源；事务由服务控制，模型调用不进入此模块。"""
import json
import sqlite3
from contextlib import contextmanager
import uuid
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
        # Rebuilding a CHECK constraint requires foreign_keys=OFF outside the transaction.
        # Each migration and its marker commit together; existing databases keep their evidence.
        self.connection.execute("CREATE TABLE IF NOT EXISTS lamemory_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
        for schema in sorted((Path(__file__).parent / "migrations").glob("[0-9]*.sql")):
            version = int(schema.name.split("_", 1)[0])
            if self.one("SELECT 1 FROM lamemory_migrations WHERE version=?", (version,)):
                continue
            self.connection.execute("PRAGMA foreign_keys = OFF")
            try:
                self.connection.executescript("BEGIN IMMEDIATE;\n" + schema.read_text(encoding="utf-8"))
                if self.connection.execute("PRAGMA foreign_key_check").fetchone():
                    raise sqlite3.IntegrityError("LaMemory 迁移产生无效的来源关系")
                self.execute("INSERT INTO lamemory_migrations VALUES (?,?)", (version, utc_now()))
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                self.connection.close()
                raise
            self.connection.execute("PRAGMA foreign_keys = ON")

    def execute(self, sql, params=()):
        return self.connection.execute(sql, params)

    @contextmanager
    def transaction(self):
        """可嵌套的短事务，不在内部提前提交外层批次。"""
        name = "lamemory_" + uuid.uuid4().hex
        self.execute(f"SAVEPOINT {name}")
        try:
            yield
        except BaseException:
            self.execute(f"ROLLBACK TO {name}")
            self.execute(f"RELEASE {name}")
            raise
        else:
            self.execute(f"RELEASE {name}")

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
