-- Rebuildable retrieval projections; authority remains in existing tables.
CREATE TABLE lamemory_retrieval_state (user_id TEXT PRIMARY KEY, generation INTEGER NOT NULL DEFAULT 0);
CREATE TABLE lamemory_embedding_entities (
 user_id TEXT NOT NULL, entity_kind TEXT NOT NULL CHECK(entity_kind IN ('item','summary','experience')),
 entity_id TEXT NOT NULL, scope_kind TEXT NOT NULL CHECK(scope_kind IN ('user','session','project')),
 scope_key TEXT NOT NULL, generation INTEGER NOT NULL CHECK(generation>0),
 is_deleted INTEGER NOT NULL CHECK(is_deleted IN (0,1)), updated_at TEXT NOT NULL,
 PRIMARY KEY(user_id,entity_kind,entity_id));
CREATE TABLE lamemory_embedding_spaces (space_id TEXT PRIMARY KEY, descriptor_json TEXT NOT NULL,
 dimensions INTEGER NOT NULL CHECK(dimensions>0), created_at TEXT NOT NULL);
CREATE TABLE lamemory_embeddings (
 user_id TEXT NOT NULL, entity_kind TEXT NOT NULL, entity_id TEXT NOT NULL, space_id TEXT NOT NULL,
 entity_generation INTEGER NOT NULL, input_hash TEXT NOT NULL, dimensions INTEGER NOT NULL CHECK(dimensions>0),
 vector_blob BLOB NOT NULL CHECK(length(vector_blob)=dimensions*4), created_at TEXT NOT NULL,
 PRIMARY KEY(user_id,entity_kind,entity_id,space_id),
 FOREIGN KEY(user_id,entity_kind,entity_id) REFERENCES lamemory_embedding_entities(user_id,entity_kind,entity_id),
 FOREIGN KEY(space_id) REFERENCES lamemory_embedding_spaces(space_id));
CREATE TABLE lamemory_embedding_jobs (
 job_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, entity_kind TEXT NOT NULL, entity_id TEXT NOT NULL,
 space_id TEXT NOT NULL, entity_generation INTEGER NOT NULL, input_hash TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('pending','completed','failed','obsolete')), attempts INTEGER NOT NULL DEFAULT 0,
 available_at REAL NOT NULL DEFAULT 0, last_error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 UNIQUE(user_id,entity_kind,entity_id,space_id,entity_generation),
 FOREIGN KEY(user_id,entity_kind,entity_id) REFERENCES lamemory_embedding_entities(user_id,entity_kind,entity_id),
 FOREIGN KEY(space_id) REFERENCES lamemory_embedding_spaces(space_id));
CREATE INDEX lamemory_embedding_schedule ON lamemory_embedding_jobs(user_id,space_id,status,available_at);
CREATE TABLE lamemory_embedding_progress (
 user_id TEXT NOT NULL, entity_kind TEXT NOT NULL, entity_id TEXT NOT NULL, space_id TEXT NOT NULL,
 handled_generation INTEGER NOT NULL, updated_at TEXT NOT NULL,
 PRIMARY KEY(user_id,entity_kind,entity_id,space_id),
 FOREIGN KEY(user_id,entity_kind,entity_id) REFERENCES lamemory_embedding_entities(user_id,entity_kind,entity_id),
 FOREIGN KEY(space_id) REFERENCES lamemory_embedding_spaces(space_id));
INSERT INTO lamemory_embedding_entities SELECT user_id,'item',memory_id,t.scope_kind,t.scope_key,1,0,updated_at FROM lamemory_items t;
CREATE TRIGGER lamemory_index_item_insert AFTER INSERT ON lamemory_items BEGIN
INSERT INTO lamemory_embedding_entities VALUES (NEW.user_id,'item',NEW.memory_id,NEW.scope_kind,NEW.scope_key,1,0,strftime('%Y-%m-%dT%H:%M:%fZ','now'))
 ON CONFLICT(user_id,entity_kind,entity_id) DO UPDATE SET generation=generation+1,scope_kind=excluded.scope_kind,scope_key=excluded.scope_key,is_deleted=excluded.is_deleted,updated_at=excluded.updated_at;
 INSERT INTO lamemory_retrieval_state VALUES (NEW.user_id,1) ON CONFLICT(user_id) DO UPDATE SET generation=generation+1;
END;
CREATE TRIGGER lamemory_index_item_update AFTER UPDATE ON lamemory_items BEGIN
INSERT INTO lamemory_embedding_entities VALUES (OLD.user_id,'item',OLD.memory_id,OLD.scope_kind,OLD.scope_key,1,1,strftime('%Y-%m-%dT%H:%M:%fZ','now'))
 ON CONFLICT(user_id,entity_kind,entity_id) DO UPDATE SET generation=generation+1,scope_kind=excluded.scope_kind,scope_key=excluded.scope_key,is_deleted=excluded.is_deleted,updated_at=excluded.updated_at;
 INSERT INTO lamemory_retrieval_state VALUES (OLD.user_id,1) ON CONFLICT(user_id) DO UPDATE SET generation=generation+1;
INSERT INTO lamemory_embedding_entities VALUES (NEW.user_id,'item',NEW.memory_id,NEW.scope_kind,NEW.scope_key,1,0,strftime('%Y-%m-%dT%H:%M:%fZ','now'))
 ON CONFLICT(user_id,entity_kind,entity_id) DO UPDATE SET generation=generation+1,scope_kind=excluded.scope_kind,scope_key=excluded.scope_key,is_deleted=excluded.is_deleted,updated_at=excluded.updated_at;
 INSERT INTO lamemory_retrieval_state VALUES (NEW.user_id,1) ON CONFLICT(user_id) DO UPDATE SET generation=generation+1;
END;
CREATE TRIGGER lamemory_index_item_delete AFTER DELETE ON lamemory_items BEGIN
INSERT INTO lamemory_embedding_entities VALUES (OLD.user_id,'item',OLD.memory_id,OLD.scope_kind,OLD.scope_key,1,1,strftime('%Y-%m-%dT%H:%M:%fZ','now'))
 ON CONFLICT(user_id,entity_kind,entity_id) DO UPDATE SET generation=generation+1,scope_kind=excluded.scope_kind,scope_key=excluded.scope_key,is_deleted=excluded.is_deleted,updated_at=excluded.updated_at;
 INSERT INTO lamemory_retrieval_state VALUES (OLD.user_id,1) ON CONFLICT(user_id) DO UPDATE SET generation=generation+1;
END;
INSERT INTO lamemory_embedding_entities SELECT user_id,'summary',summary_id,CASE WHEN t.project_id IS NULL THEN 'user' ELSE 'project' END,COALESCE(t.project_id,t.user_id),1,0,updated_at FROM lamemory_summaries t;
CREATE TRIGGER lamemory_index_summary_insert AFTER INSERT ON lamemory_summaries BEGIN
INSERT INTO lamemory_embedding_entities VALUES (NEW.user_id,'summary',NEW.summary_id,CASE WHEN NEW.project_id IS NULL THEN 'user' ELSE 'project' END,COALESCE(NEW.project_id,NEW.user_id),1,0,strftime('%Y-%m-%dT%H:%M:%fZ','now'))
 ON CONFLICT(user_id,entity_kind,entity_id) DO UPDATE SET generation=generation+1,scope_kind=excluded.scope_kind,scope_key=excluded.scope_key,is_deleted=excluded.is_deleted,updated_at=excluded.updated_at;
 INSERT INTO lamemory_retrieval_state VALUES (NEW.user_id,1) ON CONFLICT(user_id) DO UPDATE SET generation=generation+1;
END;
CREATE TRIGGER lamemory_index_summary_update AFTER UPDATE ON lamemory_summaries BEGIN
INSERT INTO lamemory_embedding_entities VALUES (OLD.user_id,'summary',OLD.summary_id,CASE WHEN OLD.project_id IS NULL THEN 'user' ELSE 'project' END,COALESCE(OLD.project_id,OLD.user_id),1,1,strftime('%Y-%m-%dT%H:%M:%fZ','now'))
 ON CONFLICT(user_id,entity_kind,entity_id) DO UPDATE SET generation=generation+1,scope_kind=excluded.scope_kind,scope_key=excluded.scope_key,is_deleted=excluded.is_deleted,updated_at=excluded.updated_at;
 INSERT INTO lamemory_retrieval_state VALUES (OLD.user_id,1) ON CONFLICT(user_id) DO UPDATE SET generation=generation+1;
INSERT INTO lamemory_embedding_entities VALUES (NEW.user_id,'summary',NEW.summary_id,CASE WHEN NEW.project_id IS NULL THEN 'user' ELSE 'project' END,COALESCE(NEW.project_id,NEW.user_id),1,0,strftime('%Y-%m-%dT%H:%M:%fZ','now'))
 ON CONFLICT(user_id,entity_kind,entity_id) DO UPDATE SET generation=generation+1,scope_kind=excluded.scope_kind,scope_key=excluded.scope_key,is_deleted=excluded.is_deleted,updated_at=excluded.updated_at;
 INSERT INTO lamemory_retrieval_state VALUES (NEW.user_id,1) ON CONFLICT(user_id) DO UPDATE SET generation=generation+1;
END;
CREATE TRIGGER lamemory_index_summary_delete AFTER DELETE ON lamemory_summaries BEGIN
INSERT INTO lamemory_embedding_entities VALUES (OLD.user_id,'summary',OLD.summary_id,CASE WHEN OLD.project_id IS NULL THEN 'user' ELSE 'project' END,COALESCE(OLD.project_id,OLD.user_id),1,1,strftime('%Y-%m-%dT%H:%M:%fZ','now'))
 ON CONFLICT(user_id,entity_kind,entity_id) DO UPDATE SET generation=generation+1,scope_kind=excluded.scope_kind,scope_key=excluded.scope_key,is_deleted=excluded.is_deleted,updated_at=excluded.updated_at;
 INSERT INTO lamemory_retrieval_state VALUES (OLD.user_id,1) ON CONFLICT(user_id) DO UPDATE SET generation=generation+1;
END;
INSERT INTO lamemory_embedding_entities SELECT user_id,'experience',experience_id,t.scope_kind,t.scope_key,1,0,updated_at FROM lamemory_experiences t;
CREATE TRIGGER lamemory_index_experience_insert AFTER INSERT ON lamemory_experiences BEGIN
INSERT INTO lamemory_embedding_entities VALUES (NEW.user_id,'experience',NEW.experience_id,NEW.scope_kind,NEW.scope_key,1,0,strftime('%Y-%m-%dT%H:%M:%fZ','now'))
 ON CONFLICT(user_id,entity_kind,entity_id) DO UPDATE SET generation=generation+1,scope_kind=excluded.scope_kind,scope_key=excluded.scope_key,is_deleted=excluded.is_deleted,updated_at=excluded.updated_at;
 INSERT INTO lamemory_retrieval_state VALUES (NEW.user_id,1) ON CONFLICT(user_id) DO UPDATE SET generation=generation+1;
END;
CREATE TRIGGER lamemory_index_experience_update AFTER UPDATE ON lamemory_experiences BEGIN
INSERT INTO lamemory_embedding_entities VALUES (OLD.user_id,'experience',OLD.experience_id,OLD.scope_kind,OLD.scope_key,1,1,strftime('%Y-%m-%dT%H:%M:%fZ','now'))
 ON CONFLICT(user_id,entity_kind,entity_id) DO UPDATE SET generation=generation+1,scope_kind=excluded.scope_kind,scope_key=excluded.scope_key,is_deleted=excluded.is_deleted,updated_at=excluded.updated_at;
 INSERT INTO lamemory_retrieval_state VALUES (OLD.user_id,1) ON CONFLICT(user_id) DO UPDATE SET generation=generation+1;
INSERT INTO lamemory_embedding_entities VALUES (NEW.user_id,'experience',NEW.experience_id,NEW.scope_kind,NEW.scope_key,1,0,strftime('%Y-%m-%dT%H:%M:%fZ','now'))
 ON CONFLICT(user_id,entity_kind,entity_id) DO UPDATE SET generation=generation+1,scope_kind=excluded.scope_kind,scope_key=excluded.scope_key,is_deleted=excluded.is_deleted,updated_at=excluded.updated_at;
 INSERT INTO lamemory_retrieval_state VALUES (NEW.user_id,1) ON CONFLICT(user_id) DO UPDATE SET generation=generation+1;
END;
CREATE TRIGGER lamemory_index_experience_delete AFTER DELETE ON lamemory_experiences BEGIN
INSERT INTO lamemory_embedding_entities VALUES (OLD.user_id,'experience',OLD.experience_id,OLD.scope_kind,OLD.scope_key,1,1,strftime('%Y-%m-%dT%H:%M:%fZ','now'))
 ON CONFLICT(user_id,entity_kind,entity_id) DO UPDATE SET generation=generation+1,scope_kind=excluded.scope_kind,scope_key=excluded.scope_key,is_deleted=excluded.is_deleted,updated_at=excluded.updated_at;
 INSERT INTO lamemory_retrieval_state VALUES (OLD.user_id,1) ON CONFLICT(user_id) DO UPDATE SET generation=generation+1;
END;
CREATE TRIGGER lamemory_sources_retrieval_insert AFTER INSERT ON lamemory_sources BEGIN
 INSERT INTO lamemory_retrieval_state VALUES (NEW.user_id,1) ON CONFLICT(user_id) DO UPDATE SET generation=generation+1;
 END;
CREATE TRIGGER lamemory_sources_retrieval_update AFTER UPDATE ON lamemory_sources BEGIN
 INSERT INTO lamemory_retrieval_state VALUES (NEW.user_id,1) ON CONFLICT(user_id) DO UPDATE SET generation=generation+1;
 END;
CREATE TRIGGER lamemory_sources_retrieval_delete AFTER DELETE ON lamemory_sources BEGIN
 INSERT INTO lamemory_retrieval_state VALUES (OLD.user_id,1) ON CONFLICT(user_id) DO UPDATE SET generation=generation+1;
 END;
CREATE TRIGGER lamemory_summary_sources_retrieval_insert AFTER INSERT ON lamemory_summary_sources BEGIN
 INSERT INTO lamemory_retrieval_state VALUES (NEW.user_id,1) ON CONFLICT(user_id) DO UPDATE SET generation=generation+1;
 END;
CREATE TRIGGER lamemory_summary_sources_retrieval_update AFTER UPDATE ON lamemory_summary_sources BEGIN
 INSERT INTO lamemory_retrieval_state VALUES (NEW.user_id,1) ON CONFLICT(user_id) DO UPDATE SET generation=generation+1;
 END;
CREATE TRIGGER lamemory_summary_sources_retrieval_delete AFTER DELETE ON lamemory_summary_sources BEGIN
 INSERT INTO lamemory_retrieval_state VALUES (OLD.user_id,1) ON CONFLICT(user_id) DO UPDATE SET generation=generation+1;
 END;
CREATE TRIGGER lamemory_topic_items_retrieval_insert AFTER INSERT ON lamemory_topic_items BEGIN
 INSERT INTO lamemory_retrieval_state VALUES (NEW.user_id,1) ON CONFLICT(user_id) DO UPDATE SET generation=generation+1;
 END;
CREATE TRIGGER lamemory_topic_items_retrieval_update AFTER UPDATE ON lamemory_topic_items BEGIN
 INSERT INTO lamemory_retrieval_state VALUES (NEW.user_id,1) ON CONFLICT(user_id) DO UPDATE SET generation=generation+1;
 END;
CREATE TRIGGER lamemory_topic_items_retrieval_delete AFTER DELETE ON lamemory_topic_items BEGIN
 INSERT INTO lamemory_retrieval_state VALUES (OLD.user_id,1) ON CONFLICT(user_id) DO UPDATE SET generation=generation+1;
 END;
CREATE TRIGGER lamemory_experience_sources_retrieval_insert AFTER INSERT ON lamemory_experience_sources BEGIN
 INSERT INTO lamemory_retrieval_state VALUES (NEW.user_id,1) ON CONFLICT(user_id) DO UPDATE SET generation=generation+1;
 END;
CREATE TRIGGER lamemory_experience_sources_retrieval_update AFTER UPDATE ON lamemory_experience_sources BEGIN
 INSERT INTO lamemory_retrieval_state VALUES (NEW.user_id,1) ON CONFLICT(user_id) DO UPDATE SET generation=generation+1;
 END;
CREATE TRIGGER lamemory_experience_sources_retrieval_delete AFTER DELETE ON lamemory_experience_sources BEGIN
 INSERT INTO lamemory_retrieval_state VALUES (OLD.user_id,1) ON CONFLICT(user_id) DO UPDATE SET generation=generation+1;
 END;
CREATE TRIGGER lamemory_experience_feedback_retrieval_insert AFTER INSERT ON lamemory_experience_feedback BEGIN
 INSERT INTO lamemory_retrieval_state VALUES (NEW.user_id,1) ON CONFLICT(user_id) DO UPDATE SET generation=generation+1;
 END;
CREATE TRIGGER lamemory_experience_feedback_retrieval_update AFTER UPDATE ON lamemory_experience_feedback BEGIN
 INSERT INTO lamemory_retrieval_state VALUES (NEW.user_id,1) ON CONFLICT(user_id) DO UPDATE SET generation=generation+1;
 END;
CREATE TRIGGER lamemory_experience_feedback_retrieval_delete AFTER DELETE ON lamemory_experience_feedback BEGIN
 INSERT INTO lamemory_retrieval_state VALUES (OLD.user_id,1) ON CONFLICT(user_id) DO UPDATE SET generation=generation+1;
 END;
CREATE TRIGGER lamemory_embeddings_retrieval_insert AFTER INSERT ON lamemory_embeddings BEGIN
 INSERT INTO lamemory_retrieval_state VALUES (NEW.user_id,1) ON CONFLICT(user_id) DO UPDATE SET generation=generation+1;
 END;
CREATE TRIGGER lamemory_embeddings_retrieval_update AFTER UPDATE ON lamemory_embeddings BEGIN
 INSERT INTO lamemory_retrieval_state VALUES (NEW.user_id,1) ON CONFLICT(user_id) DO UPDATE SET generation=generation+1;
 END;
CREATE TRIGGER lamemory_embeddings_retrieval_delete AFTER DELETE ON lamemory_embeddings BEGIN
 INSERT INTO lamemory_retrieval_state VALUES (OLD.user_id,1) ON CONFLICT(user_id) DO UPDATE SET generation=generation+1;
 END;
INSERT OR IGNORE INTO lamemory_retrieval_state SELECT DISTINCT user_id,1 FROM lamemory_embedding_entities;
