# myClaude 开发约定

## 记忆系统命名与范围

- 记忆系统统一命名为 **Layered Aggregated Memory**，简称 **LaMemory**。
- 新记忆模块使用 `core/lamemory/`，服务类使用 `LaMemoryService`，主动工具和数据表使用 `lamemory_` 前缀。
- 文档、代码、注释、示例、文件名、提交说明和评测产物统一使用该命名。
- 设计与实现根据 myClaude 的需求独立定义，不引入其他记忆项目的名称、源码路径、API 符号、实现代码、运行环境记录或专属对照组信息。
- 已有 `core/memory.py` 及其接口可保留为兼容入口，不因文档命名调整宣称已完成运行时实现。

## 文档入口

- [LaMemory 架构与实施方案](docs/lamemory-design.md)
- [LaMemory 开发指南](docs/lamemory-development-guide.md)

## 实施与验证

- 区分现有功能、待实现方案与实际测试结果。
- 记忆相关测试使用临时目录、临时数据库和可替换模型，避免改写真实会话数据。
- 保留原始证据；更正、删除和聚合摘要的更新需要有明确的一致性规则。
