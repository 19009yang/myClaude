# Plan：将 Skill 系统与 Agent 运行流程打通

## 目标

让 `SkillRegistry` 中加载的 SKILL.md 操作指南能够被 Agent（LLM）感知和使用，形成完整的"Skill 发现 → 激活 → 执行"闭环。

## 设计方案：System Prompt 注入 + activate_skill Tool（混合方案）

### 核心思路

1. **Skill 摘要注入 System Prompt**：将 `SkillRegistry.list_skills()` 的摘要（name + description）写进 `SYSTEM_PROMPT`，让 LLM 知道有哪些 Skill 可用
2. **注册 `activate_skill` 为内置 Tool**：LLM 通过 function calling 调用 `activate_skill(name)`，获取具体 Skill 的完整操作指南
3. **激活后 Skill 正文通过 tool_result 传递**：`activate_skill` 的返回值（Skill 正文）作为 tool result 加入对话上下文，LLM 按照指南继续调用内置工具执行操作

### 执行流程

```
用户输入 → LLM 看到 SYSTEM_PROMPT（含 Skill 摘要列表）
         → LLM 判断需要某个 Skill → tool_call("activate_skill", {name: "xxx"})
         → activate_skill 返回 Skill 正文（作为 tool_result）
         → LLM 按照 Skill 指南，继续调用内置工具（bash/read/write等）执行操作
```

---

## 修改清单（6 个文件）

### 1. `tools/skill_loader.py` — 增强 SkillRegistry

**改动**：
- 新增 `get_skill_full(name)` 方法：返回包含 metadata 和 body 的完整信息 dict
- 新增 `search_skills(keyword)` 方法：根据关键词在 name/description 中搜索匹配的 Skill
- 新增 `skill_summaries_text()` 方法：返回格式化的 Skill 摘要文本，方便直接拼入 system prompt

### 2. `tools/builtins/tool_def.py` — 注册 activate_skill Tool

**改动**：
- 新增 `activate_skill(name)` 函数：调用 `SkillRegistry.get_skill_full(name)` 返回 Skill 正文
- 在 `get_builtin_tools()` 的返回列表中追加 `activate_skill` Tool 定义

### 3. `tools/builtins/__init__.py` — 导出 activate_skill

**改动**：
- 在 `__all__` 中加入 `"activate_skill"`

### 4. `tools/__init__.py` — 导出 SkillRegistry

**改动**：
- import 并导出 `SkillRegistry`、`get_default_registry`

### 5. `Agent/chatBot_with_memory/main.py` — 改造 SYSTEM_PROMPT

**改动**：
- `SYSTEM_PROMPT` 中追加 Skill 摘要段落（从 `SkillRegistry.skill_summaries_text()` 动态获取）
- 新增 Skill 使用说明段落，告知 LLM 如何使用 activate_skill 工具

### 6. `Agent/chatBot_with_tool/main.py` — 同步改造

**改动**：同上，在 SYSTEM_PROMPT 中追加 Skill 摘要段落。

---

## 不改动的部分

- `core/llm.py` — 无需改动
- `core/memory.py` — 无需改动
- `core/node.py` — 无需改动
- `tools/executor.py` — 无需改动（activate_skill 作为标准 Tool 自动被 ToolExecutor 处理）
- `tools/guard.py` — 无需改动（activate_skill 不涉及安全风险操作）
- SKILL.md 文件 — 无需改动

---

## 验证方式

修改完成后，运行 `python Agent/chatBot_with_memory/main.py`，测试：

1. 输入 "你好" → LLM 正常回复，不触发 activate_skill
2. 输入 "提取这个 PDF 的文字" → LLM 调用 `activate_skill("pdf-image-text-extractor")` → 返回 Skill 操作指南 → LLM 按指南调用 `bash` 执行脚本
3. 输入 "有哪些 Skill 可用" → LLM 根据 system prompt 中的摘要列表直接回答

---

## 风险与边界

- **Token 消耗**：Skill 正文较长时会增加 token 开销，可后续增加截断/压缩机制
- **意图识别**：当前依赖 LLM 自主判断是否需要激活 Skill，对小模型可能不够可靠
- **依赖安装**：Skill 的 `dependency` 字段目前仅作为声明，不会自动安装
