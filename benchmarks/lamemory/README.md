# LaMemory 初版验证记录

日期：2026-11-09

当前范围为 M0/M1 的离线行为验证和隔离聊天 smoke，不是完整对照基准。

| 验证 | 结果 | 证据 |
|---|---|---|
| unittest 行为回归 | 34 项通过 | `python -m unittest discover -s tests/lamemory -p 'test_*.py'` |
| provider 协议及联合测试 | 20 项通过，包含 Anthropic 主模型与 DeepSeek 记忆提取的本地协议转换 | `python -m unittest discover -s tests/providers -p 'test_*.py'` |
| 关键词小型验证集 | 20 条有来源记录全部能在 top 5 中找到对应项；3 个无答案查询无结果 | `tests/lamemory/test_retrieval.py` 与 `fixtures/retrieval.json` |
| fake 聊天 smoke | 通过：跨会话召回、来源、用户隔离、上下文选择、回答引用与持久化 | [fake-smoke.json](results/fake-smoke.json) |
| 真实模型 smoke | 通过：DeepSeek `deepseek-flash`，关键词模式的跨会话召回、来源、用户隔离、上下文选择、回答引用与持久化全部通过 | [real-smoke.json](results/real-smoke.json) |

离线用例使用临时库和 fake provider，覆盖来源复用、轮次提交幂等、事件时间、用户/会话隔离、版本更正、待裁决冲突、遗忘与旧来源重放、摘要校验、预算和工具链完整性、提取故障重试、重启恢复、SQLite 外键与批次回滚、只读迁移与重复导入。fake 的零 token 用量不代表真实模型成本。

`smoke.py` 使用不依赖终端输入的 `ChatRunner`，所有状态保存在临时库，执行后删除。报告保存配置、配置哈希、检索模式、命中来源、分项检查、耗时及分用途模型 usage；真实模式不输出密钥。ChatRunner 默认仅启用绑定的记忆工具，宿主可显式增加普通工具；内置安全确认在无人值守模式返回未执行。聊天轮数和模型请求有界，宿主添加的同步工具需要自行遵守超时；此 runner 不提供操作系统级工作区隔离。

后续工作：扩大真实模型样本；再按开发指南冻结同一 Agent 的 none/legacy/lamemory 三组资产、独立工作区、模型预算和评分协议，建立 6–10 个完整任务。尚未运行这些对照，不宣称任务得分或语义召回提升。
