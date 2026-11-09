"""M3：候选、独立证据、晋升、撤销、版本、重放与会话接入。"""
from unittest.mock import Mock, patch

from common import MemoryTestCase
from core.lamemory.providers import ChatMemoryProvider
from core.lamemory.tools import get_memory_tools


class ProceduralCases(MemoryTestCase):
    def completed(self, memory=None, content="完成构建任务"):
        memory = memory or self.memory
        source = memory.append_message({"role": "user", "content": content})
        memory.append_message({"role": "assistant", "content": "测试通过，任务完成"})
        memory.commit_turn()
        return source

    def candidate(self, memory=None, *, source=None, scope_kind="user", steps=None):
        memory = memory or self.memory
        source = source or self.completed(memory)
        return memory.propose_experience("构建 Python 项目", "存在 uv.lock 的 Python 项目", steps or ["执行 uv sync", "运行项目测试"],
                                         "当前任务测试通过，但尚需独立使用验证", [source], scope_kind=scope_kind)

    def test_candidate_is_persistent_but_not_retrieved_until_independent_successes(self):
        candidate = self.candidate()
        experience_id = candidate["experience_id"]
        self.assertEqual(candidate["status"], "candidate")
        self.assertEqual(self.memory.search_experiences("构建"), [])
        # Training replay and the same trajectory do not count as independent success.
        sources = candidate["source_message_ids"]
        self.memory.experience_feedback(experience_id, "success", sources, evidence_kind="test", evidence="训练任务已通过")
        replay = self.memory.experience_feedback(experience_id, "success", sources, evidence_kind="test", evidence="重放")
        self.assertEqual(replay["successes"], 0)
        for count in (1, 2):
            source = self.completed(content=f"独立构建任务 {count}")
            result = self.memory.experience_feedback(experience_id, "success", [source], evidence_kind="test", evidence="实际测试通过")
            self.assertEqual(result["successes"], count)
            self.memory.experience_feedback(experience_id, "success", [source], evidence_kind="test", evidence="重复提交")
            self.assertEqual(self.memory.inspect_experience(experience_id)["successes"], count)
        other = self.service(session="second")
        hit = other.search_experiences("构建")[0]
        self.assertEqual(hit["status"], "active")
        self.assertEqual(hit["source_message_ids"], sources)
        self.assertEqual(hit["sources"][0]["payload"]["role"], "user")
        other.append_message({"role": "user", "content": "构建 Python 项目"})
        context = other.build_context()
        self.assertEqual(context.diagnostics["experiences"][0]["experience_id"], experience_id)
        self.assertEqual(context.diagnostics["experiences"][0]["conditions"], candidate["conditions"])

    def test_model_self_evaluation_never_promotes_but_user_approval_can(self):
        candidate = self.candidate()
        experience_id = candidate["experience_id"]
        for _ in range(3):
            source = self.completed()
            result = self.memory.experience_feedback(experience_id, "success", [source], evidence_kind="model")
            self.assertEqual((result["successes"], result["status"]), (0, "candidate"))
        approval = self.memory.append_message({"role": "user", "content": "我确认这份构建经验正确，可以复用"})
        result = self.memory.experience_feedback(experience_id, "approved", [approval])
        self.assertEqual(result["status"], "active")
        self.assertEqual(result["successes"], 0)
        # Model tools cannot declare test evidence or manufacture user approval.
        names = {tool.name for tool in get_memory_tools(self.memory)}
        self.assertIn("lamemory_experience_search", names)
        self.assertNotIn("lamemory_experience_approve", names)

    def test_failure_revokes_immediately_and_version_replacement_resets_validation(self):
        candidate = self.candidate()
        experience_id = candidate["experience_id"]
        approval = self.memory.append_message({"role": "user", "content": "认可此经验"})
        self.memory.experience_feedback(experience_id, "approved", [approval])
        self.memory.append_message({"role": "assistant", "content": "已记录认可"})
        self.memory.commit_turn()
        failure = self.completed(content="采用该经验后构建失败")
        result = self.memory.experience_feedback(experience_id, "failure", [failure], evidence_kind="test", evidence="测试退出码 1")
        self.assertEqual((result["status"], result["failures"]), ("revoked", 1))
        self.assertEqual(self.memory.search_experiences("构建"), [])
        correction = self.completed(content="修复后的构建任务")
        new = self.memory.replace_experience(experience_id, "构建 Python 项目", "存在 uv.lock", ["先确认 Python 版本", "再执行 uv sync"], "修复后测试通过", [correction])
        self.assertEqual((new["status"], new["revision"], new["successes"]), ("candidate", 2, 0))
        old = self.memory.inspect_experience(experience_id)
        self.assertEqual(old["status"], "superseded")
        self.assertEqual(old["superseded_by"], new["experience_id"])
        self.assertEqual(len(new["versions"]), 2)
        with self.assertRaises(ValueError):
            self.memory.experience_feedback(experience_id, "success", [correction])
        # Previously counted trajectory cannot be counted again on a replacement.
        self.memory.experience_feedback(new["experience_id"], "failure", [failure], evidence_kind="test", evidence="同轨迹重放")
        self.assertEqual(self.memory.inspect_experience(new["experience_id"])["failures"], 0)
        training = candidate["source_message_ids"]
        result = self.memory.experience_feedback(new["experience_id"], "success", training, evidence_kind="test", evidence="旧训练轨迹重放")
        self.assertEqual(result["successes"], 0)

    def test_explicit_revoke_survives_restart_and_success_does_not_auto_restore(self):
        candidate = self.candidate()
        experience_id = candidate["experience_id"]
        self.memory.revoke_experience(experience_id)
        for _ in range(2):
            source = self.completed()
            self.memory.experience_feedback(experience_id, "success", [source], evidence_kind="test", evidence="通过")
        resumed = self.service(session="second")
        self.assertEqual(resumed.inspect_experience(experience_id)["status"], "revoked")
        self.assertEqual(resumed.search_experiences("构建"), [])

    def test_project_and_user_sources_are_isolated(self):
        project = self.service(project_id="atlas")
        candidate = self.candidate(project, scope_kind="project")
        approval = project.append_message({"role": "user", "content": "认可此经验"})
        project.experience_feedback(candidate["experience_id"], "approved", [approval])
        self.assertTrue(self.service(session="second", project_id="atlas").search_experiences("构建"))
        other = self.service(session="other", project_id="other")
        stranger = self.service(user="bob", project_id="atlas")
        for service in (other, stranger):
            self.assertEqual(service.search_experiences("构建"), [])
            with self.assertRaises(KeyError):
                service.inspect_experience(candidate["experience_id"])
            with self.assertRaises(KeyError):
                service.revoke_experience(candidate["experience_id"])
        candidate = self.candidate()
        bob_source = self.completed(stranger)
        with self.assertRaises(ValueError):
            self.memory.experience_feedback(candidate["experience_id"], "approved", [bob_source])
        assistant = self.memory.history()[-1]["message_id"]
        with self.assertRaises(ValueError):
            self.memory.experience_feedback(candidate["experience_id"], "approved", [assistant])

    def test_capture_is_idempotent_and_provider_cannot_assign_validation(self):
        source = self.completed()
        job_id = self.memory.capture_experience()
        self.assertEqual(job_id, self.memory.capture_experience())
        def extract(messages, *, max_items, timeout):
            self.assertFalse(self.memory.db.connection.in_transaction)
            return [{"task_type": "构建", "conditions": "使用 Python", "steps": ["运行测试"],
                     "result_basis": "本轮运行完成", "source_message_ids": [source]}]
        self.provider.extract_experiences = extract
        resumed = self.service()
        self.assertEqual(resumed.flush(timeout=1).status, "complete")
        experiences = resumed.list_experiences()
        self.assertEqual(len(experiences), 1)
        self.assertEqual(experiences[0]["status"], "candidate")
        self.assertEqual(resumed.procedural.propose(extract([], max_items=3, timeout=1)[0])["experience_id"], experiences[0]["experience_id"])
        self.assertEqual(len(resumed.list_experiences()), 1)

    def test_malformed_experience_batch_is_retryable_and_atomic(self):
        source = self.completed()
        job_id = self.memory.capture_experience()
        valid = dict(task_type="构建", conditions="Python", steps=["测试"], result_basis="完成", source_message_ids=[source])
        self.provider.extract_experiences = Mock(return_value=[valid, {**valid, "source_message_ids": ["foreign"]}])
        self.assertEqual(self.memory.flush(timeout=1).status, "failed")
        self.assertEqual(self.memory.list_experiences(), [])
        self.assertTrue(self.memory.retry_failed(job_id))
        self.provider.extract_experiences.return_value = [valid, {**valid, "status": "active"}]
        self.assertEqual(self.memory.flush(timeout=1).status, "failed")
        self.assertEqual(self.memory.list_experiences(), [])
        self.memory.retry_failed(job_id)
        self.provider.extract_experiences.return_value = [valid, {**valid, "task_type": "第二条"}]
        original = self.memory.procedural.propose
        def save(candidate, **kwargs):
            if candidate["task_type"] == "第二条":
                raise OSError("模拟第二项持久化失败")
            return original(candidate, **kwargs)
        with patch.object(self.memory.procedural, "propose", side_effect=save):
            self.memory.process_pending(10)
        self.assertEqual(self.memory.list_experiences(), [])

    def test_auto_learning_is_explicit_and_requires_completed_turn(self):
        memory = self.service(session="learn", auto_learn_experiences=True)
        memory.append_message({"role": "user", "content": "尚未完成"})
        with self.assertRaises(ValueError):
            memory.capture_experience()
        memory.append_message({"role": "assistant", "content": "完成"})
        memory.commit_turn()
        self.assertEqual({job["kind"] for job in memory.jobs()}, {"extract", "experience"})
        self.assertEqual(len(memory.jobs()), 2)
        memory.commit_turn()
        self.assertEqual(len(memory.jobs()), 2)

    def test_provider_records_separate_hierarchy_and_experience_usage(self):
        source = self.completed()
        chat = Mock(side_effect=[{"content": '{"summary":"背景"}', "usage": {"total_tokens": 3}},
                                 {"content": "[]", "usage": {"total_tokens": 4}}])
        provider = ChatMemoryProvider(chat)
        provider.summarize_hierarchy({"sources": []}, timeout=1)
        provider.extract_experiences(self.memory.history(), max_items=3, timeout=1)
        self.assertEqual([e["purpose"] for e in provider.usage_events], ["hierarchy", "experience"])
        self.assertEqual([e["usage"]["total_tokens"] for e in provider.usage_events], [3, 4])
