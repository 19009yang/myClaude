# LaMemory 模块导航

目录按功能分组。顶层保留公共导出、配置、公共返回模型、统一服务和维护 CLI。

```text
core/lamemory/
├── __init__.py                   # LaMemoryConfig / LaMemoryService 等公共导出
├── __main__.py                   # python -m core.lamemory
├── config.py                     # 算法配置与默认数据库路径
├── models.py                     # 公共数据对象与返回类型
├── service.py                    # 统一服务、写事务与任务编排
├── generation/                   # 记忆生成、派生视图与整理
│   ├── extraction.py             # 候选校验、时间规范化
│   ├── providers.py              # 提取、摘要与经验模型适配
│   ├── hierarchy.py              # profile / topic 摘要
│   ├── procedural.py             # 经验候选、反馈、晋升与撤销
│   └── consolidation.py          # 精确去重与整理
├── retrieval/                    # 候选召回与排序
│   ├── keyword.py                # 原关键词算法
│   ├── engine.py                 # 三类混合检索编排
│   ├── sources.py                # 可见候选物化与来源 hydrate
│   ├── types.py                  # 检索契约、诊断与可处理错误
│   ├── fusion.py                 # cosine、向量校验与 RRF
│   └── reranking.py              # 可选 LLM JSON 重排
├── embedding/                    # 向量模型与索引
│   ├── provider.py               # EmbeddingConfig / SDK 适配器
│   ├── space.py                  # 空间指纹与输入模板
│   └── index.py                  # 持久化索引任务、CAS、repair / retry
├── storage/                      # 数据持久化与迁移
│   ├── store.py                  # SQLite、事务与 schema 加载
│   ├── legacy.py                 # 旧文件预览、备份和幂等导入
│   └── migrations/               # 原有版本化 SQL，内容保持不变
└── integration/                  # 接入聊天与宿主
    ├── context.py                # 上下文预算与参考数据渲染
    ├── tools.py                  # 绑定身份的主动记忆工具
    └── runtime.py                # 环境工厂、客户端所有权与有界维护
```

公共入口保持：

```python
from core.lamemory import LaMemoryConfig, LaMemoryService
```

按需使用功能模块：

```python
from core.lamemory.generation.providers import ChatMemoryProvider
from core.lamemory.embedding.provider import EmbeddingConfig, OpenAIEmbeddingProvider
from core.lamemory.integration.runtime import create_memory
from core.lamemory.integration.tools import get_memory_tools
from core.lamemory.storage.legacy import import_legacy
```

内部模块使用相对导入；Agent、兼容门面、评测和测试使用上述新包路径。维护命令仍为 `python -m core.lamemory`。`config.py` 留在顶层，默认数据库位置保持不变；SQL 迁移通过 `storage/store.py` 的相邻 `migrations/` 加载。目录重组不变更表结构、检索算法和任务语义。

架构与使用见 [设计](../../docs/lamemory-design.md)、[开发指南](../../docs/lamemory-development-guide.md) 和 [混合召回](../../docs/lamemory-hybrid-retrieval.md)。历史方案和原始评测报告保留当时的布局、路径与源码哈希，不作为当前导入路径使用。
