"""
Skill Discovery: 动态读取 Skills/ 目录下的 SKILL.md 文件，构建 Skills 目录。
模型通过 Description 自主决定使用哪个 Skill。
"""

import os
import re
from pathlib import Path


def load(skills_root: str = None) -> dict:
    """
    扫描 Skills/ 目录，读取所有 SKILL.md，返回 skills_catalog。
    返回格式:
    {
        "pdf": {
            "name": "PDF Skill",
            "description": "...",
            "scripts_dir": "...",
            "skill_md": "..."
        },
        "xlsx": { ... }
    }
    """
    if skills_root is None:
        skills_root = Path(__file__).parent.parent / "Skills"

    skills_root = Path(skills_root)
    catalog = {}

    for skill_md_path in skills_root.rglob("SKILL.md"):
        skill_dir = skill_md_path.parent
        skill_key = skill_dir.name  # "xlsx" or "pdf"

        content = skill_md_path.read_text(encoding="utf-8")

        # 从 YAML frontmatter 提取 name 和 description（Anthropic Progressive Disclosure Layer 1）
        frontmatter_name = skill_key
        frontmatter_description = None
        fm_match = re.match(r"^---\s*\n(.*?)\n---", content, re.DOTALL)
        if fm_match:
            fm_text = fm_match.group(1)
            name_m = re.search(r"^name:\s*(.+)$", fm_text, re.MULTILINE)
            desc_m = re.search(r'^description:\s*["\']?(.*?)["\']?\s*$', fm_text, re.MULTILINE)
            if name_m:
                frontmatter_name = name_m.group(1).strip()
            if desc_m:
                frontmatter_description = desc_m.group(1).strip()

        # fallback: 首个 # 标题
        if frontmatter_name == skill_key:
            for line in content.splitlines():
                if line.startswith("#"):
                    frontmatter_name = line.lstrip("#").strip()
                    break

        scripts_dir = skill_dir / "scripts"
        scripts = []
        if scripts_dir.exists():
            scripts = [str(p.relative_to(Path(__file__).parent.parent)) for p in scripts_dir.rglob("*.py")]

        catalog[skill_key] = {
            "name": frontmatter_name,
            "frontmatter_description": frontmatter_description,
            "skill_md_path": str(skill_md_path),
            "scripts_dir": str(scripts_dir) if scripts_dir.exists() else None,
            "scripts": scripts,
            "description": content,
        }

    return catalog


def format_for_discovery(catalog: dict) -> str:
    """
    Anthropic Progressive Disclosure — Layer 1 (startup).
    只注入 name + one-line description + SKILL.md 路径。
    成本 ~80 tokens/skill，让 agent 知道"有哪些 skill"但不塞满 context。
    Agent 按需调用 read_file(skill_md_path) 读完整内容（Layer 2）。
    """
    lines = []
    for key, skill in catalog.items():
        desc = skill.get("frontmatter_description") or skill["name"]
        # 截取 description 第一句（到第一个句号或最多 120 chars）
        first_sentence = desc.split(".")[0].strip()
        if len(first_sentence) > 120:
            first_sentence = first_sentence[:120] + "…"
        lines.append(
            f'- {key}: {first_sentence}. '
            f'Read full instructions: read_file("{skill["skill_md_path"]}")'
        )
    return "\n".join(lines)


def format_for_prompt(catalog: dict) -> str:
    """将 skills_catalog 格式化成给 Planner 看的 prompt 文本（包含前 100 行预览）。"""
    lines = ["## Available Skills\n"]
    for key, skill in catalog.items():
        lines.append(f"### Skill: `{key}`")
        lines.append(f"**Name**: {skill['name']}")
        lines.append(f"**SKILL.md path**: {skill['skill_md_path']}")
        if skill["scripts"]:
            lines.append(f"**Available scripts**: {', '.join(skill['scripts'])}")
        lines.append("")
        preview = "\n".join(skill["description"].splitlines()[:100])
        lines.append(f"**Description preview**:\n```\n{preview}\n```\n")
    return "\n".join(lines)


if __name__ == "__main__":
    catalog = load()
    print(f"Loaded {len(catalog)} skill(s): {list(catalog.keys())}")
    print(format_for_prompt(catalog))
