"""维护入口在临时数据库上的真实参数解析和持久化闭环。"""
import contextlib
import io
import json
from pathlib import Path
from unittest.mock import patch

from common import MemoryTestCase
from core.lamemory.__main__ import main


class MaintenanceCases(MemoryTestCase):
    def command(self, *args, project=None):
        argv = ["lamemory", "--database", str(self.path), "--user", "alice", "--session", "first", "--offline"]
        if project:
            argv.extend(["--project", project])
        buffer = io.StringIO()
        with patch("sys.argv", [*argv, *args]), contextlib.redirect_stdout(buffer):
            main()
        return json.loads(buffer.getvalue())

    def test_offline_hierarchy_maintenance(self):
        project = self.service(project_id="atlas")
        item = project.store("Atlas 项目使用 uv", scope_kind="project").item
        topic = self.command("topic", "构建", "--project-scope", "--sources", item.memory_id, project="atlas")
        self.command("refresh-summaries", project="atlas")
        self.assertEqual(self.command("flush", "--timeout", "1", project="atlas")["status"], "complete")
        summary = self.command("summary", topic["summary_id"], project="atlas")
        self.assertEqual(summary["content"], item.content)
        self.assertTrue(self.command("summaries", "构建", project="atlas"))
        result = self.command("consolidate", "--max-jobs", "2", project="atlas")
        self.assertEqual((result["duplicates"], result["dirty_summaries"]), (0, 0))

    def test_experience_review_approval_search_and_revoke(self):
        source, turn = self.turn()
        self.memory.commit_turn(turn)
        candidate = dict(task_type="构建", conditions="Python 项目", steps=["运行测试"], result_basis="本轮通过", source_message_ids=[source])
        path = Path(self.tmp.name) / "candidate.json"
        path.write_text(json.dumps(candidate), encoding="utf-8")
        proposed = self.command("experience-propose", str(path))
        experience_id = proposed["experience_id"]
        self.assertEqual(len(self.command("experiences", "--status", "candidate")), 1)
        approval = self.memory.append_message({"role": "user", "content": "认可这份经验"})
        result = self.command("experience-feedback", experience_id, "approved", "--sources", approval)
        self.assertEqual(result["status"], "active")
        self.assertEqual(self.command("experience-search", "构建")[0]["experience_id"], experience_id)
        self.assertEqual(self.command("experience", experience_id)["source_message_ids"], [source])
        self.command("experience-revoke", experience_id)
        self.assertEqual(self.command("experience-search", "构建"), [])
