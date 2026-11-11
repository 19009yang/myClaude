# LaMemory 混合召回实施与验证

日期：2026-11-10。基线 HEAD 为 `7d29ffd811977c7c24d7831a3264e42c94dbf9bd`，未提交或推送。本次在原有 README 修改与未跟踪方案文件之上增量实施，保留方案及历史评测证据。原 [构建方案](lamemory-hybrid-retrieval-plan.md) 保留为实施前设计；本页描述当前实现。

## 实现范围

默认仍为 `keyword`。显式配置 `hybrid` 后，当前可见的 active item、有效 topic、active experience 分别进行关键词与真实 embedding cosine 召回，再按种类执行加权 RRF、以 `(entity_kind, entity_id)` 去重；不同 ID 的相同内容与不同日期事件不会被检索层合并。profile 不索引、不重排，作为背景填充摘要剩余名额。关键词分词和分数没有改为 BM25。

`retrieve()` 返回 items/summaries/experiences/diagnostics；`search()` 等包装保留旧返回类型。`build_context()` 和 `lamemory_search` 使用一次 bundle，共用一次查询 embedding。通道宽度及重排前缀至少覆盖本次 top_k，最多 100。空白 hybrid 查询、top_k=0 或无有效向量不调用模型；关键词模式保留原有空查询概览行为。历史状态查询仅走关键词。排名元数据包含 keyword/semantic rank 与原分数、rrf_score、rerank_score、score_kind；分数不是可信度，不能跨种类、算法比较。

2026-11-11 试跑修正：自动 `build_context()` 将最近三个用户请求组合为有界查询，避免泛化追问丢失任务主题；显式 `retrieve(query)` / 主动搜索工具保持调用方给定的查询。可信宿主可传入 `retrieval_query` 覆盖派生查询，原始对话保留不变。上下文先给具体召回项分配预算，背景 profile 最后填充；总输入超限时也先移除 profile。实际查询与截断标记进入 context diagnostics，不注入历史记忆正文。真实效果改善仍需后续对照验证。

同日 benchmark 查询隔离补充：评测适配器按公开段落格式从六道记忆/偏好题提取用户请求，交付文件名及独立交付要求不参与检索，两条固定无新事实复查提示复用任务查询。其他自由文本不做通用删词；普通聊天的默认查询策略未改变。原始消息与执行模型接收的完整要求保留，协议与轨迹标记策略 v2。该调整只保证查询边界，不能据此断言真实语义召回或最终交付分数提升。

同日新增会话内 `integration/evidence.py`：仅保留实际上下文中的原子记忆引用，最多 top_k 个、最近三个用户轮次。每次从当前权威可见 active 投影重建正文、核对实体 generation；更正/删除/冲突/越权/过期立即丢弃，跨连接删除也不能恢复旧内容。先与 fresh 结果去重，再按共同记忆预算渲染，超出预算未注入的引用不保留。fresh top-k 可与最多 top_k 条旧引用合并，retained_evidence 和 fresh 检索诊断分别记录；显式搜索排名不受这个上下文引用层影响。该缓存限当前服务实例和会话，不是新的事实存储，也不绕过向量失败与索引完整性检查。重新校验有效的计划/待办须保留原意，不能因后续排名变化自动变成“没有依据”，更不能据此声称已经完成。

文件路径相对于 `core/lamemory/`，另注明的仓库路径除外。完整目录见 [模块导航](../core/lamemory/README.md)。

| 文件 | 核心职责 |
|---|---|
| `config.py`、`models.py`、`retrieval/types.py`、`retrieval/fusion.py` | 算法约束、兼容返回、cosine 验证、稳定 RRF |
| `retrieval/sources.py`、`retrieval/engine.py` | 权限/状态/来源检查、三类融合、实际模式与降级诊断 |
| `embedding/space.py`、`embedding/provider.py` | 独立模型配置、空间指纹、SDK 适配、响应顺序与向量校验 |
| `storage/migrations/004_hybrid_retrieval.sql`、`embedding/index.py` | 事务内失效账本、独立持久化任务、回填、CAS 发布、repair、retry |
| `retrieval/reranking.py`、`integration/runtime.py` | 可选 LLM JSON 重排、独立配置、客户端所有权、宿主工厂 |
| `service.py`、`generation/hierarchy.py`、`integration/context.py`、`integration/tools.py`、`core/memory.py` | 兼容接口、有效主题、预算渲染、主动工具与门面 |
| 终端、Web、维护 CLI | 相同工厂与开关、回复后索引维护、检索与覆盖率展示 |
| `benchmarks/lamemory/hybrid_*.py`、新测试及 fixture | 临时合成数据验收、真实 smoke 与可重复检索评测 |

SDK 适配器使用现有 openai 依赖，调用 `embeddings.create` 并按响应 index 还原顺序；仅在显式允许时发送 dimensions。接口依据 [官方 embeddings 参考](https://developers.openai.com/api/reference/resources/embeddings/methods/create)，兼容服务仍须自行验证。请求重试为 0，传入实际剩余超时；数量、索引、维度、bool、NaN/Inf、零向量错误使整批失败。客户端由工厂创建则服务关闭；外部注入则调用方关闭。用量分别记录 embedding_documents、embedding_query 和 rerank，不混入主聊天或提取成本。

## 一致性与索引

新增六张 `lamemory_` 投影/控制表。item/summary/experience 的 INSERT/UPDATE/DELETE 在业务事务中更新对象 generation、范围快照及删除 tombstone；关系变化增加用户级 retrieval generation，回滚也回滚这些变更。经验反馈无需依赖 revision 增长。投影发布、删除和 repair 也使用户代次变化。v1/v2/v3 数据库升级保留原证据和旧生成任务。

索引使用显式 little-endian float32 BLOB。读取只接受同一 space_id、实体 generation、实际输入 SHA-256 与预期维度一致的投影；损坏向量跳过并计数，可通过排队修复。模型/维度/端点指纹/namespace/查询及文档前缀/模板/截断/归一化变更会隔离空间，密钥轮换不会重建空间。文本模板包含经验结果依据，关键词经验文本维持原行为。前缀加上后按字符确定性截断，报告 truncated_documents；字符限制不等于模型 token 限制，不做随机补位或静默二次截断。

索引任务唯一键为对象+空间+代次。协调器只访问当前 user/session/project 范围；删除或失效对象清理投影，旧 pending/failed 代次变 obsolete；failed 不自动无限重排，需显式 retry。相同实体、空间、实际输入未变时允许复用向量并更新代次。模型在写事务外调用；短事务获取写锁后复核权威状态、来源签名、generation 和 hash，再发布。整批响应非法不发布；响应合法但部分对象过期时只跳过过期对象。数据库失败回滚整次发布。

查询不负责建文档索引。关键词结果仍可覆盖缺失投影的记录。部分覆盖如实报告 missing/partial_index；当前空间全无有效投影时不请求 query embedding，按配置降级或抛出 RetrievalUnavailableError。语义结果低于阈值是正常空结果；不会把 RRF 分数当作相关性阈值。

实现对方案缓存策略作了保守调整：只使用实例内 64 条查询向量 LRU，键包含用户、会话、项目、精确查询哈希和空间。精确查询哈希避免对大小写敏感的模型输入错误复用。**不缓存最终排名或来源包**，每次重读权威候选；诊断仍报告 retrieval_generation 和 data_version。因此同连接提交、索引发布和跨连接更改均不会从旧结果缓存恢复遗忘内容。每类重排输入前复核；所有外部调用结束后，在一个短读快照中统一再校验、hydrate 来源。变化的候选丢弃并报告 sources_changed，本版不自动重试整次检索。

重排默认关闭。开启时每类仅给既有 RRF 前缀发送 `{id,text}`，校验完整唯一 ID 集合、字段和 [0,1] 有限分数，宿主稳定排序；不能添加实体。异常、非法 JSON、超时或期限不足按种类回退 RRF。评分诊断不进入 `<lamemory_data>`；预算计算与事实实际注入使用同一 `render_memory_reference()`。context 的 retrieved/summaries/experiences 表示预算后真正注入的内容。

## 模型配置

四组配置分别管理：主聊天 `LLM_`、提取/摘要 `MEMORY_LLM_`、向量 `LAMEMORY_EMBEDDING_`、重排 `LAMEMORY_RERANK_LLM_`。Python 的 LaMemoryConfig 不读环境；宿主 runtime 工厂显式读取传入映射或进程环境。示例全部是占位符，不代表选择任何模型或供应商：

```dotenv
LAMEMORY_RETRIEVAL_MODE=hybrid
LAMEMORY_EMBEDDING_PROVIDER=openai-compatible
LAMEMORY_EMBEDDING_MODEL=your-embedding-model
LAMEMORY_EMBEDDING_BASE_URL=https://your-embedding-endpoint.example/v1
LAMEMORY_EMBEDDING_DIMENSIONS=1024
LAMEMORY_EMBEDDING_NAMESPACE=deployment-v1
LAMEMORY_EMBEDDING_API_KEY=
LAMEMORY_EMBEDDING_SEND_DIMENSIONS=false
LAMEMORY_EMBEDDING_TIMEOUT=10
LAMEMORY_EMBEDDING_MAX_RETRIES=0
LAMEMORY_RERANK_ENABLED=false
# 可选：独立的重排聊天模型，配置完整后再开启开关
# LAMEMORY_RERANK_LLM_PROVIDER=openai-compatible
# LAMEMORY_RERANK_LLM_MODEL_NAME=your-rerank-chat-model
# LAMEMORY_RERANK_LLM_BASE_URL=https://your-chat-endpoint.example/v1
# LAMEMORY_RERANK_LLM_API_KEY=
```

1024 必须改为实际输出维度。需要鉴权的端点必须提供其密钥；无需鉴权的本地端点允许空密钥。必需的 model/base_url/dimensions/namespace 不完整会在 hybrid 启动时明确报错；重排缺独立配置也会报错，不继承聊天密钥。算法环境开关包括 `LAMEMORY_{KEYWORD_CANDIDATE_K,SEMANTIC_CANDIDATE_K,RRF_K,KEYWORD_WEIGHT,SEMANTIC_WEIGHT,SEMANTIC_MIN_SIMILARITY,RETRIEVAL_TIMEOUT_SECONDS,ALLOW_KEYWORD_FALLBACK,RERANK_TOP_N,RERANK_TIMEOUT_SECONDS,EMBEDDING_BATCH_SIZE,MAX_EMBEDDING_CHARS}`，默认值见 config.py。

## 启动、回填、搜索与回退

以下均显式指定库；示例使用合成演示库。命令不会自行生成演示事实，先由可信宿主写入数据或使用本节 Python 示例。维护 CLI 全局参数放在子命令之前。先完成生成任务，再回填向量，两个 flush 的范围相互独立：

```bash
python -m core.lamemory --database /tmp/lamemory-demo.sqlite3 --user demo --session demo flush --timeout 30
python -m core.lamemory --database /tmp/lamemory-demo.sqlite3 --user demo --session demo --retrieval-mode hybrid index-enqueue
python -m core.lamemory --database /tmp/lamemory-demo.sqlite3 --user demo --session demo --retrieval-mode hybrid index-process --max-jobs 100
python -m core.lamemory --database /tmp/lamemory-demo.sqlite3 --user demo --session demo --retrieval-mode hybrid index-flush --timeout 60
python -m core.lamemory --database /tmp/lamemory-demo.sqlite3 --user demo --session demo --retrieval-mode hybrid index-status
python -m core.lamemory --database /tmp/lamemory-demo.sqlite3 --user demo --session demo --retrieval-mode hybrid search '管理 Python 依赖的工具'
python -m core.lamemory --database /tmp/lamemory-demo.sqlite3 --user demo --session demo --retrieval-mode hybrid index-retry emb_job_id
python -m Agent.chatBot_with_memory.main --database /tmp/lamemory-demo.sqlite3 --user demo --session demo --retrieval-mode hybrid --no-rerank
python -m Agent.chatBot_with_memory.web --database /tmp/lamemory-demo.sqlite3 --user demo --retrieval-mode hybrid --no-rerank
```

有项目范围时每条命令增加相同 `--project`，session 范围也需相同 `--session`。index-status 不发网络请求；无完整空间描述时返回 unselected，绝不拿其他空间覆盖率代替。`--offline index-status` 仍可用无密钥的完整空间描述查看状态。`--offline` 搜索与 Web `--demo` 强制 keyword、报告 offline，禁止向量与重排网络；offline 不执行 index-process/index-flush。CLI 完成为 0，配置错误为 2，index-flush failed/timeout 或 index-process 最终 failed 为非零。

```python
import tempfile
from pathlib import Path
from core.lamemory import LaMemoryConfig
from core.lamemory.integration.runtime import create_memory

with tempfile.TemporaryDirectory() as directory:
    config = LaMemoryConfig(database_path=Path(directory) / 'demo.sqlite3')
    with create_memory(config, retrieval_mode='hybrid', user_id='demo', session_id='demo') as memory:
        source = memory.append_message({'role': 'user', 'content': '使用 uv 管理 Python 依赖'})
        memory.store('使用 uv 管理 Python 依赖', [source])
        print(memory.flush_embeddings(timeout=30))
        print(memory.retrieve('管理依赖的工具').diagnostics)
```

终端和正常 Web 回复先展示，再执行原有至多一个生成任务，随后 hybrid 至多处理 16 个索引任务，外部请求共享 10 秒期限；积压保留，失败不重新生成主回复、不回滚显式保存。独立 Runner 不消费索引任务。hybrid 的 lamemory_store 返回 semantic_index_status=pending，只承诺事实保存；缺向量时仍立即支持关键词查询。Web 保留 `/api/memories` 数组兼容接口，新增 `/api/memory-search` 返回三类命中、实际模式、降级与可见范围覆盖率；面板展示模式与索引状态。保存聊天模型不会修改 embedding 或 rerank 配置，凭据不发送给浏览器。

运行时 embedding 异常默认回退原关键词分数与排序，实际模式为 keyword 或 mixed，原因在 diagnostics。设置 `LAMEMORY_ALLOW_KEYWORD_FALLBACK=false` 则抛可处理错误；rerank 异常始终保留 RRF。切回新代码的 keyword 即可回退：设置 `LAMEMORY_RETRIEVAL_MODE=keyword`、`LAMEMORY_RERANK_ENABLED=false`，或显式 `--retrieval-mode keyword --no-rerank`。保留投影和证据，不需要向下迁移；未验证旧应用版本读取 schema v4 的兼容性。

## 实际验证与剩余限制

所有本次测试与评测均使用临时数据库、合成消息及可替换 provider，没有读取或改写真实记忆库，没有选用收费模型或安装服务。测试顺序按 A/B 先算法与 fake 索引，C 再真实 SDK 适配器 mock 协议，D/E/F 接三类、重排与宿主，G 最后评测。

基线首次执行 69 项，66 项通过，3 项 HTTP 测试被沙箱禁止绑定临时端口；后续完整回归获准绑定回环端口。最终回归数量和原始日志见 [本轮验证报告](../benchmarks/lamemory/results/hybrid-20261110-validation-final.json)。覆盖手算算法、非法向量/响应、三类共享 query、双路去重、top_k=0/100、无答案阈值、用户/session/project 隔离、旧库升级、事务回滚、CAS、重启、failed retry、repair、同连接发布与跨连接遗忘、dirty 主题/撤销经验、跨种类重排失效、预算、工具、终端、CLI、Web HTTP/SSE 与离线零模型路径。

40 查询 fixture 含 32 个有答案查询、8 个无答案查询；校准与验收分开，当前 cosine 阈值仍为工程默认值 0.3，未用验收答案调参。评测固定合成权威快照和实体 ID（重复运行已验证稳定排名），按种类计算二值相关性 Recall@5/MRR@5/nDCG@5，记录失败样例、候选/选中/注入数、延迟、覆盖率、降级和分类 token 用量；没有配置单价，不估算费用。

本次 keyword 验收集 34 查询（28 有答案、6 无答案）：Recall@5 **0.642857**、MRR@5 **0.625**、nDCG@5 **0.629676**，无答案误召回率 **1/6**。失败样例和本机延迟见原始结果，不能推导生产延迟。真实 hybrid、hybrid+rerank 及 smoke 因缺少完整 embedding model/base_url/dimensions/namespace **blocked / 未执行**，没有真实语义收益或重排收益结论。fake 固定向量只证明协议、链路和一致性。

```bash
.venv/bin/python -m unittest discover -s tests/lamemory -p 'test_*.py'
.venv/bin/python -m unittest discover -s tests/providers -p 'test_*.py'
# 输出目录/文件须尚不存在；全部使用临时合成库
.venv/bin/python -m benchmarks.lamemory.hybrid_retrieval --mode all --output /tmp/lamemory-hybrid-new-eval
.venv/bin/python -m benchmarks.lamemory.hybrid_smoke --output /tmp/lamemory-hybrid-new-smoke.json
# 配置好独立重排模型后可加 --rerank
```

剩余事项：配置真实模型后执行上述 smoke 与两种真实评测，检查无答案样本并独立校准阈值，再决定是否开启 rerank。当前为 Python 精确 O(N×D) 扫描和 SQLite 单进程串行消费；无 ANN、多 worker、分块索引或自动空间垃圾回收，协调与候选物化仍需扫描可见范围。只有查询向量缓存，无最终排名缓存；来源持续变化时会少召回，需下一次检索。字符截断可能丢失长经验尾部的信息。不得用本次结果声称复杂任务质量、语义收益或重排收益已验证。
