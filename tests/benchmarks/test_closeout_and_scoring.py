import copy
import json
from pathlib import Path
import tempfile
import unittest

from Agent.chatBot_with_memory.runner import ChatRunner
from benchmarks.memory_benchmark.backends import NoMemory
from benchmarks.memory_benchmark.scoring import gift_score, resolve_scores, POLICY_VERSION
from benchmarks.memory_benchmark.settings import Settings, SUITE
from benchmarks.memory_benchmark.sandbox import common_tools
from benchmarks.memory_benchmark.smoke import OfflineSandbox


class CloseoutTests(unittest.TestCase):
    def test_three_none_rounds_close_within_shared_budget_without_reading_memory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "MEMORY.md").write_text("hidden-birthday-fact")
            memory = NoMemory(root, "none", Settings())
            requests = []
            def chat(messages, tools=None, **kwargs):
                requests.append({"messages": copy.deepcopy(messages), "tools": tools})
                self.assertNotIn("hidden-birthday-fact", str(messages))
                if not tools:
                    return {"role": "assistant", "content": "文件已保存；生日仍待确认。"}
                return {"role": "assistant", "content": "", "tool_calls": [
                    {"id": f"write_{len(requests)}", "type": "function", "function": {
                        "name": "write", "arguments": {"path": "birthday_reminders.md", "content": "生日：待确认"}}}]}
            runner = ChatRunner(memory, chat=chat, tools=common_tools(root, OfflineSandbox()), memory_tools=False)
            for prompt in ("整理生日提醒", "检查交付物", "最后检查"):
                start = len(requests)
                response = runner.send(prompt, max_rounds=8, max_tool_calls=24)
                self.assertEqual(response["completion_reason"], "budget_closeout")
                self.assertEqual(response["errors"], [])
                self.assertEqual(len(requests) - start, 8)
                self.assertEqual(requests[-1]["tools"], [])
                self.assertIn("最后一次可用工具", requests[-2]["messages"][0]["content"])
            self.assertEqual((root / "MEMORY.md").read_text(), "hidden-birthday-fact")
            self.assertFalse((root / "lamemory.sqlite3").exists())
            self.assertEqual(sum(m["role"] == "assistant" and not m.get("tool_calls") for m in memory.messages), 3)

    def test_model_ignoring_closeout_never_executes_an_extra_tool_or_reports_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            memory = NoMemory(root, "none", Settings())
            def chat(**kwargs):
                return {"role": "assistant", "content": "", "tool_calls": [
                    {"id": "ignored", "type": "function", "function": {"name": "write", "arguments": {
                        "path": "must-not-exist.md", "content": "not executed"}}}]}
            runner = ChatRunner(memory, chat=chat, tools=common_tools(root, OfflineSandbox()), memory_tools=False)
            with self.assertRaisesRegex(RuntimeError, "收尾回复必须"):
                runner.send("test", max_rounds=1)
            self.assertFalse((root / "must-not-exist.md").exists())


class ScoringTests(unittest.TestCase):
    def test_explicit_pending_note_and_negated_purchase_warning_get_full_credit(self):
        text = "## 爸爸生日\n| 字段 | 内容 |\n| 备注 | 给父亲买颈椎按摩仪仍是待办，尚未购买；不要误写成已买好。收货地址待确认。 |"
        self.assertEqual(gift_score(text)[0], 15)
        table = "| 事件 | 日期 | 备注 |\n|---|---|---|\n| 爸爸生日 | 3月20日 | 待办：颈椎按摩仪，未购买 |"
        self.assertEqual(gift_score(table)[0], 15)
        calendar = "BEGIN:VEVENT\nSUMMARY:爸爸生日\nDESCRIPTION:待办：给父亲买颈椎按摩仪，尚未购买\nEND:VEVENT"
        self.assertEqual(gift_score(calendar)[0], 15)
        mixed = "## 爸爸生日\n备注：已经购买生日蛋糕；给父亲买颈椎按摩仪仍是待办，未购买。"
        self.assertEqual(gift_score(mixed)[0], 15)
        named = "## 沈建国生日\n备注：颈椎按摩仪仍是待办，尚未购买。"
        self.assertEqual(gift_score(named, father_names=["沈建国"])[0], 15)

    def test_conditional_device_plan_is_stable_when_only_calendar_arithmetic_changes(self):
        text = "## 爸爸生日\n备注：京东购买礼物。\n待确认：如需补送，可与4月初给爸妈买颈椎按摩仪计划合并，待你确认是否仍执行。\n距生日345天。"
        self.assertEqual(gift_score(text)[0], 10)
        updated = text.replace("345天", "359天")
        self.assertEqual(gift_score(updated), gift_score(text))

    def test_generic_missing_and_contradicted_purchase_have_declared_tiers(self):
        self.assertEqual(gift_score("备注：提前准备礼物，京东购买。")[0], 5)
        self.assertEqual(gift_score("生日待确认")[0], 5)
        self.assertEqual(gift_score("## 爸爸生日\n备注：父亲的颈椎按摩仪已经购买。")[0], 0)

    def test_model_scores_remain_auditable_while_host_gift_decision_stays_fixed(self):
        rubric = json.loads((SUITE / "judge/01_parents_birthday/rubric.json").read_text())
        artifact = {"birthday_reminders.md": "## 爸爸生日\n备注：京东购买。\n给爸妈买颈椎按摩仪的计划，待你确认是否仍执行。"}
        results = []
        for model_score in (10, 15):
            raw = {"criteria": [{"item_id": c["item_id"], "score": model_score if c["item_id"] == "gift-note" else 0,
                                  "reason": "model explanation"} for c in rubric["criteria"]]}
            original = copy.deepcopy(raw)
            result = resolve_scores(raw, rubric, artifact)
            results.append(result["score"])
            decision = result["policy_resolutions"][0]
            self.assertEqual(decision["model_score"], model_score)
            self.assertEqual(decision["resolved_score"], 10)
            self.assertEqual(decision["policy"], POLICY_VERSION)
            self.assertEqual(raw, original)
        self.assertEqual(results, [10, 10])

    def test_changed_rubric_requires_explicit_policy_review(self):
        rubric = json.loads((SUITE / "judge/01_parents_birthday/rubric.json").read_text())
        next(c for c in rubric["criteria"] if c["item_id"] == "gift-note")["max_score"] = 20
        data = {"criteria": [{"item_id": c["item_id"], "score": 0, "reason": "empty"} for c in rubric["criteria"]]}
        with self.assertRaisesRegex(ValueError, "重新审阅"):
            resolve_scores(data, rubric, {})
        rubric = json.loads((SUITE / "judge/01_parents_birthday/rubric.json").read_text())
        next(c for c in rubric["criteria"] if c["item_id"] == "gift-note")["criterion"] += "new requirement"
        with self.assertRaisesRegex(ValueError, "重新审阅"):
            resolve_scores(data, rubric, {})

    def test_other_task_scores_are_not_changed_by_birthday_policy(self):
        rubric = json.loads((SUITE / "judge/02_therapy_schedule/rubric.json").read_text())
        data = {"criteria": [{"item_id": c["item_id"], "score": c["max_score"], "reason": "evidence"} for c in rubric["criteria"]]}
        result = resolve_scores(data, rubric, {})
        self.assertEqual(result["criteria"], data["criteria"])
        self.assertEqual(result["policy_resolutions"], [])
        self.assertEqual(result["score_source"], "model")


if __name__ == "__main__":
    unittest.main()
