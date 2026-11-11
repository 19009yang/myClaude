"""串行、持久化索引任务；模型调用在写事务外，发布使用来源 CAS。"""
import struct
import math
import time
import uuid
from ..retrieval.fusion import unit_vector
from ..retrieval.sources import documents, visible_ledger
from ..storage.store import utc_now
from ..retrieval.types import error_kind


def pack(vector, dimensions):
    return struct.pack('<' + 'f' * dimensions, *unit_vector(vector, dimensions))


def unpack(blob, dimensions):
    if len(blob) != dimensions * 4:
        raise ValueError('invalid_blob_length')
    return unit_vector(list(struct.unpack('<' + 'f' * dimensions, blob)), dimensions)


class EmbeddingIndex:
    def __init__(self, service, provider):
        self.service, self.db, self.provider = service, service.db, provider
        self.descriptor = provider.descriptor
        self.space = self.descriptor.space_id
        self._processing = False
        if self.descriptor.max_embedding_chars != service.config.max_embedding_chars:
            raise ValueError('embedding descriptor 与 max_embedding_chars 不一致')
        with self.db.transaction():
            self.db.execute('INSERT OR IGNORE INTO lamemory_embedding_spaces VALUES (?,?,?,?)',
                            (self.space, self.descriptor.json, self.descriptor.dimensions, utc_now()))

    def snapshot(self):
        return {(d.entity_kind, d.entity_id): d for d in documents(self.service, self.descriptor)}

    def vectors(self, docs):
        rows = self.db.rows('SELECT * FROM lamemory_embeddings WHERE user_id=? AND space_id=?',
                            (self.service.user_id, self.space))
        vectors, invalid = {}, 0
        for row in rows:
            key = row['entity_kind'], row['entity_id']
            doc = docs.get(key)
            if not doc or row['entity_generation'] != doc.entity_generation or row['input_hash'] != doc.input_hash:
                continue
            try:
                if row['dimensions'] != self.descriptor.dimensions:
                    raise ValueError('dimensions')
                vectors[key] = unpack(row['vector_blob'], self.descriptor.dimensions)
            except (ValueError, TypeError, struct.error):
                invalid += 1
        return vectors, invalid

    def _progress(self, state):
        self.db.execute('INSERT INTO lamemory_embedding_progress VALUES (?,?,?,?,?,?) ON CONFLICT(user_id,entity_kind,entity_id,space_id) DO UPDATE SET handled_generation=excluded.handled_generation,updated_at=excluded.updated_at',
                        (state['user_id'], state['entity_kind'], state['entity_id'], self.space, state['generation'], utc_now()))

    def enqueue(self):
        queued, repaired = 0, 0
        with self.db.transaction():
            self.db.execute('UPDATE lamemory_retrieval_state SET generation=generation WHERE user_id=?', (self.service.user_id,))
            docs = self.snapshot()
            vectors, _ = self.vectors(docs)
            for state in visible_ledger(self.service):
                key = state['entity_kind'], state['entity_id']
                args = (state['user_id'], *key, self.space)
                self.db.execute("UPDATE lamemory_embedding_jobs SET status='obsolete',updated_at=? WHERE user_id=? AND entity_kind=? AND entity_id=? AND space_id=? AND entity_generation!=? AND status IN ('pending','failed')",
                                (utc_now(), *args, state['generation']))
                if key not in docs:
                    self.db.execute('DELETE FROM lamemory_embeddings WHERE user_id=? AND entity_kind=? AND entity_id=? AND space_id=?', args)
                    self.db.execute("UPDATE lamemory_embedding_jobs SET status='obsolete',updated_at=? WHERE user_id=? AND entity_kind=? AND entity_id=? AND space_id=? AND status IN ('pending','failed')", (utc_now(), *args))
                    self._progress(state)
                    continue
                doc = docs[key]
                if key in vectors:
                    self._progress(state)
                    continue
                previous = self.db.one('SELECT * FROM lamemory_embeddings WHERE user_id=? AND entity_kind=? AND entity_id=? AND space_id=? AND input_hash=?', (*args, doc.input_hash))
                if previous:
                    try:
                        unpack(previous['vector_blob'], self.descriptor.dimensions)
                        if previous['dimensions'] != self.descriptor.dimensions:
                            raise ValueError('dimensions')
                    except (ValueError, TypeError, struct.error):
                        pass
                    else:
                        self.db.execute('UPDATE lamemory_embeddings SET entity_generation=?,created_at=? WHERE user_id=? AND entity_kind=? AND entity_id=? AND space_id=?',
                                        (doc.entity_generation, utc_now(), *args))
                        self._progress(state)
                        continue
                job = self.db.one('SELECT * FROM lamemory_embedding_jobs WHERE user_id=? AND entity_kind=? AND entity_id=? AND space_id=? AND entity_generation=?', (*args, doc.entity_generation))
                if job:
                    if job['status'] == 'completed':
                        self.db.execute("UPDATE lamemory_embedding_jobs SET status='pending',attempts=0,available_at=0,last_error='repair_missing_or_invalid',updated_at=? WHERE job_id=?", (utc_now(), job['job_id']))
                        self.db.execute('UPDATE lamemory_retrieval_state SET generation=generation+1 WHERE user_id=?', (state['user_id'],))
                        repaired += 1
                    continue
                self.db.execute('INSERT INTO lamemory_embedding_jobs(job_id,user_id,entity_kind,entity_id,space_id,entity_generation,input_hash,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,\'pending\',?,?)',
                                ('emb_' + uuid.uuid4().hex, *args, doc.entity_generation, doc.input_hash, utc_now(), utc_now()))
                queued += 1
        return {'queued': queued, 'repaired': repaired, 'space_id': self.space}

    def jobs(self):
        visible = {(s['entity_kind'], s['entity_id']) for s in visible_ledger(self.service)}
        return [j for j in self.db.rows('SELECT * FROM lamemory_embedding_jobs WHERE user_id=? AND space_id=? ORDER BY created_at,job_id', (self.service.user_id, self.space))
                if (j['entity_kind'], j['entity_id']) in visible]

    def status(self):
        docs = self.snapshot()
        vectors, invalid = self.vectors(docs)
        jobs = self.jobs()
        ledger = {(s['entity_kind'], s['entity_id']): s for s in visible_ledger(self.service)}
        def target(job):
            key = job['entity_kind'], job['entity_id']
            return key in docs and job['entity_generation'] == ledger[key]['generation']
        counts = {state: sum(j['status'] == state and (state in ('completed', 'obsolete') or target(j)) for j in jobs)
                  for state in ('pending', 'completed', 'failed', 'obsolete')}
        return {'space_id': self.space, 'eligible': len(docs), 'indexed': len(vectors), 'missing': len(docs)-len(vectors),
                'invalid_vectors': invalid, 'truncated_documents': sum(d.truncated for d in docs.values()), 'remaining': counts.pop('pending'), **counts}

    def retry(self, job_id):
        job = next((j for j in self.jobs() if j['job_id'] == job_id and j['status'] == 'failed'), None)
        if not job:
            return False
        with self.db.transaction():
            self.db.execute("UPDATE lamemory_embedding_jobs SET status='pending',attempts=0,available_at=0,last_error=NULL,updated_at=? WHERE job_id=?", (utc_now(), job_id))
        return True

    def process(self, max_jobs=16, *, deadline=None):
        if self._processing:
            raise RuntimeError('索引任务正在串行消费')
        self._processing = True
        try:
            return self._process(max_jobs, deadline=deadline)
        finally:
            self._processing = False

    def _process(self, max_jobs, *, deadline):
        if deadline is not None and not math.isfinite(deadline):
            raise ValueError('deadline 必须为有限数')
        if type(max_jobs) is not int or max_jobs < 0:
            raise ValueError('max_jobs 必须为非负整数')
        if self.db.connection.in_transaction:
            raise RuntimeError('embedding 不允许在数据库写事务中调用')
        self.enqueue()
        output = []
        pending = [j for j in self.jobs() if j['status'] == 'pending' and j['available_at'] <= time.time()][:max_jobs]
        size = self.service.config.embedding_batch_size
        for start in range(0, len(pending), size):
            remaining = min(self.service.config.provider_timeout_seconds, deadline-time.monotonic()) if deadline is not None else self.service.config.provider_timeout_seconds
            if remaining <= 0:
                break
            snapshot = self.snapshot()
            batch = []
            with self.db.transaction():
                for job in pending[start:start+size]:
                    doc = snapshot.get((job['entity_kind'], job['entity_id']))
                    if not doc or doc.entity_generation != job['entity_generation'] or doc.input_hash != job['input_hash']:
                        self.db.execute("UPDATE lamemory_embedding_jobs SET status='obsolete',updated_at=? WHERE job_id=?", (utc_now(), job['job_id']))
                    else:
                        batch.append((job, doc))
            if not batch:
                continue
            if deadline is not None:
                remaining = min(remaining, deadline-time.monotonic())
                if remaining <= 0:
                    break
            try:
                vectors = self.provider.embed_documents([d.text for _, d in batch], timeout=remaining)
                if len(vectors) != len(batch):
                    raise ValueError('invalid_batch_count')
                blobs = [pack(v, self.descriptor.dimensions) for v in vectors]
                # Include provider latency in deadline: late responses cannot publish.
                if deadline is not None and time.monotonic() > deadline:
                    raise TimeoutError('embedding_deadline')
            except Exception as exc:
                with self.db.transaction():
                    for job, _ in batch:
                        attempts = job['attempts'] + 1
                        status = 'failed' if attempts >= self.service.config.job_max_attempts else 'pending'
                        self.db.execute('UPDATE lamemory_embedding_jobs SET status=?,attempts=?,available_at=?,last_error=?,updated_at=? WHERE job_id=?',
                                        (status, attempts, time.time()+self.service.config.retry_delay_seconds*2**(attempts-1), error_kind(exc), utc_now(), job['job_id']))
                        output.append({'job_id': job['job_id'], 'status': status, 'error': error_kind(exc)})
                continue
            with self.db.transaction():
                self.db.execute('UPDATE lamemory_retrieval_state SET generation=generation WHERE user_id=?', (self.service.user_id,))
                current = self.snapshot()
                for (job, doc), blob in zip(batch, blobs):
                    fresh = current.get((doc.entity_kind, doc.entity_id))
                    valid = fresh and fresh.entity_generation == doc.entity_generation and fresh.input_hash == doc.input_hash
                    status = 'completed' if valid else 'obsolete'
                    if valid:
                        self.db.execute('INSERT INTO lamemory_embeddings VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(user_id,entity_kind,entity_id,space_id) DO UPDATE SET entity_generation=excluded.entity_generation,input_hash=excluded.input_hash,dimensions=excluded.dimensions,vector_blob=excluded.vector_blob,created_at=excluded.created_at',
                                        (doc.user_id, doc.entity_kind, doc.entity_id, self.space, doc.entity_generation, doc.input_hash, self.descriptor.dimensions, blob, utc_now()))
                        self._progress({'user_id': doc.user_id, 'entity_kind': doc.entity_kind, 'entity_id': doc.entity_id, 'generation': doc.entity_generation})
                    self.db.execute('UPDATE lamemory_embedding_jobs SET status=?,attempts=attempts+1,updated_at=?,last_error=NULL WHERE job_id=?', (status, utc_now(), job['job_id']))
                    output.append({'job_id': job['job_id'], 'status': status})
        return output

    def flush(self, timeout=30):
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError('timeout 必须为正数')
        deadline = time.monotonic()+timeout
        while True:
            self.enqueue()
            status = self.status()
            if status['failed']:
                return {'status': 'failed', **status}
            if not status['remaining'] and not status['missing']:
                return {'status': 'complete', **status}
            if time.monotonic() >= deadline:
                return {'status': 'timeout', **status}
            self.process(deadline=deadline)
            time.sleep(min(.02, max(0, deadline-time.monotonic())))
