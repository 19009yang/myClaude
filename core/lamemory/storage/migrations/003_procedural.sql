CREATE TABLE lamemory_experiences (
    experience_id TEXT PRIMARY KEY, user_id TEXT NOT NULL,
    scope_kind TEXT NOT NULL CHECK(scope_kind IN ('user','project')), scope_key TEXT NOT NULL,
    task_type TEXT NOT NULL, conditions TEXT NOT NULL, steps_json TEXT NOT NULL,
    result_basis TEXT NOT NULL, content_hash TEXT NOT NULL, trajectory_key TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('candidate','active','revoked','superseded')),
    revision INTEGER NOT NULL, lineage_id TEXT NOT NULL, superseded_by TEXT,
    successes INTEGER NOT NULL DEFAULT 0, failures INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE(user_id,experience_id), UNIQUE(user_id,trajectory_key,content_hash),
    FOREIGN KEY(user_id,superseded_by) REFERENCES lamemory_experiences(user_id,experience_id)
);
CREATE TABLE lamemory_experience_sources (
    user_id TEXT NOT NULL, experience_id TEXT NOT NULL, message_id TEXT NOT NULL,
    PRIMARY KEY(user_id,experience_id,message_id),
    FOREIGN KEY(user_id,experience_id) REFERENCES lamemory_experiences(user_id,experience_id),
    FOREIGN KEY(user_id,message_id) REFERENCES lamemory_messages(user_id,message_id)
);
CREATE TABLE lamemory_experience_feedback (
    feedback_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, experience_id TEXT NOT NULL,
    lineage_id TEXT NOT NULL, trajectory_key TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK(outcome IN ('success','failure','approved')),
    evidence_kind TEXT NOT NULL CHECK(evidence_kind IN ('user','test','model')),
    evidence TEXT NOT NULL, source_ids_json TEXT NOT NULL, created_at TEXT NOT NULL,
    UNIQUE(user_id,lineage_id,trajectory_key),
    FOREIGN KEY(user_id,experience_id) REFERENCES lamemory_experiences(user_id,experience_id)
);
