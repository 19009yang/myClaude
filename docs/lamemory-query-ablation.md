# LaMemory 固定快照查询对照

用于定位“记忆有向量，但没有进入语义候选”的原因。入口：

```bash
.venv/bin/python -m benchmarks.lamemory.query_ablation --help
```

## 对照定义

| 方案 | 关键词输入 | embedding 输入 | 与上一组相比 |
|---|---|---|---|
| A：`raw` | 原始任务请求 | 原始任务请求 | 基线 |
| B：`instruct` | 与 A 相同 | 检索指令 + 原始任务请求 | 只改变查询侧指令 |
| C：`facts` | 所需事实查询 | 与 B 相同的检索指令 + 所需事实查询 | 只改变任务请求的表述 |

原始任务请求指去掉评测协议头和已识别的独立交付段后的用户请求，不是带文件名、宿主路径和复查要求的整条提示。提取规则复用 `benchmarks/memory_benchmark/queries.py`。

C 先采用调用方手工固定的 `--facts-query`，用于单独研究改写本身的效果，不引入改写模型的随机性、模型调用或额外费用。只能从公开请求提取需要查找的事实类型，不能填入从记忆或 rubric 读到的答案。例如：

```text
原请求：帮我在日历上加一下我爸妈的生日提醒，我老是忘。要提前提醒我好准备礼物。
事实需求：父亲和母亲的姓名、生日日期、生日提醒偏好，以及相关礼物待办
```

不应在改写中补入具体姓名、生日、提前天数、购买渠道或具体礼物。若后续接入自动改写，应让改写模型只接收公开请求，先保存改写结果再检索，沿用同一评测入口。

默认检索指令：

```text
Instruct: Given a user's request, retrieve personal facts, preferences, and pending tasks needed to fulfill it.
Query: <查询>
```

## 先固定实验，再执行

以本次已完成评测为例，先创建新实验目录：

```bash
.venv/bin/python -m benchmarks.lamemory.query_ablation prepare \
  --run .myclaude/benchmarks/run-20261111-144240-154353 \
  --task 01_parents_birthday \
  --facts-query '父亲和母亲的姓名、生日日期、生日提醒偏好，以及相关礼物待办' \
  --target-slot family.father.birthday \
  --target-slot family.mother.birthday \
  --repeats 3 \
  --output .myclaude/benchmarks/query-ablation-parents
```

`prepare` 不调用模型。它读取源运行的配置与公开首轮提示，把 `seeds/lamemory/lamemory.sqlite3` 复制成实验内的 `snapshot.sqlite3`，验证原文件与副本 SHA-256 一致，检查可见 active 记忆的向量维度、来源 generation、输入哈希与空间描述。含非空 WAL 的库会被拒绝，避免 immutable 读取漏掉未 checkpoint 的内容。

所有查询、指令、身份范围、标注、候选数、阈值、RRF 参数、定价和重复次数固定在 `plan.json`，并保存计划哈希。默认身份与原 benchmark 相同；可显式指定 `--user`、`--session`。目标 slot 仅用于观察排名和计算指定目标的覆盖率，不参与构造查询。

审阅 `plan.json` 后执行：

```bash
.venv/bin/python -m benchmarks.lamemory.query_ablation run \
  --experiment .myclaude/benchmarks/query-ablation-parents \
  --max-cost 0.1
```

真实运行读取项目 `.env` 与进程环境中的 embedding 端点和凭据。端点指纹、模型、维度必须与冻结配置一致；实验使用源报告的定价，`--max-cost` 单位为该定价的币种。每次请求前预留估算费用，失败但有 usage 的调用也留档计量。金额是配置快照核算，不是供应商账单。

三次重复共请求 **9 个查询向量**。每次重复轮换 A/B/C 的请求顺序，不使用本地查询缓存，不请求文档向量，不重新提取记忆、生成摘要、调用执行模型或评分模型。已有结果不覆盖；重做实验需新建输出目录。

## 固定什么、改变什么

- 共用同一份冻结原子记忆和文档向量；只检索当前身份可见的 active item，排除其他会话/用户/项目的数据。
- 保留原运行的 cosine 阈值、通道候选数、top-k 和 RRF 权重；固定关闭重排。
- 不实例化 `LaMemoryService`，避免初始化、迁移、摘要维护和索引任务改变快照。
- provider 的隐式 `query_prefix` 固定为空，各组完整 embedding 输入显式记录，避免环境前缀叠加两次。
- 查询模板是实验变量。直接对冻结文档向量计算 cosine，不把 B/C 当成新的运行时空间建索引，也不修改生产空间的隔离规则。
- 比较原子记忆的排名与候选覆盖，不运行 profile/topic/experience 检索或上下文预算渲染，因此不是完整任务分数评测。

## 如何读结果

`results/report.md` 是目标事实对照表；`results/report.json` 保存各次结果索引、目标覆盖率、usage 与成功/失败状态。每组每次单独的 JSON 包含：

- 实际关键词查询、完整 embedding 输入、归一化查询向量及其哈希；可以不再请求 API，复核原始分数。
- 全部可见记忆的原始语义排名与 cosine，包括低于阈值和没进候选池的条目。
- 关键词排名、语义候选排名、融合排名与最终 top-k；排序和 RRF 复用现有实现。
- `semantic_exclusion`：`below_similarity_threshold` 表示低于阈值，`outside_candidate_limit` 表示达到阈值但被候选宽度截断，null 表示进入语义候选。

判断顺序：先比较 A/B 的目标 cosine 与语义排名，观察指令是否有效；再比较 B/C，观察事实需求改写是否有效；最后看语义候选与融合排名，区分编码效果和融合规则的影响。不能只看最终 top-k 命中，因为关键词通道可能掩盖语义通道的问题。

`target_coverage_at_k` 只衡量预先指定的目标事实，不等于对所有相关记忆的 Recall@k。一次任务的效果也不能外推到其他任务。

## 已验证范围

隔离测试使用临时数据库和可替换模型，覆盖原文件不变、可见性、三组输入、完整排名、阈值/截断诊断、与运行时原请求排名一致、重复顺序、故障留档、预算拦截和重复前缀拒绝：

```bash
.venv/bin/python -m unittest discover -s tests/benchmarks -p 'test_query_ablation.py'
```

假向量验证链路，不代表真实语义收益；需要显式执行真实 `run` 才能取得三组效果数据。
