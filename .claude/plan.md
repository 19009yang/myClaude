# Plan：研究助手工作流 (ResearchAssistant Workflow)

## 需求回顾

用户要求的完整流程：
> 论文搜索 → 挑选感兴趣论文下载 → 解析下载的论文 → 阅读论文提出写作主题 → user 评价后开始写作 → LaTeX 渲染成 PDF → 结束

## 设计方案

### 整体架构

采用项目现有的 **Node/Flow DAG** 模式构建工作流，位于 `Agent/ResearchAssistant/` 目录。
流程由 9 个核心 Node 组成，通过 action 字符串连接形成 DAG：

```
SearchNode → SelectNode → DownloadNode → ParseNode → TopicNode → ReviewGateNode → WritingNode → RenderNode → EndNode
```

其中 **ReviewGateNode** 是人工审批节点，等待用户确认后才继续写作。

### Node 设计

| # | Node | 类型 | 职责 | 输入 payload | 输出 payload | action |
|---|------|------|------|-------------|-------------|--------|
| 1 | **SearchNode** | 程序化 | 调用 `search_papers` 搜索论文 | `{"query": str}` | `{"query": ..., "papers": [list]}` | `"select"` |
| 2 | **SelectNode** | LLM驱动 | LLM 分析搜索结果，推荐最感兴趣的论文；展示给用户确认 | `{"query": ..., "papers": [list]}` | `{"query": ..., "selected_ids": [list]}` | `"download"` |
| 3 | **DownloadNode** | 程序化 | 调用 `download_papers` 下载选定论文 PDF | `{"query": ..., "selected_ids": [list]}` | `{"query": ..., "selected_ids": [...], "downloaded": {id: path}}` | `"parse"` |
| 4 | **ParseNode** | 程序化 | 调用 PyMuPDF 提取 PDF 文本为 Markdown | `{"query": ..., "downloaded": {id: path}}` | `{"query": ..., "parsed_papers": [{id, title, text}]}` | `"topic"` |
| 5 | **TopicNode** | LLM驱动 | LLM 阅读论文，提出 2-3 个写作主题建议 | `{"query": ..., "parsed_papers": [list]}` | `{"topics": [{title, angle, rationale}], "parsed_papers": [...]}` | `"review"` |
| 6 | **ReviewGateNode** | 交互式 | 展示主题，等待用户选择/修改/要求重新提议 | `{"topics": [...], "parsed_papers": [...]}` | `{"chosen_topic": {...}, "parsed_papers": [...]}` | `"write"` 或 `"back_to_topic"` |
| 7 | **WritingNode** | LLM驱动 | 基于选定主题+论文内容撰写 LaTeX 格式论文 | `{"chosen_topic": {...}, "parsed_papers": [...]}` | `{"latex_content": str, "chosen_topic": {...}}` | `"render"` |
| 8 | **RenderNode** | 程序化 | 调用 LaTeX 编译工具，.tex → PDF | `{"latex_content": str}` | `{"pdf_path": str, "chosen_topic": {...}}` | `"end"` 或 `"render_error"` |
| 9 | **EndNode** | 输出 | 展示最终报告 | `{"pdf_path": ..., "chosen_topic": ...}` | None | `"done"` |

### DAG 连接

```python
search - "select" >> select
select - "download" >> download
download - "parse" >> parse
parse - "topic" >> topic
topic - "review" >> review_gate
review_gate - "write" >> writing
review_gate - "back_to_topic" >> topic    # 用户不满意，重新提议
writing - "render" >> render
render - "end" >> end
render - "render_error" >> end            # LaTeX编译失败，保存.tex并告知用户
search - "error" >> end                   # 搜索失败
```

### Node 类型说明

- **程序化 Node**（SearchNode/DownloadNode/ParseNode/RenderNode）：直接调用工具函数，确定性执行，无需 LLM 参与
- **LLM驱动 Node**（SelectNode/TopicNode/WritingNode）：调用 `call_llm()` 让 LLM 分析/创作，通过精心设计的 prompt 引导
- **交互式 Node**（ReviewGateNode）：使用 `input()` 等待用户选择，是流程中唯一的"人工闸门"

### 关键设计决策

#### 1. 搜索由用户启动，程序化执行

SearchNode 直接调用 `search_papers(query)`，query 由用户在入口处输入。这样 DAG 是纯数据流，每个 Node 职责清晰，避免在 DAG 内嵌套 ChatNode+ToolCallNode 的复杂循环。

#### 2. ReviewGateNode — 人工审批

核心交互点。`exec()` 中：
- 展示 LLM 提出的主题列表（编号+标题+角度+理由）
- 等待用户输入：选择编号 / 自定义主题 / 输入 "back" 要求重新提议
- 用户选择 → action `"write"`；用户输入 "back" → action `"back_to_topic"`

#### 3. ParseNode — PDF 文本提取

直接 import `pdf_text_extractor.extract_text_from_pdf`（PyMuPDF），将每篇 PDF 解析为 Markdown 文本。不需要通过 bash subprocess 调用脚本。

#### 4. LaTeX 渲染 — 新增 `latex_render.py` 工具

项目目前无 LaTeX 编译能力，新增 `tools/builtins/latex_render.py`：

- 函数 `render_latex(tex_content, output_dir)`
- 写入 .tex 文件到 output_dir
- 检测系统 LaTeX 编译器（优先 MiKTeX/pdflatex）
- 调用 `pdflatex` 编译（两次，确保引用正确）
- 返回 `{success, pdf_path, tex_path, log, error}`
- 编译失败时 .tex 文件仍保存，用户可手动编译

同时在 `tool_def.py` 注册为 `render_latex` 工具。

#### 5. WritingNode — LaTeX 论文输出

LLM 的 prompt 明确要求输出完整 LaTeX 代码（包含 `\documentclass`、`\begin{document}` 等完整结构），而非 Markdown。WritingNode 将 LLM 返回的 LaTeX 内容传递给 RenderNode。

#### 6. shared 状态

沿用项目模式，使用 `shared` dict 存储全局对象：
- `shared["tool_executor"]` — ToolExecutor 实例
- `shared["guard"]` — SafetyGuard 实例

Node 间数据通过 Flow 的 payload 传递（更清晰、可追溯）。

### 文件变更清单

| 文件 | 操作 | 说明 |
|------|------|------|
| `Agent/ResearchAssistant/__init__.py` | **新建** | 模块空声明 |
| `Agent/ResearchAssistant/main.py` | **新建** | 9个Node定义 + DAG连接 + `run_research_assistant()` 入口 |
| `Agent/ResearchAssistant/prompts.py` | **新建** | 各LLM阶段使用的system prompt + user prompt模板 |
| `tools/builtins/latex_render.py` | **新建** | `render_latex()` 函数 + LaTeX编译器检测 |
| `tools/builtins/tool_def.py` | **修改** | 注册 `render_latex` 工具 |
| `tools/builtins/__init__.py` | **修改** | 导出 `render_latex` |

### 目录结构

```
Agent/ResearchAssistant/
├── __init__.py
├── main.py                  # 9个Node + DAG + run_research_assistant()
└── prompts.py               # SELECT_PROMPT / TOPIC_PROMPT / WRITING_PROMPT

tools/builtins/
├── latex_render.py          # render_latex() 函数
└ (其余不变)
```

### 运行入口

```python
def run_research_assistant():
    """运行研究助手工作流"""
    shared.clear()
    shared["guard"] = SafetyGuard()
    shared["tool_executor"] = ToolExecutor(guard=shared["guard"])
    
    # 构建 DAG
    search = SearchNode()
    select = SelectNode()
    download = DownloadNode()
    parse = ParseNode()
    topic = TopicNode()
    review_gate = ReviewGateNode()
    writing = WritingNode()
    render = RenderNode()
    end = EndNode()
    
    # 连接
    search - "select" >> select
    select - "download" >> download
    download - "parse" >> parse
    parse - "topic" >> topic
    topic - "review" >> review_gate
    review_gate - "write" >> writing
    review_gate - "back_to_topic" >> topic
    writing - "render" >> render
    render - "end" >> end
    render - "render_error" >> end
    search - "error" >> end
    
    print("=" * 60)
    print("📚 研究助手工作流")
    print("=" * 60)
    
    user_query = input("👤 输入研究主题关键词: ").strip()
    if not user_query:
        print("请输入有效的研究主题")
        return
    
    flow = Flow(search)
    flow.run({"query": user_query})
```

### 错误处理

- **SearchNode**: 搜索失败 → action `"error"` → EndNode 报告原因
- **DownloadNode**: 部分下载失败 → 继续处理成功部分，在 payload 中标记失败项
- **ParseNode**: 解析失败 → 跳过该论文，从 payload 中移除
- **RenderNode**: LaTeX 编译失败 → action `"render_error"` → EndNode 保存 .tex 文件并告知用户

### 前置要求

- 系统安装 LaTeX 发行版（Windows 推荐 MiKTeX：`https://miktex.org/`）
- 已安装 PyMuPDF（`pip install pymupdf`，项目已有）
- 已安装 httpx（项目已有）
