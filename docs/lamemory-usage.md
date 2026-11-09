# LaMemory v0.1 使用与验证

日期：2026-11-09

当前已实现 M0/M1 的单机运行时，检索模式为 `keyword`。支持事实、偏好、事件、用户/会话范围、原始证据、版本更正、待裁决冲突、遗忘抑制、持久化提取任务和上下文预算。主题摘要、用户概览、经验学习、embedding 和完整对照评测仍属后续工作。DeepSeek `deepseek-flash` 已通过真实记忆 smoke，记录见 [real-smoke.json](../benchmarks/lamemory/results/real-smoke.json)。

## 启动聊天

在项目根目录配置 `.env`，填写模型名称及密钥；使用 `LLM_PROVIDER` 选择 OpenAI、Anthropic、DeepSeek、Qwen 或自定义兼容接口。主模型和记忆模型分开配置的方法见 [模型提供商适配层](model-providers.md)。Python 运行环境使用项目 `.venv`，也可用 `uv run python` 替换下列 `python`。

```bash
python -m Agent.chatBot_with_memory.main --user local-user
python -m Agent.chatBot_with_memory.main --user local-user --session session-example
```

不指定 `--session` 时每次启动创建新会话；终端会打印会话 ID，后续指定该 ID 可以恢复原会话。同一 `--user` 的用户级记忆跨会话共享，session 记忆仅对原会话可见。

默认数据库是项目内 `.myclaude/lamemory/lamemory.sqlite3`，不随启动目录变化，并已加入 Git 忽略。可用 `--database /absolute/path/memory.sqlite3` 隔离数据。`--context-window` 和 `--reserved-output` 应按实际模型设置；默认值分别为 32768 与 4096，另保留 512 安全余量。

用户可以直接说“记住……”“查找关于……的记忆”“把这条记忆改为……”“忘记这条记忆”。工具返回 `memory_id`、状态和来源 ID；`saved` 表示已持久化，`duplicate` 表示复用已有条目，`conflict` 表示待裁决且不进入默认检索，`suppressed` 表示未重新保存。不能将后两种状态描述为“已记住”。

最终回复保存后投递提取任务，展示回复后至多消费一个任务。提取不依赖 usage 是否存在。初版只有串行任务消费；不要让多个进程同时消费同一个库的任务。provider 必须遵守传入的 timeout；默认模型适配器限制请求超时并关闭客户端内部重试。

## Python 接口

```python
from core.lamemory import LaMemoryConfig, LaMemoryService
from core.lamemory.providers import ChatMemoryProvider

config = LaMemoryConfig(database_path=".myclaude/example.sqlite3")
with LaMemoryService(config, user_id="local-user", session_id="example",
                     provider=ChatMemoryProvider()) as memory:
    source = memory.append_message({"role": "user", "content": "开发回答请使用中文"})
    saved = memory.store("开发回答使用中文", [source], type="preference",
                         slot_key="response.language.development")
    memory.append_message({"role": "assistant", "content": "已保存"})
    memory.commit_turn()                 # 相同轮次重复调用返回原任务
    context = memory.build_context("你是一个助手。", tools=[])
    print(context.diagnostics)            # 实际注入的 ID、来源、分数、预算和命中原因
    print(memory.inspect(saved.item.memory_id))
```

`LaMemoryService` 不默认调用外部模型；需要自动提取时传入 provider。测试可传入实现 `extract(messages, *, max_items, timeout)` 的 fake；可选 `summarize(messages, *, timeout)` 用于工作摘要。`ChatMemoryProvider(chat=...)` 可以绑定使用显式 client/model 的模型函数，各实例不必修改全局环境。

保留的 `Memory.add_message()` 和 `build_context()` 接口转发到同一服务。`Memory(memory_path=...)` 将该参数定义为旧 JSONL 导入源，在源文件旁使用独立数据库，发出弃用提示；新代码应明确提供 `database_path`。仅支持旧 `memory_type="memory"`。

关键词检索采用规范化英文词和中文双字片段，没有 embedding，也不保证同义改写召回。`search(query, filters=..., top_k=...)` 支持 `type`、`scope_kind`、`status`、`occurred_after`、`occurred_before` 过滤；空查询返回空结果。只有显式历史查询才包含 deleted/superseded 项。未确定的事件时间保留 null，不用写入时间代替。

相同内容去重并合并来源；未知时间、不同来源的事件各自保留。同 slot 的普通冲突进入 pending；明确 `correction=True` 或 `update(id, content, sources)` 才创建替代版本。`inspect()` 返回原始来源和版本链。自动提取只使用本轮用户消息为证据，模型不能填写身份、状态或版本；本轮主动保存的相同内容不会因提取类型不同重复新增。

`forget()` 停止检索并保留原文与审计记录，不是物理删除。抑制覆盖被忘记的内容、slot 和旧来源，并包括旧版本；这是保守策略，同一旧消息中的其他未提取事实也可能被抑制。重新保存同一内容需要新用户指令；旧来源的主动工具重放同样不能解除抑制。可信宿主也可以无来源显式保存。

工作上下文按“减少召回记忆 → 删除较旧完整轮次/工作摘要 → 截短当前工具结果视图”收缩。原始消息始终留档。系统提示、工具 schema 或当前不可裁剪内容仍超限时抛出 `BudgetError`。预算计数是带包装的 UTF-8 估算，诊断中标记 `utf8_estimate`，并非精确 tokenizer。可调用 `compress()` 生成可选工作摘要，校验失败保留原视图，更正和遗忘会使已有摘要失效；它不属于 M2 的跨会话主题聚合。

## 任务检查与维护

所有命令都使用同一组全局 `--database`、`--user`、`--session` 参数，放在子命令之前。

```bash
python -m core.lamemory --user local-user --session example jobs
python -m core.lamemory --user local-user --session example process --max-jobs 1
python -m core.lamemory --user local-user --session example flush --timeout 30
python -m core.lamemory --user local-user --session example retry job_example
python -m core.lamemory --user local-user search "开发回答"
python -m core.lamemory --user local-user inspect lam_example
```

任务默认最多尝试三次（包含首次），错误保存在 `last_error`，耗尽后 failed。`retry` 对原 job 复位，不创建新的幂等键。`flush` 等待本次目标会话的任务快照，返回 complete/failed/timeout 和剩余数量；complete 才代表该范围的提取已完成且查询可见。维护命令不会自动遍历其他会话。

## 旧文件迁移与回滚

默认启动不自动导入旧的真实会话。先发现路径和只读预览，再明确执行导入。POSIX 上带反斜杠的旧文件名也可发现和显式传入。

```bash
python -m core.lamemory discover .
python -m core.lamemory --user local-user --session imported preview --session-file core/chat_memory/session.jsonl --memory-file core/chat_memory/MEMORY.md
python -m core.lamemory --user local-user --session imported import --session-file core/chat_memory/session.jsonl --memory-file core/chat_memory/MEMORY.md
```

preview 不创建数据库或备份，返回合法消息数量、非法行位置、Markdown 条目数、文件哈希和目标身份。import 先备份源文件到数据库旁的 `backups/<batch_id>/`，写入 manifest，再事务导入；重复输入不新增数据。Markdown 的 `-`/`*` 列表项标记为 legacy_import，不伪造来源或事件时间；非法 JSONL 行在报告中保留位置，原文件和备份均保留原始字节。导入不会自动创建历史提取任务。

回滚时停止聊天进程，保留新库以便排查，切回部署前的应用版本，使用未修改的旧文件或校验过的备份。新版本不向 Markdown 追加，因此旧后端无法看到新版本期间产生的记录。切回新版本时继续使用新库，不重复向旧文件写入。

## 验证记录与后续

```bash
python -m unittest discover -s tests/lamemory -p 'test_*.py'
python -m benchmarks.lamemory.smoke --output benchmarks/lamemory/results/fake-smoke.json
python -m benchmarks.lamemory.smoke --real --output benchmarks/lamemory/results/real-smoke.json
```

所有测试和 smoke 都使用临时数据库及合成消息，不读取 `core/chat_memory/`。真实 smoke 使用环境中的模型配置；缺少配置时以退出码 2 和 blocked 状态报告，不能当作通过。带来源的小型关键词验证集位于 `tests/lamemory/fixtures/retrieval.json`，包含 20 条事实/偏好/事件及 3 个无答案查询。

离线与真实 smoke 结果见 [验证记录](../benchmarks/lamemory/README.md)。真实 smoke 通过表示该配置的基础调用和记忆流程可用，没有形成模型准确率结论或完整对照评测分数。M2 的主题摘要、用户概览、来源版本传播和增量整理，以及 M3 的经验候选与反馈晋升均未实现。
