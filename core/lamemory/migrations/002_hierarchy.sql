CREATE TABLE lamemory_items_new (
    memory_id TEXT PRIMARY KEY, user_id TEXT NOT NULL,
    scope_kind TEXT NOT NULL CHECK(scope_kind IN ('user','session','project')),
    scope_key TEXT NOT NULL, type TEXT NOT NULL CHECK(type IN ('fact','preference','event')),
    content TEXT NOT NULL, content_hash TEXT NOT NULL, slot_key TEXT,
    occurred_at TEXT, status TEXT NOT NULL CHECK(status IN ('active','superseded','deleted','pending')),
    revision INTEGER NOT NULL, superseded_by TEXT, origin TEXT NOT NULL,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE(user_id,memory_id),
    FOREIGN KEY(user_id,superseded_by) REFERENCES lamemory_items(user_id,memory_id)
);
INSERT INTO lamemory_items_new SELECT * FROM lamemory_items;
DROP TABLE lamemory_items;
ALTER TABLE lamemory_items_new RENAME TO lamemory_items;
CREATE INDEX lamemory_items_visible ON lamemory_items(user_id,status,scope_kind,scope_key);

CREATE TABLE lamemory_summaries (
    summary_id TEXT PRIMARY KEY, user_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('profile','topic')),
    topic_key TEXT NOT NULL, project_id TEXT, content TEXT NOT NULL DEFAULT '',
    revision INTEGER NOT NULL DEFAULT 0, generation INTEGER NOT NULL DEFAULT 1,
    dirty INTEGER NOT NULL DEFAULT 1, source_signature TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL, UNIQUE(user_id,kind,topic_key), UNIQUE(user_id,summary_id)
);
CREATE TABLE lamemory_summary_sources (
    user_id TEXT NOT NULL, summary_id TEXT NOT NULL, memory_id TEXT NOT NULL,
    source_revision INTEGER NOT NULL, source_hash TEXT NOT NULL,
    PRIMARY KEY(user_id,summary_id,memory_id),
    FOREIGN KEY(user_id,summary_id) REFERENCES lamemory_summaries(user_id,summary_id),
    FOREIGN KEY(user_id,memory_id) REFERENCES lamemory_items(user_id,memory_id)
);
CREATE TABLE lamemory_topic_items (
    user_id TEXT NOT NULL, summary_id TEXT NOT NULL, memory_id TEXT NOT NULL,
    PRIMARY KEY(user_id,summary_id,memory_id),
    FOREIGN KEY(user_id,summary_id) REFERENCES lamemory_summaries(user_id,summary_id),
    FOREIGN KEY(user_id,memory_id) REFERENCES lamemory_items(user_id,memory_id)
);

-- New records must also dirty an existing aggregate, including additions not yet in its sources.
CREATE TRIGGER lamemory_hierarchy_insert AFTER INSERT ON lamemory_items
WHEN NEW.status='active'
BEGIN
    UPDATE lamemory_summaries SET dirty=1,generation=generation+1
    WHERE user_id=NEW.user_id AND (kind='profile' OR project_id=NEW.scope_key AND NEW.scope_kind='project');
END;
CREATE TRIGGER lamemory_hierarchy_update AFTER UPDATE ON lamemory_items
BEGIN
    UPDATE lamemory_summaries SET dirty=1,generation=generation+1
    WHERE user_id=NEW.user_id AND (kind='profile'
        OR project_id=NEW.scope_key AND NEW.scope_kind='project'
        OR summary_id IN (SELECT summary_id FROM lamemory_summary_sources WHERE user_id=NEW.user_id AND memory_id=NEW.memory_id)
        OR summary_id IN (SELECT summary_id FROM lamemory_topic_items WHERE user_id=NEW.user_id AND memory_id=NEW.memory_id));
    INSERT OR IGNORE INTO lamemory_topic_items
    SELECT user_id,summary_id,NEW.superseded_by FROM lamemory_topic_items
    WHERE user_id=NEW.user_id AND memory_id=NEW.memory_id AND NEW.superseded_by IS NOT NULL;
END;
CREATE TRIGGER lamemory_hierarchy_source_added AFTER INSERT ON lamemory_sources
BEGIN
    UPDATE lamemory_summaries SET dirty=1,generation=generation+1
    WHERE user_id=NEW.user_id AND summary_id IN
        (SELECT summary_id FROM lamemory_summary_sources WHERE user_id=NEW.user_id AND memory_id=NEW.memory_id);
END;
