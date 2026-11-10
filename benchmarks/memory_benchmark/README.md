# LaMemory 任务 benchmark

这个运行器使用 `benchmarks/memory_task` 冻结的 8 题、历史种子、固定追问和评分标准，比较历史 Markdown 算法（`legacy`）与 LaMemory（`lamemory`），默认加上无长期记忆基线（`none`）。不修改任务资产，也不访问 `core/chat_memory/` 或默认 LaMemory 数据库。

已完成的是运行脚本与离线验证。尚未执行真实模型的完整对照，不能将离线占位分数解释成记忆效果。

## 运行

在项目根目录使用现有虚拟环境。默认子命令是 `estimate`，不会请求模型或建立记忆库。

```bash
# 8题 × 3组 × 3次重复；预估文件必须使用新路径
.venv/bin/python -m benchmarks.memory_benchmark estimate --output /tmp/memory-estimate.json

# 如果只比较两套记忆系统
.venv/bin/python -m benchmarks.memory_benchmark estimate --groups legacy lamemory

# 无网络、无模型的完整协议 smoke；3组 × 8题 × 1次重复 × 3轮
.venv/bin/python -m benchmarks.memory_benchmark smoke --repeats 1

# 真实模型单题试跑，校准 token、输出上限与评分质量
.venv/bin/python -m benchmarks.memory_benchmark run \
  --tasks 01_parents_birthday --repeats 1 --max-cost 1

# 正式运行；实际支出按当前价格表估算并逐请求检查预算
.venv/bin/python -m benchmarks.memory_benchmark run --max-cost 10
```

`run` 会发送付费模型请求。默认产物在 `.myclaude/benchmarks/<mode>-<timestamp>/`，可以用 `--output /absolute/new/directory` 指定。已存在目录不会覆盖。费用不足、建库失败、隔离失败或中断均保留已有报告与证据，退出码为 2；完整运行退出码为 0。当前没有断点续跑，失败后请使用新目录，避免在不同状态下续接实验。

配置示例为 [config.example.json](config.example.json)。通过 `--config` 加载；`--groups`、`--tasks`、`--repeats`、`--max-cost` 可覆盖对应字段。所有组共用窗口、输出上限、工具定义、最大模型调用数、最大工具调用数和固定追问。默认每个用户轮次最多 8 次模型调用、24 次工具调用；不是把一次工具循环当作一个用户轮次。

## 模型与价格

主模型读取项目 `.env` / 进程环境的 `LLM_*`。存在 `MEMORY_LLM_*` 时按项目已有规则独立解析记忆模型，否则共用主模型。存在 `BENCH_JUDGE_LLM_*` 时独立解析评分模型，否则评分共用主模型。独立前缀需要完整的供应商、型号与凭据配置，例如：

```dotenv
BENCH_JUDGE_LLM_PROVIDER=deepseek
BENCH_JUDGE_LLM_MODEL_NAME=deepseek-flash
BENCH_JUDGE_LLM_API_KEY=...
```

三类模型均复用项目 `ChatModel` 适配层。SDK 自动重试关闭，记忆任务最多尝试 3 次，所有实际尝试计费。原有 `LLM_TEMPERATURE`、`LLM_REASONING_EFFORT` 等配置继续生效；模型推理也占输出 token。若提取或评分经常因输出截断失败，应提高输出上限并重新预估，而不是把失败记为无记忆或零分通过。报告中不保存密钥或请求头。

`--prices` 接受 JSON，格式见 [prices.deepseek-flash.json](prices.deepseek-flash.json)。每类角色（`agent` / `memory` / `judge`）分别填写 `provider`、`model` 及 `input`、`output`、`cache_read`、`cache_write` 的**每百万 token**单价，币种由 `currency` 指定。实际运行会核对定价的供应商/型号与模型配置。网关、自定义端点或合约价应使用自己的价格表；仅有相同模型名称不能证明网关收费相同。

默认表是 2026-11-10 查阅 [DeepSeek 官方价格](https://api-docs.deepseek.com/quick_start/pricing/) 的 Flash **高峰价**快照：未命中输入 $0.30/M、输出 $1.20/M、命中输入 $0.006/M。官方非高峰费率为其一半。脚本不自动按时段切价；全程按选定价格快照核算，报告保留价格来源与配置。

预估分为 `low`、`expected`、`configured_ceiling`。常规假设是每轮 3 次模型调用、每次输出约 900 token、每轮交付物约 2400 token，另计前轮历史、工具参数和结果的重复输入。建库按 44 条用户消息、21 个会话计算；每个有记忆后端只建一次库，所有题与重复共享该初始快照。

默认 **8题 × 3组 × 3次 = 72次任务运行、216个用户轮次、216次独立评分**：

| 情景 | 输入 token | 输出 token | 费用（USD） |
|---|---:|---:|---:|
| 较轻输出/调用假设 | 5,439,385 | 728,920 | 2.51 |
| 常规假设 | 8,908,264 | 923,320 | 3.78 |
| 配置上限情景 | 63,832,224 | 12,005,376 | 33.56 |

仅 `legacy lamemory` 两组、各重复 3 次时为 **48 次任务运行**；常规假设约 **6,970,960 输入 + 642,520 输出 token，$2.86**。

这里的 token 是带消息/工具包装的 UTF-8 字节数除以 3 的粗估，不是供应商 tokenizer。全部预估输入按缓存未命中计价；真实运行优先记录供应商 usage，并利用适配层提供的缓存读写量核算。调用失败或没有 usage 时，以请求输入粗估及最大输出预留费用，标记为估算，不冒充零成本。价格表费用不包含容器/机器成本。

`configured_ceiling` 把 Agent 调用、输出、记忆重试、分层刷新及旧算法可能触发的压缩计入。摘要源长度、推理长度和真实分词取决于模型，所以它是高用量情景，不能当作供应商账单的绝对上限。`--max-cost` 在发送每个请求前检查“累计核算费用 + 本次预留”；若配置币种为 USD，则数值单位为美元。它同样依赖 token 粗估和静态单价。

建库、任务在线、评分分阶段计量；Agent、提取、摘要分开记录。固定追问直接使用资产包文本，不调用用户模拟模型；当前没有 embedding 或 rerank 费用。

## 对照协议

1. 启动时运行原任务包 `validate.py`，检查全部冻结 SHA-256、历史依据和参考差异结果。不会重新冻结任务资产。
2. `legacy` 与 `lamemory` 按相同数组顺序回放全部种子。每条原始用户消息后追加固定确认“已收到。”，按后端正常流程提交并等待提取成功。跨会话只重置会话历史，长期记忆继续共享。保存本地消息 ID → 运行时来源 ID、原始日期/种类和正文 SHA-256 映射。LaMemory 默认在建库完成后显式启用用户概览（M2）；M3 自动经验学习关闭。
3. 每个题目、每次重复、每组从对应初始快照克隆独立存储，并创建新会话、空交付目录。`none` 不导入种子，保留本题对话历史，完全不执行长期记忆提取或注入。冻结种子不会被任务中的新写入改动。
4. 三组复用 `Agent.chatBot_with_memory.runner.ChatRunner` 和项目工具执行器，仅提供共同的 `read/write/ls/python` 工具。三组均关闭主动记忆工具与静态 Skill，因此这里测量的是**自动提取和上下文注入**，尚未评估主动搜索/更新工具的收益。LaMemory 使用现有关键词召回和预算策略；旧算法全量注入 Markdown。后者超出共同输入预算时明确失败，不偷偷改变其检索算法。
5. 固定执行初始请求和两条追问，每轮保存完整工具轨迹、回复、产物快照与哈希。评分不会作为下一轮反馈；任务 Agent 看不到 rubric、私有证据和参考答案。为减少时间顺序影响，按任务/重复交错运行各组。若工具链因错误未完成，后续轮次标记为跳过，不拼接非法历史或把旧产物当成新一轮成功。
6. 每轮由独立、无任务工具的评分调用逐项盲评。只提供当轮产物、正文/工具轨迹、该题 rubric、相关种子证据和执行结果；移除上下文诊断并把含组名的宿主交付路径归一化。宿主校验项 ID、分值范围、非有限数字和重复/缺失项，再自行求总分、维度分和阈值。
7. 配置题在隔离环境重跑 `python diff_configs.py current.json proposed.json`，分别核对提交版/重跑版报告与 16 项参考变更。代码评审题实际执行提交的 unittest，另在只含原始提交修复模块的工作区执行独立契约检查，核对原文件字节不变。并发压力测试仅是执行证据，评审器仍需检查是否正确加锁。

某些评分文字自身有局部分值歧义（如修复项描述的每类分值相加大于该项 `max_score`），评分器明确以 JSON 的 `max_score` 为上限，记录折算理由；任务包原文件保持不变。

首轮/最终均分的有效评分数量单独记录。运行失败仍计入通过率分母；即使失败产物被评高分也不计为成功通过。报告包含逐题 `memory/mixed/general` 得分与满分、每次运行最早达标轮次、首轮/最终分数、错误和耗时。`mixed` 不是纯记忆召回准确率，本题集也不能证明程序经验迁移有效。

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
  seeds/<group>/                # 冻结存储、来源映射、任务/重试记录
  runs/<task>/repeat-N/<group>/
    private/                    # 本题自己的记忆快照，模型文件工具不可见
    work/                       # 本题公开资产与产物
    round-N/
      prompt.md / trace.jsonl / response.json
      artifacts/                # 此轮产物快照
      memory_jobs.json          # 本轮记忆处理与失败/重试记录
      judge_response.json       # 原始评分输出（无效 JSON 也保留）
      execution.json / result.json
```

旧算法原始文件冻结于 `72fa6ee96492f92ea60887cef6d3681bc549bbd8:core/memory.py`，保存在 [frozen/legacy.py](frozen/legacy.py)，SHA-256 为 `d6b346be5c8323b0bc73167ff5a93f8322190c5acb1865998dfb7d19773b4a1a`。适配器不改该文件，只在实例级注入独立路径和模型，绕过旧的 usage 门控以便正常提交合成确认轮次，暴露原来吞掉的无效 JSON，并把压缩输出的记忆数组转换成 Markdown，修复原来的数组拼接错误。旧算法的提取提示词、追加式去重/更正语义及全量注入方式保持原样；它可能保留过时信息，这是本对照需要观察的行为。

```bash
.venv/bin/python -m unittest discover -s tests/benchmarks -p 'test_*.py' -v
.venv/bin/python -m unittest discover -s tests/lamemory -p 'test_*.py'
```

2026-11-10：benchmark 回归 15 项、既有 LaMemory 回归 69 项、供应商回归 20 项全部通过。覆盖无模型的 24 次任务/72 轮完整流程、快照隔离、来源映射、盲评输入、费用与缓存计量、预算阻断、错误状态，以及 macOS 中的私有文件/网络阻断和实际 unittest 执行。假模型的所有任务评分固定为 0，真实模型调用数和实际费用均为 0。若宿主禁止嵌套沙箱，隔离项会明确跳过，不能当作隔离验证通过。
