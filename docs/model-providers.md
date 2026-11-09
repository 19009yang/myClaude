# 模型提供商适配层

当前提供同步文本与工具调用适配，统一入口为 `core.providers.ChatModel`。现有 `core.llm.call_llm()` 和 `call_llm_chat()` 转发到同一层，继续返回原有的 assistant 字典。Agent、LaMemory 提取和工作摘要可以使用不同的模型实例。

## 已实现的供应商

| provider | 协议 | 默认 base URL | 专属 API key 环境变量 | 主要差异 |
|---|---|---|---|---|
| `openai` | Chat Completions | `https://api.openai.com/v1` | `OPENAI_API_KEY` | 默认输出上限字段为 `max_completion_tokens` |
| `anthropic` | 原生 Messages | `https://api.anthropic.com` | `ANTHROPIC_API_KEY` | system、tool_use/tool_result、思考签名块和缓存用量转换 |
| `deepseek` | Chat Completions | `https://api.deepseek.com` | `DEEPSEEK_API_KEY` | 相同来源的工具历史回传 reasoning_content；显式推理开关 |
| `qwen` | Chat Completions | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `DASHSCOPE_API_KEY`、其次 `QWEN_API_KEY` | 输出字段 max_tokens，显式 enable_thinking 开关 |
| `openai-compatible` | Chat Completions | `https://api.openai.com/v1` | 使用通用 `LLM_API_KEY` | 保留旧版 max_tokens 行为，自定义端点可接代理或本地服务 |

`dashscope` 是 `qwen` 的别名，`custom` 是 `openai-compatible` 的别名。默认 provider 为 `openai-compatible`，保留旧配置行为。注册表不维护价格、推荐模型、模型名称或上下文窗口目录；模型 ID 和能力需与实际部署一致。

这些适配已通过本地 mock 协议测试；DeepSeek `deepseek-flash` 另外通过了真实文本、工具调用回传和记忆流程测试，结果见 [真实测试记录](../benchmarks/providers/README.md)。其他供应商仍只有离线协议验证。实现期间官方文档工具连接失败，OpenAI 字段通过本地 SDK 类型与真实 SDK 的 MockTransport 编码验证，其他转换通过协议合同测试验证。模型版本之间的思考、温度和工具能力差异仍需在目标服务上运行 smoke 确认。

## 环境配置与启动

项目根目录提供 [.env.example](../.env.example)。现有三个变量 `LLM_API_KEY`、`LLM_BASE_URL`、`LLM_MODEL_NAME` 继续有效，新增 `LLM_PROVIDER` 选择供应商。

```dotenv
LLM_PROVIDER=anthropic
LLM_MODEL_NAME=your-claude-model-id
ANTHROPIC_API_KEY=your-key
```

```bash
python -m Agent.chatBot_with_memory.main
python -m Agent.chatBot_with_memory.main --provider qwen --model your-qwen-model-id
```

CLI 也支持 `--base-url`。切换供应商时需检查是否还配置了旧的 `LLM_BASE_URL`，显式 URL 会覆盖默认端点；密钥必须对应新供应商。密钥优先级是显式 ModelConfig → 通用 `LLM_API_KEY` → 所选供应商的专属变量，不会读取其他供应商的 key。`openai-compatible` 不自动读取 `OPENAI_API_KEY`，以避免给自定义网关误用专属凭据。

| 环境变量 | 默认值 | 用途 |
|---|---|---|
| `LLM_PROVIDER` | `openai-compatible` | 供应商注册名 |
| `LLM_MODEL_NAME` | 必填 | 实际模型 ID |
| `LLM_API_KEY` | 无 | 显式通用 API key |
| `LLM_BASE_URL` | 供应商默认值 | 自定义 API 根地址，不含用户名、密码、query 或 fragment |
| `LLM_TIMEOUT` | 30 | 单次调用的请求/重试时间预算，秒 |
| `LLM_MAX_RETRIES` | 0 | 有限重试次数，不包含首次；上限 10 |
| `LLM_MAX_TOKENS` | 4096 | 输出 token 上限 |
| `LLM_CONTEXT_WINDOW` | 未指定 | 聊天 CLI 未提供窗口时使用该值，再回退到 32768 |
| `LLM_TEMPERATURE` | 不发送 | 模型支持时设置温度 |
| `LLM_REASONING_EFFORT` | 不发送 | OpenAI/DeepSeek 的推理参数，或 Qwen 的思考开关 |
| `LLM_THINKING_BUDGET` | 不发送 | Anthropic 传统 extended thinking 预算 |

`--context-window`、`--reserved-output` 优先于模型环境配置。输出上限作为 LaMemory 输入预算的输出预留。SDK 级重试关闭，由适配层统一计次；401/403 不重试，超时、连接错误、429 和部分服务器错误可按配置重试。HTTP timeout 是客户端各网络阶段的超时，配合总预算限制后续重试，不提供进程级强制中断。

## 实例配置和复用

```python
from core.providers import ChatModel, ModelConfig

config = ModelConfig(
    provider="deepseek",
    model="your-model-id",
    api_key="read-from-your-secret-store",
    timeout=20,
    max_tokens=4096,
)
with ChatModel(config) as model:
    response = model.complete([
        {"role": "user", "content": "你好"},
    ])
    print(response["content"], response["usage"])
```

`ChatModel` 也可以直接作为 callable 注入 `ChatRunner(chat=model)` 或 `ChatMemoryProvider(chat=model)`。宿主管理共享实例的 close 时机；适配层只关闭自己创建的 SDK/HTTP client，不关闭借用的 client。兼容函数每次构造并释放自己的实例；高频调用建议复用 ChatModel。

也可以继续使用原接口：

```python
from core import call_llm

response = call_llm(
    messages=[{"role": "user", "content": "你好"}],
    config=config,
)
```

`call_llm(provider=model)` 可使用已创建的 ChatModel。`client=` 仍支持注入 SDK client，Anthropic 则接受 httpx client；显式配置中的 key、URL 和 headers 优先应用。兼容模式未指定 URL/key 时保留借用 OpenAI client 的端点和凭据。`provider=ChatModel` 不能与另一套 config/client/registry 混用，防止配置来源不明确。

## 主模型与记忆模型分开

```dotenv
LLM_PROVIDER=anthropic
LLM_MODEL_NAME=your-main-model-id
ANTHROPIC_API_KEY=your-main-key

MEMORY_LLM_PROVIDER=deepseek
MEMORY_LLM_MODEL_NAME=your-memory-model-id
DEEPSEEK_API_KEY=your-memory-key
```

没有任何 `MEMORY_LLM_*` 变量时，记忆提取和摘要共用主模型配置。启用该前缀后独立解析同名后缀，包括 API_KEY、BASE_URL、TIMEOUT、MAX_RETRIES、MAX_TOKENS 等；需要提供完整模型选择，不继承主模型名称或通用 key，避免跨供应商串配置。记忆任务每次请求显式传入剩余 timeout 和 `max_retries=0`，由持久化 job 管理重试。

程序中可以传入独立配置，避免依赖进程环境：

```python
from core.lamemory.providers import ChatMemoryProvider

memory_provider = ChatMemoryProvider(model_config=config)
```

`chat=` 与 `model_config=` 二选一。usage_events 分别记录 extraction/summary 用量及供应商、协议、模型标识，不记录密钥。

## 模型差异和扩展

供应商默认参数不代表该供应商所有模型都支持同一能力。实例可以覆盖兼容选项：

```python
from core.providers import Compatibility, ModelConfig

older_model = ModelConfig(
    provider="openai",
    model="your-model-id",
    api_key="your-key",
    compatibility=Compatibility(
        max_tokens_field="max_tokens",
        supports_temperature=False,
        supports_tools=True,
    ),
)
```

其他兼容项包括 `system_role`、`reasoning_format`、`replay_reasoning`。不支持的显式请求返回配置错误，不静默丢弃。Qwen 的 reasoning_effort 只转换为 enable_thinking 的开关，不承诺映射具体思考强度。DeepSeek 的显式开关使用 thinking/type 与 reasoning_effort；旧模型不支持时，应保持默认不发送，或将 reasoning_format 设为 none。Anthropic 只映射显式 thinking_budget（至少 1024 且小于输出上限），不自动推断或开启 adaptive thinking；仅在模型支持传统模式时使用。

注册自定义兼容服务无需改 Agent：

```python
from core.providers import ChatModel, ModelConfig, ProviderSpec, create_default_registry
from core.providers.openai_chat import OpenAIChatProvider

registry = create_default_registry()
registry.register(
    ProviderSpec(id="internal", api="openai-chat", base_url="https://gateway.example/v1",
                 api_key_envs=("INTERNAL_MODEL_API_KEY",)),
    OpenAIChatProvider,
)
config = ModelConfig.from_env(provider="internal", model="your-model-id", registry=registry)
model = ChatModel(config, registry=registry)
model.close()
```

新增协议实现 `ChatProvider.complete(messages, tools, options)` 与 close，再通过 factory 注册。注册表按实例隔离，不存在全局可变供应商列表。ModelConfig、Compatibility 与 RequestOptions 分别承载配置、能力差异和请求选项，不向业务模块暴露网络协议。

## 历史、错误和验证

返回对象保留 `role/content/tool_calls/usage/reasoning_content`，另带 finish_reason 和 provider_data。工具仍使用 OpenAI 风格定义和调用结构，因此执行器无需随协议变化。适配层将 Anthropic tool_use 转为该结构，并将工具结果转换回 tool_result；多个并行结果放在同一 user 消息中。

原始历史不会被协议转换改写。工具 ID 必须在调用与结果间一致，发送时对不合法 ID 做确定性映射；未知结果、缺失结果、重复 ID、非法参数和不支持的非文本输入会在网络请求前报错。返回的非法或截断工具调用不会交给执行器。推理文本和原生签名/隐藏思考块在原文及工作上下文中保留，仅向相同供应商、模型、协议和端点重放必要字段，切换来源时过滤，且不会作为用户事实提取。

ProviderConfigError 表示配置或输入不兼容；ProviderError 提供 provider、kind、status_code 和 retryable。kind 包括 authentication、rate_limit、timeout、connection、http、invalid_response。错误消息不包含服务端原文、请求内容或密钥。usage 统一为 prompt_tokens/completion_tokens/total_tokens，并在可获得时保留缓存读写；缓存用量口径按协议转换，不估算价格。

```bash
python -m unittest discover -s tests/providers -p 'test_*.py'
python -m unittest discover -s tests/lamemory -p 'test_*.py'
python -m benchmarks.lamemory.smoke --output benchmarks/lamemory/results/fake-smoke.json
python -m benchmarks.lamemory.smoke --real --output benchmarks/lamemory/results/real-smoke.json
```

当前 provider 测试 20 项、记忆回归 34 项通过。联合测试使用 Anthropic 主模型和 DeepSeek 提取模型的本地 MockTransport，验证了主动保存 → 工具结果回传 → 自动提取去重 → 新会话召回与来源追溯。DeepSeek `deepseek-flash` 的真实 smoke 也已通过；缺少可用配置时 smoke 仍以退出码 2 和 blocked 状态报告。

尚未实现：流式/异步事件、原生 Gemini、OpenAI Responses、OAuth、多模态、embedding、reranker、自动模型目录和价格目录。关键词检索不会因增加聊天供应商而自动变成语义检索。
