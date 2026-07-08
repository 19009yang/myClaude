# Learn — LLM Agent 实验项目

一个基于 Python 的 LLM Agent 实验项目，包含对话记忆管理、工具调用安全校验、工作流编排等核心能力的实现。项目从零构建了一个类 Claude Code 的智能助手框架，适合学习 Agent 开发的核心概念。

## ✨ 核心特性

- **🧠 记忆管理** — 短期对话上下文 + 长期记忆自动提取，上下文接近上限时自动摘要压缩
- **🔧 工具调用** — 内置 7 种工具（read/search/bash/ls/edit/grep/write），支持 LLM function calling
- **🛡️ 安全校验** — 分级拦截策略（blocked/confirm/pass），YAML 配置，防止危险操作
- **🔄 工作流编排** — Node/Flow 模式，支持条件分支和重试机制，用 `>>` 语法链式定义节点
- **🤖 Agent 示例** — 带记忆与工具的对话机器人、搜索摘要工作流

## 📁 项目结构

```
learn/
├── core/               # 核心模块
│   ├── llm.py          # LLM 调用封装（文本生成 + 工具调用模式）
│   ├── memory.py       # 记忆管理（对话历史 + 长期记忆 + 自动压缩）
│   ├── node.py         # 工作流编排（Node/Flow）
│   └── chat_memory/    # 记忆存储目录
├── tools/              # 工具系统
│   ├── builtins/       # 内置工具实现
│   │   ├── bash.py     # 命令执行
│   │   ├── read.py     # 文件读取
│   │   ├── write.py    # 文件写入
│   │   ├── edit.py     # 文件编辑
│   │   ├── ls.py       # 目录列表
│   │   ├── grep.py     # 内容搜索
│   │   ├── search.py   # 网络搜索（DuckDuckGo）
│   │   └── tool_def.py # Tool 定义与注册
│   ├── executor.py     # 工具执行器（解析 + 执行 + 安全校验）
│   ├── guard.py        # 安全校验器
│   ├── safety_policy.yaml  # 安全策略配置
│   ├── skill_loader.py # Skill 加载器（frontmatter 解析）
│   └── skills/         # 自定义 Skill 目录
├── Agent/              # Agent 应用示例
│   ├── chatBot_with_memory/  # 带记忆和工具的对话机器人
│   └── workflow/              # 搜索摘要工作流示例
├── main.py             # 入口文件
└── pyproject.toml      # 项目配置
```

## 🚀 快速开始

### 环境准备

```bash
# 安装依赖（推荐使用 uv）
uv sync

# 配置 LLM API（在 core/ 目录下创建 .env 文件）
# 需要配置以下环境变量：
# LLM_API_KEY=your_api_key
# LLM_BASE_URL=your_base_url
# LLM_MODEL_NAME=your_model_name
```

### 运行示例

```bash
# 带记忆和工具的对话机器人
python Agent/chatBot_with_memory/main.py

# 搜索摘要工作流
python Agent/workflow/main.py
```

## 🧩 核心模块说明

### Memory — 记忆管理

`core/memory.py` 实现了双层记忆架构：

- **短期记忆**：对话历史存储在 JSONL 文件中，每条消息逐行追加
- **长期记忆**：每轮对话结束后，LLM 自动提取用户偏好和关键事实，追加到长期记忆文件
- **自动压缩**：当 token 数达到上限的 90% 时，自动将较早对话压缩为摘要，保留最近几轮
- **崩溃恢复**：启动时检测并清理未完成的 tool_call 消息链

```python
from core import Memory

memory = Memory()
memory.add_message({"role": "user", "content": "你好"})
# 构建带长期记忆的上下文
messages = memory.build_context(system_prompt="你是一个助手")
```

### Node / Flow — 工作流编排

`core/node.py` 实现了简洁的 DAG 工作流：

- **Node**：每个节点实现 `exec(payload)` 返回 `(action, next_payload)`
- **Flow**：按 action 路由到下一个节点，支持重试机制
- **链式语法**：用 `>>` 和 `-` 操作符定义节点关系

```python
from core import Node, Flow

class MyNode(Node):
    def exec(self, payload):
        result = do_something(payload)
        if success:
            return "next", result
        return "retry", payload

node_a = MyNode()
node_b = AnotherNode()

node_a - "next" >> node_b  # action 为 "next" 时流转到 node_b
node_a - "retry" >> node_a  # action 为 "retry" 时重试自身

flow = Flow(node_a)
flow.run(initial_payload)
```

### LLM — 大模型调用

`core/llm.py` 提供两个调用接口：

- `call_llm_chat(prompt)` — 简单文本生成
- `call_llm(messages, tools, system_prompt)` — 完整消息模式，支持 function calling 和 reasoning_content

### Tools — 工具系统

`tools/` 模块实现了完整的工具调用生命周期：

| 工具 | 说明 |
|------|------|
| `read` | 读取文件内容，支持 offset/limit 分段读取 |
| `write` | 写入/创建文件 |
| `edit` | 替换文件中的文本片段 |
| `ls` | 列出目录内容 |
| `grep` | 搜索文件内容，支持正则、glob 过滤、上下文行 |
| `bash` | 执行终端命令 |
| `search` | DuckDuckGo 网络搜索 |

```python
from tools import get_tools, execute_tool

# 获取工具列表（可直接传给 LLM API）
tools = get_tools()

# 执行单个工具
result = execute_tool("ls", {"path": "."})
```

### SafetyGuard — 安全校验

`tools/guard.py` 在工具执行前进行分级拦截：

- **blocked**：绝对拦截（如 `rm -rf /`、写入系统目录）
- **confirm**：需用户确认（如 `sudo`、删除文件、修改敏感配置）
- **pass**：安全放行

策略通过 `safety_policy.yaml` 配置，支持正则匹配 bash 命令和路径校验。

### Skill — 技能系统

`tools/skill_loader.py` 支持加载 Markdown frontmatter 格式的技能文件：

```markdown
---
name: hello
description: 示例技能
---
技能正文内容...
```

## 📋 依赖

- Python ≥ 3.13
- openai — LLM API 调用
- python-dotenv — 环境变量管理
- ddgs — DuckDuckGo 搜索
- pyyaml — YAML 解析
- chardet — 编码检测

## 📝 License

本项目仅供学习实验使用。
