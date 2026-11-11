from unittest.mock import patch
from dataclasses import replace

from common import MemoryTestCase
from core.lamemory.retrieval.types import RetrievalBundle


class ContextEvidenceCases(MemoryTestCase):
    def user(self, text):
        self.memory.append_message({"role": "user", "content": text})

    def end(self):
        self.memory.append_message({"role": "assistant", "content": "已保存。"})

    def seed(self):
        item = self.memory.store("给父亲买颈椎按摩仪仍是待办，尚未购买").item
        self.user("整理父亲颈椎按摩仪待办")
        context = self.memory.build_context()
        self.assertIn(item.memory_id, [h["item"]["memory_id"] for h in context.diagnostics["retrieved"]])
        self.end()
        return item

    def test_missing_in_next_retrieval_keeps_revalidated_fact_across_two_followups(self):
        item = self.seed()
        with patch.object(self.memory, "retrieve", return_value=RetrievalBundle()):
            for prompt in ("检查遗漏", "最终一致性检查"):
                self.user(prompt)
                context = self.memory.build_context()
                self.assertIn("尚未购买", context.messages[0]["content"])
                self.assertIn("revalidated_active", context.messages[0]["content"])
                self.assertEqual(context.diagnostics["retained_evidence"][0]["memory_id"], item.memory_id)
                self.end()

    def test_forget_and_replacement_do_not_restore_cached_old_facts(self):
        item = self.seed()
        updated = self.memory.update(item.memory_id, "父亲的颈椎按摩仪已经购买").item
        self.user("再次检查颈椎按摩仪")
        context = self.memory.build_context()
        ids = [h["item"]["memory_id"] for h in context.diagnostics["retrieved"]]
        self.assertNotIn(item.memory_id, ids)
        self.assertIn(updated.memory_id, ids)
        self.assertNotIn("尚未购买", context.messages[0]["content"])
        self.end()
        self.memory.forget(updated.memory_id)
        self.user("最后检查")
        with patch.object(self.memory, "retrieve", return_value=RetrievalBundle()):
            context = self.memory.build_context()
        self.assertEqual(context.diagnostics["retrieved"], [])
        self.assertEqual(context.diagnostics["retained_evidence"], [])

    def test_retained_references_expire_after_three_user_turns(self):
        self.seed()
        with patch.object(self.memory, "retrieve", return_value=RetrievalBundle()):
            for i in range(3):
                self.user(f"新请求{i}")
                context = self.memory.build_context()
                self.end()
        self.assertEqual(context.diagnostics["retained_evidence"], [])
        self.assertEqual(context.diagnostics["retrieved"], [])

    def test_forget_from_another_connection_invalidates_retained_reference(self):
        item = self.seed()
        other = self.service(session="maintenance")
        other.forget(item.memory_id)
        self.user("再检查")
        with patch.object(self.memory, "retrieve", return_value=RetrievalBundle()):
            context = self.memory.build_context()
        self.assertEqual(context.diagnostics["retained_evidence"], [])
        self.assertNotIn(item.memory_id, str(context.diagnostics["retrieved"]))

    def test_generation_change_invalidates_same_id_even_if_still_active(self):
        item = self.seed()
        with self.memory.db.connection:
            self.memory.db.execute("UPDATE lamemory_items SET revision=revision+1 WHERE memory_id=?", (item.memory_id,))
        self.user("检查")
        with patch.object(self.memory, "retrieve", return_value=RetrievalBundle()):
            context = self.memory.build_context()
        self.assertEqual(context.diagnostics["retained_evidence"], [])
        self.assertNotIn(item.memory_id, str(context.diagnostics["retrieved"]))

    def test_project_scope_change_drops_previous_project_reference(self):
        memory = self.service(session="project", project_id="A")
        item = memory.store("项目A的部署区域是北京", scope_kind="project").item
        memory.append_message({"role": "user", "content": "项目部署区域"})
        memory.build_context()
        memory.append_message({"role": "assistant", "content": "done"})
        memory.config = replace(memory.config, project_id="B")
        memory.append_message({"role": "user", "content": "检查区域"})
        with patch.object(memory, "retrieve", return_value=RetrievalBundle()):
            context = memory.build_context()
        self.assertEqual(context.diagnostics["retained_evidence"], [])
        self.assertNotIn(item.memory_id, str(context.diagnostics["retrieved"]))

    def test_new_session_or_different_user_does_not_receive_session_evidence(self):
        item = self.seed()
        for memory in (self.service(session="another"), self.service(user="bob", session="first")):
            memory.append_message({"role": "user", "content": "检查交付物"})
            with patch.object(memory, "retrieve", return_value=RetrievalBundle()):
                context = memory.build_context()
            self.assertEqual(context.diagnostics["retained_evidence"], [])
            self.assertNotIn(item.memory_id, str(context.diagnostics["retrieved"]))

    def test_small_context_budget_can_drop_evidence_without_retaining_hidden_overflow(self):
        self.seed()
        self.user("检查")
        with patch.object(self.memory, "retrieve", return_value=RetrievalBundle()):
            context = self.memory.build_context(budget=100)
        self.assertLessEqual(context.diagnostics["estimated_tokens"], 100)
        self.assertEqual(context.diagnostics["retained_evidence"], [])
        self.assertEqual(self.memory._context_evidence.references, {})
