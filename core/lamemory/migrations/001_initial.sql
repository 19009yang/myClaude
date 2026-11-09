CREATE TABLE IF NOT EXISTS lamemory_migrations (
    version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS lamemory_sessions (
    user_id TEXT NOT NULL, session_id TEXT NOT NULL, created_at TEXT NOT NULL,
    PRIMARY KEY (user_id, session_id)
);
CREATE TABLE IF NOT EXISTS lamemory_turns (
    user_id TEXT NOT NULL, session_id TEXT NOT NULL, turn_id TEXT NOT NULL,
    committed_at TEXT,
    PRIMARY KEY (user_id, session_id, turn_id),
    FOREIGN KEY (user_id, session_id) REFERENCES lamemory_sessions(user_id, session_id)
);
CREATE TABLE IF NOT EXISTS lamemory_messages (
    message_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, session_id TEXT NOT NULL,
    turn_id TEXT NOT NULL, seq INTEGER NOT NULL, role TEXT NOT NULL,
    payload_json TEXT NOT NULL, created_at TEXT NOT NULL,
    UNIQUE (user_id, session_id, seq), UNIQUE (user_id, message_id),
    FOREIGN KEY (user_id, session_id, turn_id) REFERENCES lamemory_turns(user_id, session_id, turn_id)
);
CREATE TABLE IF NOT EXISTS lamemory_items (
    memory_id TEXT PRIMARY KEY, user_id TEXT NOT NULL,
    scope_kind TEXT NOT NULL CHECK(scope_kind IN ('user', 'session')),
    scope_key TEXT NOT NULL, type TEXT NOT NULL CHECK(type IN ('fact', 'preference', 'event')),
    content TEXT NOT NULL, content_hash TEXT NOT NULL, slot_key TEXT,
    occurred_at TEXT, status TEXT NOT NULL CHECK(status IN ('active', 'superseded', 'deleted', 'pending')),
    revision INTEGER NOT NULL, superseded_by TEXT,
    origin TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE (user_id, memory_id),
    FOREIGN KEY (user_id, superseded_by) REFERENCES lamemory_items(user_id, memory_id)
);
CREATE INDEX IF NOT EXISTS lamemory_items_visible ON lamemory_items(user_id, status, scope_kind, scope_key);
CREATE TABLE IF NOT EXISTS lamemory_sources (
    user_id TEXT NOT NULL, memory_id TEXT NOT NULL, message_id TEXT NOT NULL,
    PRIMARY KEY (user_id, memory_id, message_id),
    FOREIGN KEY (user_id, memory_id) REFERENCES lamemory_items(user_id, memory_id),
    FOREIGN KEY (user_id, message_id) REFERENCES lamemory_messages(user_id, message_id)
);
CREATE TABLE IF NOT EXISTS lamemory_jobs (
    job_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, session_id TEXT NOT NULL, turn_id TEXT NOT NULL,
    kind TEXT NOT NULL, idempotency_key TEXT NOT NULL, status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0, available_at REAL NOT NULL, last_error TEXT,
    payload_json TEXT NOT NULL, UNIQUE (user_id, idempotency_key),
    FOREIGN KEY (user_id, session_id, turn_id) REFERENCES lamemory_turns(user_id, session_id, turn_id)
);
CREATE TABLE IF NOT EXISTS lamemory_suppressions (
    user_id TEXT NOT NULL, scope_kind TEXT NOT NULL, scope_key TEXT NOT NULL,
    source_key TEXT NOT NULL DEFAULT '', slot_key TEXT NOT NULL DEFAULT '',
    content_hash TEXT NOT NULL, created_at TEXT NOT NULL,
    PRIMARY KEY(user_id, scope_kind, scope_key, source_key, slot_key, content_hash)
);
CREATE TABLE IF NOT EXISTS lamemory_imports (
    user_id TEXT NOT NULL, session_id TEXT NOT NULL, batch_id TEXT NOT NULL,
    manifest_json TEXT NOT NULL, created_at TEXT NOT NULL,
    PRIMARY KEY(user_id, session_id, batch_id)
);
CREATE TABLE IF NOT EXISTS lamemory_working_views (
    user_id TEXT NOT NULL, session_id TEXT NOT NULL, through_seq INTEGER NOT NULL,
    summary TEXT NOT NULL, created_at TEXT NOT NULL,
    PRIMARY KEY(user_id, session_id),
    FOREIGN KEY(user_id, session_id) REFERENCES lamemory_sessions(user_id, session_id)
);
