当前评测日期固定为2025-03-26（Asia/Shanghai），相对日期以此计算，不使用机器当前日期。你正在帮助当前绑定的评测用户；可使用本组已经获得授权的长期记忆和本任务内历史。
本任务离线完成，不联网，不访问真实日历、Notion、购物或其他账号。用Markdown表达事件、模板或方案即可；不能声称已在外部应用执行。未知事实请标待确认，推测或建议请明确标注。
交付目录是 `{{output_dir}}`。请实际写入文件，聊天回复只给简短总结。

工作目录里有一个 Python 文件 `buggy_module.py`（路径 `{{output_dir}}/buggy_module.py`），它含有若干**未暴露**的缺陷，请你做一次完整的代码评审。

请产出三个交付文件，**全部写到 `{{output_dir}}/` 下**：

1. `buggy_module_fixed.py` —— 修复后的完整版本
2. `test_buggy_module.py` —— 单元测试（pytest 或 unittest）
3. `review_report.md` —— 评审报告

**保留原始的 `buggy_module.py` 不要删除**，方便评审方对比。

不要把代码或报告全文塞进 chat 回复——chat 里写概要 + 关键决策即可，文件评审方会自己读。

保持原有公开类和函数名。parse_user_input 对合法整数或整数字符串返回年龄的两倍，缺少age或值不能转换为整数时抛ValueError；divide_safe 正常返回商，除零抛ZeroDivisionError，非数值参数抛TypeError。get_top_n 的n为非负整数，n=0返回空列表。Counter支持并发安全递增。测试使用Python标准库unittest，不依赖安装第三方包。
