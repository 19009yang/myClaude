"""会话内有限的上下文证据引用；不缓存正文，不恢复失效或不可见的记录。"""
from collections import OrderedDict
from dataclasses import replace

from ..models import SearchHit
from ..retrieval.sources import documents


class ContextEvidence:
    def __init__(self):
        self.references = OrderedDict()

    def merge(self, service, rows, fresh):
        turns = list(dict.fromkeys(r["turn_id"] for r in rows if r["role"] == "user"))[-3:]
        current = turns[-1] if turns else None
        authoritative = {doc.entity_id: doc for doc in documents(service, None, kinds=("item",))}
        fresh = [replace(hit, item=authoritative[hit.item.memory_id].payload["item"]) for hit in fresh
                 if hit.item.memory_id in authoritative and
                 authoritative[hit.item.memory_id].payload["item"].content_hash == hit.item.content_hash and
                 authoritative[hit.item.memory_id].revision == hit.item.revision]
        current_hits = {hit.item.memory_id: hit for hit in fresh}
        kept, candidates = OrderedDict(), []
        for ident, (origin_turn, generation) in self.references.items():
            doc = authoritative.get(ident)
            if origin_turn not in turns or doc is None or doc.entity_generation != generation:
                continue
            kept[ident] = (origin_turn, generation)
            if origin_turn == current:
                continue
            hit = current_hits.get(ident) or SearchHit(doc.payload["item"], 0, "此前已注入，当前重新校验有效", mode="retained")
            candidates.append(replace(hit, item=doc.payload["item"], retrieval={**hit.retrieval,
                "retained_from_turn": origin_turn, "verified_generation": generation}))
        self.references = kept
        included = {h.item.memory_id for h in candidates}
        candidates.extend(h for h in fresh if h.item.memory_id not in included)
        return candidates, current, authoritative

    def remember(self, service, context, current_turn, authoritative):
        context.diagnostics["retained_evidence"] = []
        if current_turn is None:
            return
        # Only references actually included by the shared memory/context budget survive.
        selected = [h["item"]["memory_id"] for h in context.diagnostics["retrieved"]]
        refs = OrderedDict()
        for ident in selected[:service.config.top_k]:
            doc = authoritative.get(ident)
            if doc is not None:
                refs[ident] = self.references.get(ident, (current_turn, doc.entity_generation))
        self.references = refs
        context.diagnostics["retained_evidence"] = [
            {"memory_id": h["item"]["memory_id"], "origin_turn": h["retrieval"]["retained_from_turn"],
             "verified_generation": h["retrieval"]["verified_generation"]}
            for h in context.diagnostics["retrieved"] if h["retrieval"].get("retained_from_turn")]
