"""只从权威表物化当前可见、有效的候选，top-k 在此之后执行。"""
import json
from ..embedding.space import digest
from .types import RetrievalDocument


def documents(service, descriptor, kinds=('item', 'summary', 'experience'), filters=None):
    result = []
    ledger = {(r['entity_kind'], r['entity_id']): r for r in service.db.rows(
        'SELECT * FROM lamemory_embedding_entities WHERE user_id=?', (service.user_id,))}

    def add(kind, ident, row, text, payload):
        state = ledger.get((kind, ident))
        if not state or state['is_deleted']:
            return
        actual = descriptor.document_input(text) if descriptor else text
        result.append(RetrievalDocument(kind, ident, service.user_id, state['scope_kind'], state['scope_key'],
                                       state['generation'], row['revision'], actual, digest(actual), payload,
                                       bool(descriptor and len(descriptor.document_prefix + text) > descriptor.max_embedding_chars)))

    if 'item' in kinds:
        for item in service._item_candidates(filters):
            if item.status == 'active':
                add('item', item.memory_id, item.to_dict(), item.content, {'item': item})
    if 'summary' in kinds:
        for row in service.db.rows("SELECT * FROM lamemory_summaries WHERE user_id=? AND kind='topic' AND dirty=0 AND content!=''", (service.user_id,)):
            if row['project_id'] and row['project_id'] != service.config.project_id:
                continue
            if service.hierarchy.snapshot(row)[1] != row['source_signature']:
                continue
            add('summary', row['summary_id'], row, '主题：' + row['topic_key'] + '\n' + row['content'], row)
    if 'experience' in kinds:
        for row in service.db.rows("SELECT * FROM lamemory_experiences WHERE user_id=? AND status='active'", (service.user_id,)):
            if service._visible(row):
                add('experience', row['experience_id'], row,
                    '\n'.join(['任务：' + row['task_type'], row['conditions'], *json.loads(row['steps_json']), row['result_basis']]), row)
    return result


def visible_ledger(service):
    return [r for r in service.db.rows('SELECT * FROM lamemory_embedding_entities WHERE user_id=?', (service.user_id,))
            if service._visible(r)]


def hydrate(service, doc):
    if doc.entity_kind == 'item':
        return service._item(service._row(doc.entity_id))
    if doc.entity_kind == 'summary':
        return service.hierarchy.inspect(doc.entity_id)
    return service.procedural.inspect(doc.entity_id)
