"""有界离线整理：精确去重、传播已确定状态、刷新摘要和报告积压。"""
from ..storage.store import utc_now


def consolidate(service, *, max_jobs=100):
    if not isinstance(max_jobs, int) or max_jobs < 0:
        raise ValueError("max_jobs 必须为非负整数")
    db = service.db
    duplicates = 0
    groups = {}
    with db.connection:
        rows = db.rows("SELECT * FROM lamemory_items WHERE user_id=? AND status='active' ORDER BY created_at,memory_id", (service.user_id,))
        for row in rows:
            # Unknown-time events may be distinct even when their text is identical.
            if row["type"] == "event" and row["occurred_at"] is None:
                continue
            key = tuple(row[k] for k in ("scope_kind", "scope_key", "type", "content_hash", "slot_key", "occurred_at"))
            keeper = groups.setdefault(key, row)
            if keeper["memory_id"] == row["memory_id"]:
                continue
            db.execute("INSERT OR IGNORE INTO lamemory_sources SELECT user_id,?,message_id FROM lamemory_sources WHERE user_id=? AND memory_id=?",
                       (keeper["memory_id"], service.user_id, row["memory_id"]))
            db.execute("UPDATE lamemory_items SET status='superseded',superseded_by=?,updated_at=? WHERE user_id=? AND memory_id=?",
                       (keeper["memory_id"], utc_now(), service.user_id, row["memory_id"]))
            duplicates += 1
        if duplicates:
            service._invalidate_views()
    service.refresh_summaries()
    processed = []
    for _ in range(max_jobs):
        result = service.process_pending(1)
        if not result:
            break
        processed.extend(result)
    jobs = service.jobs()
    return {"duplicates": duplicates, "processed": len(processed),
            "completed": sum(j["status"] == "completed" for j in processed),
            "pending": sum(j["status"] == "pending" for j in jobs),
            "failed": sum(j["status"] == "failed" for j in jobs),
            "dirty_summaries": db.one("SELECT COUNT(*) AS n FROM lamemory_summaries WHERE user_id=? AND dirty=1", (service.user_id,))["n"],
            "raw_messages_retained": True}
