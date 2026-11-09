# myClaude Web 工作台

`Agent/chatBot_with_memory` 的本地 Web 入口，复用 `ChatRunner`、工具执行器、模型适配层和 Layered Aggregated Memory（LaMemory）。交互参考 [deepseek-harness Web 工作流](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/user/guide/index.zh.md) 中的会话、模型配置与工具轨迹；界面和后端在本项目独立实现。

## 启动

在项目根目录执行：

```bash
uv sync
uv run python -m Agent.chatBot_with_memory.web
```

浏览器访问 **http://127.0.0.1:3080**。前端是随 Python 服务提供的 HTML/CSS/JavaScript，无需安装 Node 或构建前端。服务只绑定本机地址，不作为公网部署入口。

模型默认读取根目录 `.env` 或进程中的 `LLM_*` 配置（见 [模型提供商文档](model-providers.md)）。未配置模型时仍能打开页面，在「模型设置」里填写提供商、模型 ID 和密钥。网页修改只保存在服务进程内，重启后重新读取环境配置；密钥不会返回页面、写入浏览器存储或写回 `.env`。留空密钥仅在提供商和 API 地址完全不变时保留旧密钥，切换路由需要重新填写。

```bash
# 自定义端口、用户、项目和数据库
uv run python -m Agent.chatBot_with_memory.web \
  --port 3081 --user local-user --project myClaude \
  --database .myclaude/lamemory/lamemory.sqlite3

# 无需模型的离线演示：默认创建临时数据库，退出后清理
uv run python -m Agent.chatBot_with_memory.web --demo
```

`--demo` 使用明确标注的固定演示回复，不请求外部模型；「记住：…」会执行真实的 LaMemory 工具和留档流程。不指定 `--database` 时使用临时数据库，指定后写入指定数据库。演示模式不运行自动记忆提取任务。

## 已实现的交互

- 会话：创建、搜索、切换、重命名、服务重启后恢复；`⌘/Ctrl + K` 新建，`/` 搜索会话。
- 聊天：`Enter` 发送，`Shift + Enter` 换行；基础 Markdown、代码块、回复复制、模型返回的思考内容折叠展示。
- 工具：复用内置文件、命令、搜索及 Skill 工具；显示参数、真实返回结果和执行进度。工具与技能面板列出实际可用能力。
- LaMemory：查询当前可见的用户/会话/项目记忆，关键词搜索、查看消息来源与版本、更正、遗忘。更正调用现有 `update()` 创建新版本；遗忘调用现有 `forget()` 停止召回，保留原文及抑制旧来源重放的标记。
- 上下文：展示本轮实际召回条目、输入预算、工具定义开销和视图裁剪情况；用量是现有 LaMemory 的 UTF-8 估算，非模型精确 tokenizer。
- 整理任务：主回复先展示，再处理最多一个记忆任务；面板展示未完成任务，可手动重试有界处理。
- 外观：浅色/深色模式、小屏侧栏折叠；窄窗口下可通过右上角按钮打开记忆面板。

默认与终端入口共用 `.myclaude/lamemory/lamemory.sqlite3` 和 `local-user`，可恢复同一用户的历史，不自动导入旧 JSONL/Markdown。需要隔离时用 `--user` 和 `--database`。`--project` 在启动时固定绑定，并决定可见的项目记忆范围；界面显示的当前目录只是工具运行目录，不提供动态切换工作目录的功能。

## 运行语义与范围

HTTP 服务使用 Python 标准库，同源 API + SSE。SSE 推送的是模型轮次、完整消息、工具开始/返回和整理状态，**尚未实现模型逐 token 输出**。当前全局串行执行一个 Agent 任务，其他聊天请求返回 409；读取会话仍然可用。活动任务期间不允许手动更改记忆或会话标题；更改模型配置对下一次任务生效。

「停止」是协作式停止：不强杀已发出的模型请求或已启动的工具，在下一处轮次/工具检查点停止。已返回的最终回复可能正常完成；已写入的消息和工具副作用不会回滚。不完整轮次保留原始证据，后续新消息通过现有上下文规则排除不完整的历史链。

现有 `SafetyGuard` 继续生效。Web 入口使用非交互工具执行器；需要确认的操作返回「未执行：需要用户确认」，**尚未实现网页审批弹窗**，也不会等待终端输入。这不是文件系统沙箱，工具遵循当前进程的文件访问能力和原有安全策略。

会话、工具结果和记忆持久化到 SQLite；运行事件缓存和上下文诊断只在本次服务内保留。刷新页面可重新附着仍在运行的任务，服务重启后可恢复原始会话，无法继续上次进程中的任务。Web 会话显示名称存于独立 `lamemory_web_sessions` 表，不修改原始消息。HTTP 写入要求同源请求与页面令牌，响应不启用跨域访问。

## 验证

```bash
uv run python -m unittest discover -s tests/lamemory -v
uv run python -m unittest discover -s tests/providers -v
node --check Agent/chatBot_with_memory/web_static/app.js
```

Web 测试全部使用临时数据库和替代模型，覆盖会话恢复、身份和作用域隔离、真实工具/来源链、更正与遗忘、取消后的恢复、错误状态、密钥隔离、HTTP 同源约束、静态文件及 SSE 重放。HTTP 测试需要环境允许绑定 loopback 临时端口。

浏览器离线演示验证包括：初始页面、示例消息发送、工具执行结果、记忆保存、来源查看和本轮上下文。未进行真实供应商 Web 端到端调用，也未对所有设备尺寸进行实机验证。
