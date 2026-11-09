# LaMemory M2/M3 开发交付

日期：2026-11-09。当前交付为单机、串行任务消费、关键词检索的功能实现；本次验证使用临时数据库和 fake provider，没有访问真实会话或调用真实模型。

## M2：启用分层整理

首次调用 `refresh_summaries()` 或 `consolidate()` 建立用户概览；配置 `project_id` 时同时建立该项目的固定主题。此后新增、更正、遗忘及来源补充会使相关摘要在写事务内立即变脏，并投递对应 generation 的任务。脏摘要不进入检索或上下文，结构化记录仍可直接检索。摘要重建发布前再次校验来源快照，保留记忆 ID、源 revision/哈希和摘要 revision。

用户概览使用 user 范围的 fact/preference 及项目/主题导航，不包括事件和 session 临时要求；稳定性需要在写入类型和范围时正确表达。项目事实只在绑定相同 `project_id` 的服务中可见。概览可列出其他项目/主题名称作为导航，不加载其项目事实。

```python
from core.lamemory import LaMemoryConfig, LaMemoryService

config = LaMemoryConfig(database_path=".myclaude/example.sqlite3", project_id="atlas")
with LaMemoryService(config, user_id="local-user", session_id="example") as memory:
    item = memory.store("Atlas 使用 uv", scope_kind="project").item
    topic_id = memory.define_topic("atlas.构建", [item.memory_id], project=True)
    memory.refresh_summaries()
    result = memory.flush(timeout=5)
    assert result.status == "complete"
    print(memory.inspect_summary(topic_id))
    print(memory.search_summaries("构建"))
```

没有 `summarize_hierarchy` provider 时使用原文摘录；`ChatMemoryProvider` 提供模型摘要，错误或超长输出进入持久化重试。默认 `max_summary_chars=1200`，完整来源保存在数据库，提示词只携带有限来源导航，可用 `inspect_summary()` 查看全部来源。已验证经验、相关主题、用户概览及结构化记录共同受 `memory_token_budget` 和总输入预算约束。

手动主题的 key 在同一用户内唯一。`define_topic(key, ids)` 为用户主题，不能放入 session 或项目私有记忆；项目主题需指定 `project=True`。再次传入来源列表会替换成员集合；来源被更正时成员关系跟随新版本。项目固定主题自动包含该项目的有效记录。

```bash
python -m core.lamemory --database .myclaude/example.sqlite3 --user local-user --session example --project atlas --offline consolidate --max-jobs 100
python -m core.lamemory --database .myclaude/example.sqlite3 --user local-user --session example --project atlas --offline summaries "构建"
python -m core.lamemory --database .myclaude/example.sqlite3 --user local-user --session example --project atlas --offline summary sum_example
```

`consolidate` 精确合并同用户、同范围/类型/slot/时间的重复有效记录，合并来源并保留旧记录为 superseded。未知时间的事件不合并，不猜测过期时间或裁决语义冲突。返回去重数、处理数、完成/积压/失败任务数、脏摘要数。`--max-jobs` 限制本次任务执行数；遇到重试退避可稍后执行 `flush`。`--offline` 不调用模型，事实/经验提取任务会留下可检查的错误，不会伪报已处理。

`jobs/process/flush/retry` 包含当前会话任务及用户级摘要任务。`flush()` 还追踪消费期间新增的摘要任务；失败和超时均返回明确状态。失败摘要保留旧内容以便检查，但 dirty 时不会使用；使用原 job ID 重试。恢复项目会话或消费其事实/经验任务时，应使用原来的 `--project` 配置。

## M3：积累与验证经验

每条经验保存任务类型、适用条件、步骤、结果依据、原始消息来源、反馈、成功/失败计数和版本。状态为 `candidate → active`，错误经验可撤销为 revoked，替代旧版本为 superseded。只有 active 经验进入 `search_experiences()` 和 `build_context()`。

候选提取有两种入口：

- `capture_experience(completed_turn_id)` 为已提交轮次创建幂等提取任务。
- `LaMemoryConfig(auto_learn_experiences=True)` 在 `commit_turn()` 中同时投递候选提取；默认关闭。终端使用 `--learn-experiences`。任务在主回复之后有界消费，积压可手动 flush。

`ChatMemoryProvider.extract_experiences()` 只提出候选。可信宿主也可通过 `propose_experience(task_type, conditions, steps, result_basis, source_message_ids, scope_kind="user")` 直接保存候选。来源必须属于当前会话的同一已完成轮次，不接受模型填写状态、身份或成功次数。绑定项目的自动候选属于该项目。

```python
# source_ids 引用当前会话的一个已完成任务；以下操作由可信宿主调用。
candidate = memory.propose_experience(
    "构建 Python 项目", "项目包含 uv.lock",
    ["执行 uv sync", "运行项目测试"], "本次构建及测试通过", source_ids,
)
experience_id = candidate["experience_id"]

# independent_source_ids 引用另一个已完成任务；证据由实际测试执行器提供。
memory.experience_feedback(experience_id, "success", independent_source_ids,
                          evidence_kind="test", evidence="测试退出码 0，全部用例通过")
# 第二个独立成功任务达到默认晋升门槛。
# 或者提交用户明确认可：approval_ids 必须引用当前会话中的用户原文。
memory.experience_feedback(experience_id, "approved", approval_ids)
```

默认 `experience_min_successes=2`。独立任务以持久化的 `(session_id, turn_id)` 标识，调用方应将一个任务轨迹放入同一轮次。训练轨迹及同一版本链中的旧训练轨迹不计独立成功；同一版本链的同一反馈轨迹只记录一次。`evidence_kind="model"` 仅留档，不增加成功/失败计数或触发晋升。普通 success 不能恢复 revoked 状态；恢复需要新用户认可或替代候选重新验证。

用户/测试 failure 会立即撤销有效经验。`revoke_experience(id)` 可主动停用；`replace_experience(id, task_type, conditions, steps, result_basis, source_message_ids)` 保留旧来源、反馈与替代链，新版本以 candidate 开始，不继承成功计数。`inspect_experience(id)` 返回原始来源、反馈与版本列表，`list_experiences(status)` 可审阅候选。

```bash
python -m Agent.chatBot_with_memory.main --user local-user --project atlas --learn-experiences
python -m core.lamemory --user local-user --session example --project atlas experience-capture
python -m core.lamemory --user local-user --session example --project atlas process --max-jobs 10
python -m core.lamemory --user local-user --session example --project atlas experiences --status candidate
python -m core.lamemory --user local-user --session example --project atlas experience exp_example
python -m core.lamemory --user local-user --session example --project atlas experience-feedback exp_example approved --sources msg_user_approval
python -m core.lamemory --user local-user --session example --project atlas experience-search "构建"
python -m core.lamemory --user local-user --session example --project atlas experience-revoke exp_example
```

`experience-propose candidate.json` 和 `experience-replace exp_example candidate.json` 接受含五个候选字段的 JSON 文件。测试反馈由宿主/CLI 明确提交，不能把助手的一句“成功”当作实际执行结果。模型可用的工具提供经验搜索、查看和按用户要求撤销；不会把提交测试证据或用户认可的权限交给模型。静态 Skill 加载器继续保留原有职责，经验从服务和上下文读取，不生成或安装 SKILL.md。

## 数据迁移与验证范围

启动时按版本执行 `002_hierarchy.sql` 和 `003_procedural.sql`。M1 的消息、来源、版本和待处理任务保留；重建 SQLite 范围约束后检查外键，迁移和版本标记一起提交。原始证据不会被整理任务删除。

离线验证命令及实际结果见 [验证记录](../benchmarks/lamemory/README.md)。本次涵盖旧库升级、项目隔离、来源版本传播、生成期间更正、失败重试、重启、依赖摘要 flush、精确去重、预算、候选原子写入、反馈重放、自评过滤、晋升、撤销、替代及 CLI 闭环。

尚未验证真实模型的摘要质量和经验迁移收益；没有启用 embedding、自动聚类、后台并发 worker、跨机器同步或自动 Skill 安装。离线通过不等于任务准确率提升。
