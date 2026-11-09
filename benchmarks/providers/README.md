# 真实模型调用测试

当前 `.env` 的 DeepSeek `deepseek-flash` 已完成真实测试，端点为 `api.deepseek.com`。本次未改变模型或凭据配置，也未启用额外推理参数。

| 检查 | 结果 |
|---|---|
| 文本调用 | 通过，返回 `MODEL_OK` |
| 工具调用 | 通过，生成 `test_add(a=17,b=25)` |
| 工具结果回传 | 通过，收到结果 42 后返回 `TOOL_OK=42` |
| 记忆提取与持久化 | 通过 |
| 跨会话检索及上下文召回 | 通过 |
| 来源追溯、用户隔离及回答引用 | 通过 |

本次耗时 10.587 秒，所有模型请求合计 3628 tokens。该数字是本次小规模测试用量，不是长期成本估计或准确率评测。

完整记录见 [live-smoke.json](results/live-smoke.json)，记忆分项记录见 [real-smoke.json](../lamemory/results/real-smoke.json)。报告只保存模型、端点主机、合成测试数据、用量和检查结果，已核对不包含配置中的密钥。记忆使用临时 SQLite 数据库，不读取或改写真实会话。

重新测试：

```bash
python -m benchmarks.providers.live_smoke
```

该命令从项目 `.env` 读取当前配置，执行真实 API 请求并更新报告。当前结果只覆盖此模型配置下的文本、工具和关键词记忆流程，不代表其他模型、多模态、流式输出或所有推理设置已验证。
