"""带来源的小型检索验证集，只验证关键词模式。"""
import json
from pathlib import Path

from common import MemoryTestCase


class RetrievalCases(MemoryTestCase):
    def test_labeled_fixture_with_evidence(self):
        data = json.loads((Path(__file__).parent / "fixtures/retrieval.json").read_text(encoding="utf-8"))
        expected = {}
        for row in data["items"]:
            source = self.memory.append_message({"role": "user", "content": row["content"]})
            result = self.memory.store(row["content"], [source], type=row["type"],
                                       scope_kind=row.get("scope_kind", "user"), occurred_at=row.get("occurred_at"))
            expected[row["key"]] = result.item.memory_id
        for row in data["items"]:
            with self.subTest(row["key"]):
                hits = self.memory.search(row["query"])
                self.assertIn(expected[row["key"]], [h.item.memory_id for h in hits])
                for hit in hits:
                    self.assertTrue(hit.item.source_message_ids)
                    self.assertTrue(hit.reason)
                    self.assertEqual(hit.mode, "keyword")
        for query in data["no_answer_queries"]:
            self.assertEqual(self.memory.search(query), [])
        self.assertEqual(self.memory.search(""), [])
        self.assertEqual(self.memory.search("   "), [])

    def test_unknown_time_events_are_not_collapsed(self):
        for _ in range(2):
            source = self.memory.append_message({"role": "user", "content": "又去出差了"})
            self.memory.store("北京出差", [source], type="event")
        self.assertEqual(len(self.memory.search("北京")), 2)

    def test_keyword_mode_is_explicit(self):
        with self.assertRaises(ValueError):
            self.service(retrieval_mode="hybrid")
