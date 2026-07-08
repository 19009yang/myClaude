"""Skill 加载模块
- load(path): 解析单个 SKILL.md 文件
- SkillRegistry: 扫描 skills/ 目录，按 name 建索引，支持 list/get
"""

import yaml
from pathlib import Path

SKILLS_DIR = Path(__file__).parent / "skills"


def load(path: str) -> tuple[dict, str]:
    """解析 SKILL.md 文件，返回 (metadata, content)。"""
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()

    if not content.startswith("---"):
        return {}, content

    parts = content.split("---", 2)
    if len(parts) < 3:
        return {}, content

    metadata = yaml.safe_load(parts[1])
    body = parts[2].strip()

    return metadata or {}, body


class SkillRegistry:
    """扫描 skills/ 目录下所有 SKILL.md，按 name 建索引。"""

    def __init__(self, skills_dir: Path | str = SKILLS_DIR):
        self.skills_dir = Path(skills_dir)
        # name -> (metadata, body, path)
        self._skills: dict[str, tuple[dict, str, Path]] = {}
        self._scan()

    def _scan(self):
        """扫描目录，加载所有 SKILL.md"""
        if not self.skills_dir.exists():
            return
        for skill_file in self.skills_dir.rglob("SKILL.md"):
            metadata, body = load(str(skill_file))
            name = metadata.get("name") or skill_file.parent.name
            self._skills[name] = (metadata, body, skill_file)

    def list_skills(self) -> list[dict]:
        """返回所有可用 skill 的摘要列表 [{name, description}]"""
        return [
            {"name": name, "description": meta.get("description", "")}
            for name, (meta, _, _) in self._skills.items()
        ]

    def get_skill(self, name: str) -> str | None:
        """返回指定 skill 的正文内容，不存在则返回 None"""
        entry = self._skills.get(name)
        return entry[1] if entry else None


# 默认单例，供外部直接使用
_default_registry: SkillRegistry | None = None


def get_default_registry() -> SkillRegistry:
    """获取默认 SkillRegistry 单例"""
    global _default_registry
    if _default_registry is None:
        _default_registry = SkillRegistry()
    return _default_registry


if __name__ == "__main__":
    import sys

    registry = SkillRegistry()

    print("=" * 50)
    print("Skill Registry Demo")
    print("=" * 50)

    print("\nAvailable skills:")
    for s in registry.list_skills():
        print(f"  - {s['name']}: {s['description']}")

    if len(sys.argv) > 1:
        name = sys.argv[1]
        print(f"\nSkill '{name}' content:")
        content = registry.get_skill(name)
        if content:
            print(content[:500])
        else:
            print(f"  Skill '{name}' not found")
