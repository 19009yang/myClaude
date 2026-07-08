---
name: hello
description: A simple greeting skill that says hello in different languages
version: 0.1.0
author: learn-project
tags:
  - greeting
  - demo
  - example
parameters:
  language:
    type: string
    description: The language to greet in (en, zh, ja, es, fr)
    default: en
---

# Hello Skill

这是一个示例 Skill，用于验证 `skill_loader.py` 是否能正确解析 SKILL.md 文件。

## 功能

根据指定的语言返回问候语：

- `en` → "Hello, World!"
- `zh` → "你好，世界！"
- `ja` → "こんにちは、世界！"
- `es` → "¡Hola, Mundo!"
- `fr` → "Bonjour, le Monde!"

## 使用方式

```python
from tools.skill_loader import load

meta, body = load("tools/skills/hello/SKILL.md")
print(meta)  # 输出 YAML frontmatter 解析后的字典
print(body)  # 输出 Markdown 正文内容
```

## 注意事项

- 此 Skill 仅作为示例，不包含实际执行逻辑
- 主要用于验证 YAML frontmatter 解析功能
