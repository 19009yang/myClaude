# LaMemory 任务 benchmark

这个运行器使用 `benchmarks/memory_task` 冻结的 8 题、历史种子、固定追问和评分标准，默认比较无长期记忆（`none`）、历史 Markdown 算法（`legacy`）、完整历史用户原文（`full_history`）与 LaMemory（`lamemory`）。不修改任务资产，也不访问 `core/chat_memory/` 或默认 LaMemory 数据库。

LaMemory 默认使用 **hybrid：关键词 + 真实 embedding 余弦相似度召回 + RRF 融合**。已有三组真实试跑见[历史实验汇总](../../docs/lamemory-benchmark-experiments-20261111.md)；它们不属于新四组协议，不能并入新协议结果。新协议目前只做离线验证，不能将占位分数解释成记忆效果。

## 当前四组协议（2026-11-12）

完整规则见 [LaMemory 任务对照协议 v2](../../docs/lamemory-benchmark-protocol.md)。四组使用相同的任务、工具、模型和资源上限；`legacy` 全量注入的是提取后的 Markdown，`full_history` 才注入全部带日期和来源的种子用户原文，且不调用提取、摘要或 embedding。

默认 `context_policy=shared_strict`：所有组保留完整当前任务会话，不摘要、丢轮次或截短工具结果；超出共同输入预算时明确记录容量失败。LaMemory 的 top-k 与记忆预算仍生效。`--context-policy runtime` 可恢复各后端原有上下文策略，用于产品整体运行能力比较；两种策略的结果不要合并。

建议质量/成本实验使用 [fair-comparison.json](fair-comparison.json)：四组、3 次任务重复、65536 Agent/评分窗口、固定评分推理设置、原请求查询。模型声明的窗口必须足够。新的报告显示计划/未启动样本、执行与评分失败、同题同重复的配对分差和在线费用，并分开列出建库、在线维护及 judge 成本。

```bash
# 仅预估，不请求模型；96 次任务、288 个用户轮次
.venv/bin/python -m benchmarks.memory_benchmark estimate \
  --config benchmarks/memory_benchmark/fair-comparison.json

# 真实运行；先补齐模型、embedding 与匹配的价格配置
.venv/bin/python -m benchmarks.memory_benchmark run \
  --config benchmarks/memory_benchmark/fair-comparison.json \
  --prices /absolute/prices.json --max-cost 10
```

下面的压缩说明仅适用于 runtime 模式；历史测试数量和真实试跑记录保留原协议含义，不作为四组新协议结果。

## 运行

在项目根目录使用现有虚拟环境。默认子命令是 `estimate`，不会请求模型或建立记忆库。

```bash
# 8题 × 4组 × 3次重复；预估文件必须使用新路径
.venv/bin/python -m benchmarks.memory_benchmark estimate --output /tmp/memory-estimate.json

# 如果只比较两套记忆系统
.venv/bin/python -m benchmarks.memory_benchmark estimate --groups legacy lamemory

# 无网络、无模型的完整协议 smoke；4组 × 8题 × 1次重复 × 3轮
.venv/bin/python -m benchmarks.memory_benchmark smoke --repeats 1

# 真实模型单题试跑，校准 token、输出上限与评分质量
# 先按下文补齐 .env 与自己的 prices.json
.venv/bin/python -m benchmarks.memory_benchmark run \
  --tasks 01_parents_birthday --repeats 1 --prices /absolute/prices.json --max-cost 1

# 正式运行；实际支出按当前价格表估算并逐请求检查预算
.venv/bin/python -m benchmarks.memory_benchmark run --prices /absolute/prices.json --max-cost 10
```

`run` 会发送付费模型请求。默认产物在 `.myclaude/benchmarks/<mode>-<timestamp>/`，可以用 `--output /absolute/new/directory` 指定。已存在目录不会覆盖。费用不足、建库失败、隔离失败或中断均保留已有报告与证据，退出码为 2；完整运行退出码为 0。当前没有断点续跑，失败后请使用新目录，避免在不同状态下续接实验。

配置示例为 [config.example.json](config.example.json)。通过 `--config` 加载；`--groups`、`--tasks`、`--repeats`、`--max-cost`、`--retrieval-mode` 可覆盖对应字段。`retrieval_mode` 默认 `hybrid`，可显式使用 `--retrieval-mode keyword` 复现关键词协议。所有组共用窗口、输出上限、工具定义、最大模型调用数、最大工具调用数和固定追问。默认每个用户轮次最多 8 次模型调用、24 次工具调用；不是把一次工具循环当作一个用户轮次。

当前默认 `max_output_tokens=8192`（此前为 4096），为思考与工具参数留出更多输出空间；仍由所有组共用。`response_retries=1` 允许对尚未提交/执行工具的无效响应最多重新请求一次，**重试占用同一轮的 8 次模型调用上限、时间和费用预算**；SDK 重试仍为 0。达到输出上限时明确报 `output_truncated`，不执行截断的工具调用，也不以相同上限盲目重试。

各组共用预算收尾规则：每次调用明确告知剩余次数，倒数第二次提示完成必要写入，最后一次撤下工具、要求基于已有产物给最终文本。工具次数提前耗尽时也进入文本收尾；同批次超出工具预算的调用记录为 `executed=false` 的未执行错误，并补齐对应工具消息，不能额外执行或伪造成功。没有扩大 8 次模型/24 次工具上限。无记忆组对未知事实继续标待确认，不读取长期记忆；共同提示要求不要反复校验已通过结果或靠示例猜个人事实。`completion_reason=budget_closeout` 与汇总的 `budget_closeout_rounds` 标识预算收尾，任务质量仍由实际产物评分决定。忽略收尾要求仍请求工具、返回空文本或超时依然失败。

评分输入只使用可观测正文和工具证据，去掉思考内容、usage、供应商元数据，并将 assistant 消息中的重复工具参数指向同一轨迹的 `tool_start`；没有对应执行事件的调用仍保留参数。产物、rubric、来源证据和独立执行结果完整保留，原始轨迹不改写。默认评分窗口仍与 Agent 相同；确有更大证据包时，可在 JSON 设置 `judge_context_window`，并保证评分模型声明的 `BENCH_JUDGE_LLM_CONTEXT_WINDOW` 足够大（启用独立评分前缀需完整模型配置）。计费器按评分角色使用相同的独立输入预算，超限仍明确失败。

## Hybrid 配置与索引

真实 hybrid 运行除主聊天、提取和评分模型外，需要项目根目录 `.env` 中的独立 embedding 配置；同名进程环境变量优先：

```dotenv
LAMEMORY_EMBEDDING_PROVIDER=openai-compatible
LAMEMORY_EMBEDDING_MODEL=your-embedding-model
LAMEMORY_EMBEDDING_BASE_URL=https://your-embedding-endpoint.example/v1
LAMEMORY_EMBEDDING_DIMENSIONS=1024
LAMEMORY_EMBEDDING_NAMESPACE=memory-benchmark-v1
LAMEMORY_EMBEDDING_API_KEY=your-embedding-key
LAMEMORY_EMBEDDING_SEND_DIMENSIONS=false
LAMEMORY_EMBEDDING_TIMEOUT=10
LAMEMORY_EMBEDDING_MAX_RETRIES=0
```

模型、端点及维度须替换成实际服务参数；需鉴权时必须提供密钥，无鉴权的本地服务可留空。是否发送 dimensions 取决于端点支持情况。缺少 MODEL/BASE_URL/DIMENSIONS/NAMESPACE 或缺少匹配的 embedding 单价时，在付费调用前停止。

评测通过共用 runtime 工厂接入当前 LaMemory。检索模式由 benchmark JSON/CLI 决定，覆盖 `LAMEMORY_RETRIEVAL_MODE`。其余候选数、RRF 权重、相似度阈值、查询超时等 `LAMEMORY_*` 检索参数按 runtime 规则读取，并保存实际配置。**本协议固定关闭 rerank 和关键词降级**，覆盖宿主的 `LAMEMORY_RERANK_ENABLED` / `LAMEMORY_ALLOW_KEYWORD_FALLBACK`，避免将向量失败当成完整 hybrid 结果；如需评估重排应另定义协议及价格。

`retrieval_top_k` 默认 10（此前为 5），仍受共同记忆预算约束。LaMemory 自动上下文检索使用最近三个有检索内容的用户请求的有界组合。benchmark 按公开提示格式剥离日期/隔离/宿主路径协议头，并从六道记忆/偏好题中分离独立的“输出 `文件`，……”交付段；两条公开的 `fixed_no_new_facts` 复查提示不追加到查询，复用原任务主题。其他追问保留，未识别的交付格式保守保留，不用通用删词规则猜测用户意图。完整要求仍提供给执行模型并留档，不修改原始消息或冻结资产。报告与 context 轨迹记录 `retrieval_query_strategy` 版本、实际查询及裁剪标记；没有读取 rubric 或私有答案构造查询。具体召回证据优先于背景 profile 占用预算，profile 只填充剩余空间。

定位语义候选漏召回时，可使用独立的[固定快照查询对照](../../docs/lamemory-query-ablation.md)：冻结已完成运行的同一份记忆和文档向量，对比原请求、原请求加检索指令、事实需求改写三组，保存完整语义排名、cosine、阈值/候选截断原因和融合结果；不重新建库，也不运行任务模型。

主评测默认 `retrieval_query_mode=request`（方案 A）。显式设置 `--query-mode facts` 或配置 `retrieval_query_mode=facts` 可运行方案 C：关键词与语义通道均使用预先固定的事实需求查询，语义查询额外使用与独立对照 B/C 相同的检索指令。这里尚未启用“关键词保留原请求、仅语义使用事实查询”的另一方案。

方案 C 的八条查询保存在 [lamemory-fact-queries.json](lamemory-fact-queries.json)，只根据公开请求人工编写；不使用隐藏事实、评分表或任务运行结果。两道通用编程题仅提取公开技术要求。每条查询绑定公开提示 SHA-256，提示变化时拒绝运行。执行模型仍收到各组相同的完整提示，只有 LaMemory 检索侧改变；两条固定追问复用同一事实查询。运行目录保存 `query_plan.json`，报告与轨迹标记 `manual-public-request-facts-with-instruction-v1`，embedding 空间记录实际查询指令，预估包含指令 token。不要把此实验解释为自动查询改写器的效果。

方案 C 全量一轮配置为 [lamemory-facts-full.json](lamemory-facts-full.json)：8 题 × 3 组 × 1 次重复，每题仍完成初始请求和两条固定复查，共 24 个任务运行、72 个用户轮次，费用预算上限 10 元（使用下述人民币定价）。先预估，再运行：

```bash
.venv/bin/python -m benchmarks.memory_benchmark estimate \
  --config benchmarks/memory_benchmark/lamemory-facts-full.json \
  --prices benchmarks/memory_benchmark/prices.deepseek-flash-siliconflow-qwen3-4b.cny.json

.venv/bin/python -m benchmarks.memory_benchmark run \
  --config benchmarks/memory_benchmark/lamemory-facts-full.json \
  --prices benchmarks/memory_benchmark/prices.deepseek-flash-siliconflow-qwen3-4b.cny.json
```

真实运行仍执行隔离自检与每轮产物验证，使用新的运行目录保留证据。原请求 A 与方案 C 的严格因果比较应复用同一冻结种子快照；本入口的三组全量结果用于检查 C 在整套任务上的可用性，并非固定快照 A/C 配对实验。

预算中断后的补跑可指定 `--seed-from <原运行目录>`（JSON 为 `seed_source`）。它先校验原任务资产、摘要设置和各组完整快照文件哈希，再验证 embedding 空间一致，然后复制原种子快照，不再回放历史或重建索引。新报告记录 `seed_origin`，原始运行与失败结果保留；这是在新目录中重跑选定任务，不是在中断的工具链中原地续写。预估不再计入建库费用。两道编程题的补跑配置见 [lamemory-facts-coding.json](lamemory-facts-coding.json)，评分窗口设为 65536，需要同时声明评分模型支持相应上下文；通过 `judge_reasoning_effort=off` 关闭评分模型的推理模式，降低输出截断风险。Agent 的共同上下文预算仍为 32768，Agent 的推理配置不变。该补跑的评分窗口/推理设置与原运行不同，报告应分别展示，不覆盖或冒充原协议结果。

### 旧系统发送前压缩

以下压缩仅在 `context_policy=runtime` 下生效，`shared_strict` 不执行会话压缩。runtime 中 `legacy_preflight_compression=true` 默认启用。在每次 Agent 请求前，使用与发送检查一致的 UTF-8 估算，计入系统提示、完整长期记忆、会话和工具 schema。超过实际输入预算的 `legacy_compress_trigger_ratio`（默认 0.85）即尝试摘要，不再等上一条模型 usage 达到总窗口的 90%。默认窗口 32768、输出预留 8192、安全余量 512 时，输入预算为 24064，摘要触发点为 20454。

适配器调用冻结旧系统的原 `compress()` 和原摘要提示词，只调整本实例的触发值与合法保留边界。通常保留最近四条消息；优先压缩更早历史，必要时也可摘要当前轮已完成的工具组。当前用户请求原文始终保留，assistant/tool 组不拆开，未收到全部结果的工具调用不会交给摘要模型。长期记忆仍完整注入，提取提示词与追加式更新语义不变，不引入 LaMemory 的关键词/向量召回。

每次构造上下文至多执行 `job_attempts` 次摘要，摘要请求本身必须在输入预算内；模型调用共享本轮期限并计入 memory/summary 用量和共同费用预算。摘要结果非空、工具链有效且整体输入确实缩小时才提交；无效响应、逾期响应或无缩减会回滚当前摘要对会话视图和 MEMORY.md 的更新。无法容纳的长期记忆或当前请求仍明确失败，不截断这些原文来伪装成功。压缩会影响之后的对话和追加记忆，原算法保留过时信息的行为仍可能存在。

原始消息独立保存在 `<session>.raw.jsonl`，在原初始化清理之前复制已有会话，并对新增消息只追加；压缩只重写 `<session>.jsonl` 工作视图。`private/compression/<session>/` 保存每次摘要的前后视图、长期记忆前后副本及提交/回滚记录。context 轨迹包含 `legacy_compression` 诊断，报告标记 `legacy-input-budget-preflight-v1`，便于与历史协议区分。

可通过 `--no-legacy-compression` 或 `legacy_preflight_compression=false` 恢复旧的轮次结束 usage 触发方式。无记忆组和 LaMemory 的上下文规则不因本开关改变，旧报告与冻结源码不改写。预估的 expected 场景按旧系统每轮一次摘要预留，configured_ceiling 按每次 Agent 请求至多 `job_attempts` 次预留；实际次数和费用以 usage.jsonl 为准。

旧系统单独重跑配置见 [lamemory-legacy-compression.json](lamemory-legacy-compression.json)，复用原快照、默认只跑 legacy 的全部 8 题，并保留较大的评分窗口：

```bash
env LLM_CONTEXT_WINDOW=65536 .venv/bin/python -m benchmarks.memory_benchmark run \
  --config benchmarks/memory_benchmark/lamemory-legacy-compression.json \
  --prices benchmarks/memory_benchmark/prices.deepseek-flash-siliconflow-qwen3-4b.cny.json
```

隔离测试覆盖无 usage 触发、当前轮工具组、当前请求保留、只追加原始证据、失败回滚、无缩减回滚、期限与原源码哈希。对之前 7 个旧系统超限视图的临时副本做替身模型回放，全部回到输入预算内且工具链有效，原始文件未变，见[回放证据](../../.myclaude/benchmarks/lamemory-legacy-compression-replay-20261111.json)。该回放只证明预算适配和证据保留，不证明真实摘要质量、事实保留率或新任务分数；真实模型全量尚未重跑。

跨轮证据保留：同一个服务实例/会话只保存实际注入过的原子记忆 ID、首次注入轮次与实体 generation，最多 `top_k` 个引用，最长覆盖最近三个用户轮次。每次构造上下文都重新读取当前可见 active 记录并核对 generation；更正、删除、冲突、范围变化或过期会立即排除旧引用，不缓存旧正文，也不跟随旧 ID 恢复被替换内容。重新校验的旧引用先与本次 fresh top-k 去重合并（候选最多 2×top_k），共同受原记忆预算与输入预算约束；只有实际进入上下文的引用继续保留。来源和 `revalidated_active` 状态进入记忆数据，`retained_evidence` 记录诊断；fresh 检索统计与最终注入数量分开看。缓存不跨新实例/新会话继承，不绕过 hybrid 失败检查。

## 评分一致性

评分模型默认请求 `judge_temperature=0.0`；可在 JSON 设为 `null` 保留模型原配置。显式禁用 temperature 的兼容设置或启用 thinking_budget 时不强制覆盖。低温不能保证模型评分完全确定。

针对生日题中实际发生波动的 `gift-note`，宿主私有口径 `birthday-gift-note-v1` 在 [scoring.py](scoring.py) 明确分档，适用于所有组，不修改冻结 rubric，也不向任务 Agent 暴露：

| 产物中的证据 | 分数 |
|---|---:|
| 提醒备注明确给父亲买颈椎按摩仪仍是待办/未购买，非条件性建议 | 15 |
| 提到该设备计划，但仅放在正文建议、待确认，或没有明确备注/状态 | 10 |
| 只有泛化礼物信息，或无礼物备注（15 分项按原规则扣 10 分） | 5 |
| 与来源相反地明确声称颈椎按摩仪已经购买 | 0 |

宿主识别 Markdown 备注字段/表格和日历 DESCRIPTION，按实际父亲事件上下文取证；条件性计划不因其他日期算术修正而升级为满分。口径绑定冻结评分项的完整 SHA-256，标准变化必须重新审阅。`judge_response.json` 保留模型原分，`result.json` 的 `policy_resolutions` 同时保留原分、裁决分、规则和证据哈希；最终汇总使用裁决分。其他评分项继续由模型独立评分，仍可能有语义判断波动；本口径不是通用语义评测器。离线 smoke 仍使用全零占位分，不执行真实效果裁决。

种子全部回放、分层摘要完成后统一建立向量索引；索引成功才冻结初始快照。每题/重复克隆数据库中的同一向量空间和投影，复用种子向量，不重复建库。每轮提取/摘要更新后，在轮次剩余时间和记忆超时内维护新增/失效投影。索引失败、部分覆盖或查询失败明确记为失败，保留索引任务和检索轨迹；后续轮次跳过。相似度阈值以下的正常空结果不是故障。

`smoke` 同样执行 hybrid 索引、查询和更新流程，但只用离线假向量/假模型，不读真实模型环境、不访问网络，也不证明语义效果。

## 模型与价格

主模型读取项目 `.env` / 进程环境的 `LLM_*`。存在 `MEMORY_LLM_*` 时按项目已有规则独立解析记忆模型，否则共用主模型。存在 `BENCH_JUDGE_LLM_*` 时独立解析评分模型，否则评分共用主模型。独立前缀需要完整的供应商、型号与凭据配置，例如：

```dotenv
BENCH_JUDGE_LLM_PROVIDER=deepseek
BENCH_JUDGE_LLM_MODEL_NAME=deepseek-flash
BENCH_JUDGE_LLM_API_KEY=...
```

三类模型均复用项目 `ChatModel` 适配层。SDK 自动重试关闭，记忆任务最多尝试 3 次，所有实际尝试计费。原有 `LLM_TEMPERATURE`、`LLM_REASONING_EFFORT` 等配置继续生效；模型推理也占输出 token。若提取或评分经常因输出截断失败，应提高输出上限并重新预估，而不是把失败记为无记忆或零分通过。报告中不保存密钥或请求头。

`--prices` 接受 JSON，格式见 [prices.deepseek-flash.json](prices.deepseek-flash.json)。每类角色（`agent` / `memory` / `judge`）分别填写 `provider`、`model` 及 `input`、`output`、`cache_read`、`cache_write` 的**每百万 token**单价，币种由 `currency` 指定。实际运行会核对定价的供应商/型号与模型配置。网关、自定义端点或合约价应使用自己的价格表；仅有相同模型名称不能证明网关收费相同。

Hybrid 还要求一个 `embedding` 条目，填写 `provider`、`model` 和每百万输入 token 单价 `input`，所有角色必须使用同一币种。可复制 [prices.hybrid.example.json](prices.hybrid.example.json)，将 embedding 模型占位符和 `input: null` 替换为实际值，并核实聊天单价。模板故意不能直接运行，不自动猜测 embedding 价格。建库文档、在线新增文档、查询向量各自记录 usage、失败尝试和费用；每个请求发送前检查共同 `--max-cost` 预算。失败但有供应商 usage 的 embedding 仍按实际 usage 计量；缺 usage 时按输入粗估计费。

官方 DeepSeek Flash 聊天 + SiliconFlow `Qwen/Qwen3-Embedding-4B` 可使用 [人民币价格表](prices.deepseek-flash-siliconflow-qwen3-4b.cny.json)：聊天采用 2026-11-11 查阅的官方高峰价，embedding 按用户提供的 **0.14 元/百万输入 token**，后者并非独立核验的公开价格。指定 `--prices benchmarks/memory_benchmark/prices.deepseek-flash-siliconflow-qwen3-4b.cny.json` 后，`--max-cost` 的单位为人民币，不自动切换空闲时段费率。

默认价格表仅包含聊天角色。默认 hybrid 的 `estimate` 在未补齐 embedding 单价时返回 `unpriced_roles=["embedding"]`、总 `cost=null`，`priced_cost` 只表示已定价部分，不能作为完整预算；`run` 会拒绝该价格表。提供自定义价格表后再预估完整费用：

```bash
.venv/bin/python -m benchmarks.memory_benchmark estimate --prices /absolute/prices.json
```

默认表是 2026-11-10 查阅 [DeepSeek 官方价格](https://api-docs.deepseek.com/quick_start/pricing/) 的 Flash **高峰价**快照：未命中输入 $0.30/M、输出 $1.20/M、命中输入 $0.006/M。官方非高峰费率为其一半。脚本不自动按时段切价；全程按选定价格快照核算，报告保留价格来源与配置。

预估分为 `low`、`expected`、`configured_ceiling`。常规假设是每轮 3 次模型调用、每次输出约 900 token、每轮交付物约 2400 token，另计前轮历史、工具参数和结果的重复输入。建库按 44 条用户消息、21 个会话计算；每个有记忆后端只建一次库，所有题与重复共享该初始快照。

默认 **8题 × 3组 × 3次 = 72次任务运行、216个用户轮次、216次独立评分**。下表是原关键词协议的聊天费用历史预估，**不含 embedding，不是当前 hybrid 总价**：

| 情景 | 输入 token | 输出 token | 费用（USD） |
|---|---:|---:|---:|
| 较轻输出/调用假设 | 5,439,385 | 728,920 | 2.51 |
| 常规假设 | 8,908,264 | 923,320 | 3.78 |
| 配置上限情景 | 63,832,224 | 12,005,376 | 33.56 |

仅 `legacy lamemory` 两组、各重复 3 次时为 **48 次任务运行**；常规假设约 **6,970,960 输入 + 642,520 输出 token，$2.86**。

这里的 token 是带消息/工具包装的 UTF-8 字节数除以 3 的粗估，不是供应商 tokenizer。全部预估输入按缓存未命中计价；真实运行优先记录供应商 usage，并利用适配层提供的缓存读写量核算。调用失败或没有 usage 时，以请求输入粗估及最大输出预留费用，标记为估算，不冒充零成本。价格表费用不包含容器/机器成本。

`configured_ceiling` 把 Agent 调用、输出、记忆重试、分层刷新及旧算法可能触发的压缩计入。摘要源长度、推理长度和真实分词取决于模型，所以它是高用量情景，不能当作供应商账单的绝对上限。`--max-cost` 在发送每个请求前检查“累计核算费用 + 本次预留”；若配置币种为 USD，则数值单位为美元。它同样依赖 token 粗估和静态单价。

建库、任务在线、评分分阶段计量；Agent、提取、摘要、embedding 文档和查询分开记录。固定追问直接使用资产包文本，不调用用户模拟模型；rerank 关闭。embedding 实体数量由实际提取和摘要决定；预估按显式实体数量、输入长度及批次假设计算，详情见 estimate.json 的 assumptions，不能当作账单上限。

## 对照协议

1. 启动时运行原任务包 `validate.py`，检查全部冻结 SHA-256、历史依据和参考差异结果。不会重新冻结任务资产。
2. `legacy` 与 `lamemory` 按相同数组顺序回放全部种子。每条原始用户消息后追加固定确认“已收到。”，按后端正常流程提交并等待提取成功。跨会话只重置会话历史，长期记忆继续共享。保存本地消息 ID → 运行时来源 ID、原始日期/种类和正文 SHA-256 映射。LaMemory 默认在建库完成后显式启用用户概览（M2）；M3 自动经验学习关闭。
3. 每个题目、每次重复、每组从对应初始快照克隆独立存储，并创建新会话、空交付目录。`none` 不导入种子，保留本题对话历史，完全不执行长期记忆提取或注入。冻结种子不会被任务中的新写入改动。
4. 各组复用 `Agent.chatBot_with_memory.runner.ChatRunner` 和项目工具执行器，仅提供共同的 `read/write/ls/python` 工具。各组均关闭主动记忆工具与静态 Skill，因此这里测量的是**自动提取和上下文注入**，尚未评估主动搜索/更新工具的收益。LaMemory 使用当前 hybrid 召回和记忆预算；用户概览作为背景注入，不参与向量排名。旧算法全量注入 Markdown；完整历史组全量注入种子用户原文。当前会话默认 shared_strict；只有显式 runtime 模式使用各自摘要/裁剪机制。
5. 固定执行初始请求和两条追问，每轮保存完整工具轨迹、回复、产物快照与哈希。评分不会作为下一轮反馈；任务 Agent 看不到 rubric、私有证据和参考答案。按任务/重复轮换组顺序。若工具链因错误未完成，后续轮次标记为跳过，不拼接非法历史或把旧产物当成新一轮成功。
6. 每轮由独立、无任务工具的评分调用逐项盲评。只提供当轮产物、正文/工具轨迹、该题 rubric、相关种子证据和执行结果；移除上下文诊断并把含组名的宿主交付路径归一化。宿主校验项 ID、分值范围、非有限数字和重复/缺失项，再自行求总分、维度分和阈值。
7. 配置题在隔离环境重跑 `python diff_configs.py current.json proposed.json`，分别核对提交版/重跑版报告与 16 项参考变更。代码评审题实际执行提交的 unittest，另在只含原始提交修复模块的工作区执行独立契约检查，核对原文件字节不变。并发压力测试仅是执行证据，评审器仍需检查是否正确加锁。

某些评分文字自身有局部分值歧义（如修复项描述的每类分值相加大于该项 `max_score`），评分器明确以 JSON 的 `max_score` 为上限，记录折算理由；任务包原文件保持不变。

首轮/最终均分只计入执行与评分均成功的轮次，有效评分数量单独记录。运行失败仍计入通过率分母；失败轮次如有产物评分会标记 `artifact_only=true`，不进入均分或维度汇总。报告包含逐题 `memory/mixed/general` 得分与满分、每次运行最早达标轮次、首轮/最终分数、错误和耗时。`mixed` 不是纯记忆召回准确率，本题集也不能证明程序经验迁移有效。

## 隔离执行

默认 `sandbox=seatbelt` 适用于 macOS。宿主文件工具只接受当前交付目录内的路径，拒绝越界与指向外部的符号链接；生成代码复制到临时目录后，通过 `sandbox-exec` 限制为该题文件、Python 运行时和必要系统只读路径，禁止网络和私有文件访问。生成代码使用清理后的环境，不继承 API key。限制单次代码执行时间、CPU、文件大小与文件描述符数；产物还受文件数、总大小及文本编码限制。

Linux 或希望使用容器时，在配置副本中设置：

```json
{"sandbox": "docker", "docker_image": "python:3.13-slim"}
```

需自行安装并启动 Docker，提前准备对应本地镜像。运行器使用 `--pull=never`、无网络、只读根文件系统、低权限用户、CPU/内存/进程数限制，只挂载本次公开文件副本。Docker 路径已实现，尚未在当前 macOS 验证环境实测。运行前的隔离自检必须通过，否则在任何付费模型调用之前停止；没有仅切换 cwd 的降级模式。在禁止嵌套沙箱的环境中，请从允许启动 Seatbelt/Docker 的终端运行。

## 输出与验证

```text
<run>/
  estimate.json                 # 此配置的事前预估
  report.json / report.md       # 配置/代码/资产哈希、组间汇总、错误、费用
  usage.jsonl                   # 每次调用的阶段、角色、模型、原 usage、缓存、成本与用量来源
  provider_errors/              # 解析失败的响应字段证据（脱敏），usage.jsonl 记录原因、finish_reason、路径
  memory_responses/             # 记忆模型请求/响应正文及 usage（脱敏）；在提取解析/来源校验前保存
  seeds/<group>/                # 冻结存储、来源映射、任务/重试记录
    maintenance_jobs.json       # LaMemory 最终摘要/向量索引状态与任务（hybrid）
  runs/<task>/repeat-N/<group>/
    private/                    # 本题自己的记忆快照，模型文件工具不可见
    work/                       # 本题公开资产与产物
    round-N/
      prompt.md / trace.jsonl / response.json
      artifacts/                # 此轮产物快照
      memory_jobs.json          # 本轮记忆处理、向量覆盖率与索引失败/重试记录
      judge_response.json       # 原始评分输出（无效 JSON 也保留）
      judge_input.json          # 实际盲评请求与输入预算，发送前保存（超限也保留）
      execution.json / result.json
```

旧算法原始文件冻结于 `72fa6ee96492f92ea60887cef6d3681bc549bbd8:core/memory.py`，保存在 [frozen/legacy.py](frozen/legacy.py)，SHA-256 为 `d6b346be5c8323b0bc73167ff5a93f8322190c5acb1865998dfb7d19773b4a1a`。适配器不改该文件，在实例级注入独立路径和模型，绕过旧的 usage 门控以便正常提交合成确认轮次，暴露原来吞掉的无效 JSON，并把压缩输出的记忆数组转换成 Markdown，修复原来的数组拼接错误。runtime 模式增加预算发送前触发与工具链安全边界，原摘要提示词及原 compress() 仍复用；原始会话独立归档。旧算法的提取提示词、追加式去重/更正语义及全量注入方式保持原样；它可能保留过时信息，这是本对照需要观察的行为。

```bash
.venv/bin/python -m unittest discover -s tests/benchmarks -p 'test_*.py' -v
.venv/bin/python -m unittest discover -s tests/lamemory -p 'test_*.py'
```

2026-11-10：benchmark 回归 15 项、既有 LaMemory 回归 69 项、供应商回归 20 项全部通过。覆盖无模型的 24 次任务/72 轮完整流程、快照隔离、来源映射、盲评输入、费用与缓存计量、预算阻断、错误状态，以及 macOS 中的私有文件/网络阻断和实际 unittest 执行。假模型的所有任务评分固定为 0，真实模型调用数和实际费用均为 0。若宿主禁止嵌套沙箱，隔离项会明确跳过，不能当作隔离验证通过。

2026-11-11：接入 hybrid 后，benchmark 回归 **23 项**与 LaMemory 回归 **108 项**全部通过。新增检查覆盖种子向量克隆复用、无关键词命中的向量通道、查询向量缓存、在线索引刷新、建库失败不冻结快照、查询失败不降级、embedding usage/缺 usage/失败计费和发送前预算拦截，以及替换 SDK 的真实运行分支配置读取。最初在宿主沙箱内，本地 HTTP 测试被端口权限阻断、Seatbelt 项跳过；经授权在沙箱外重跑后上述两组均无失败或跳过。全部使用临时目录/数据库与假模型/假向量，**未执行真实付费模型或真实 embedding 效果评测**；历史 results 文件保留原协议证据，不改写成 hybrid 结果。

2026-11-11 试跑故障修正：benchmark **30 项**、LaMemory **110 项**、供应商 **20 项**最终全部通过，无跳过；新增用例覆盖任务主题保留、背景概览预算让位、重复参数精简、不完整调用参数保留、独立评分预算、无效响应受限恢复且工具只执行一次、输出截断诊断、失败响应实际计费与脱敏，以及失败轮次不进入均分。使用真实试跑 `none/round-2` 的既有轨迹离线回放，评分输入粗估从 **35,681** 降为 **23,691**，低于当前默认输入预算 **24,064**；未截断产物或标准，未改写原始轨迹。见 [验证记录](results/trial-fixes-20261111.json)。本次修正未请求真实模型；旧运行没有保存无效响应的原文，不能追溯具体格式错误，也不能将离线回归解释成真实召回效果提升。

2026-11-11 收尾/证据/评分完善：benchmark **38 项**、LaMemory **118 项**、供应商 **20 项**全部通过，无跳过。覆盖无记忆组三轮各 8 次调用内收尾且不读长期记忆、拒绝超预算工具且保持合法链、忽略收尾仍调用工具时明确失败；跨轮引用保留与更正/删除/跨连接删除/generation/项目范围/会话隔离/过期/预算失效；私有礼物评分口径及原分审计。对 `run-20261111-130241-066797` 的既有产物离线回放，gift-note 原模型分 **12/10/15** 按同一口径裁决为 **10/10/10**，第二、三轮礼物证据哈希相同；这不是新一轮模型效果提升，旧报告未改写。见 [独立验证记录](results/closeout-evidence-scoring-20261111.json)。本次未请求付费模型，其他评分项仍可能存在模型判断波动。
