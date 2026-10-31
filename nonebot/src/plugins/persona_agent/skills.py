from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from nonebot.log import logger

from .decisions import SkillRequest


@dataclass
class Skill:
    name: str
    description: str
    body: str
    directory: Path
    version: str
    references: tuple[str, ...]


@dataclass
class ActiveSkills:
    requests: dict[str, list[str]] = field(default_factory=dict)
    instructions: str = ""
    versions: dict[str, str] = field(default_factory=dict)


class SkillLibrary:
    """A bounded instruction/reference host; it does not execute skill code."""

    def __init__(self, config):
        self.config = config.skills
        self.prompt_limit = min(
            self.config.max_prompt_chars, config.budget.context_token_budget
        )
        self.skills: dict[str, Skill] = {}
        self.unavailable: dict[str, str] = {}

    def load(self):
        if not self.config.enabled:
            return
        root = self.config.directory.resolve()
        if not root.is_dir():
            raise ValueError("Skill directory does not exist")
        for path in sorted(root.glob("*/SKILL.md")):
            name = path.parent.name
            if self.config.enabled_skills and name not in self.config.enabled_skills:
                continue
            try:
                if not path.resolve().is_relative_to(root):
                    raise ValueError("outside_skill_directory")
                text = self._read(path)
                front = re.match(r"\A---\s*\n(.*?)\n---\s*\n", text, re.S)
                if front is None:
                    raise ValueError("missing_frontmatter")
                meta = yaml.safe_load(front.group(1))
                if not isinstance(meta, dict):
                    raise ValueError("invalid_frontmatter")
                if (
                    meta.get("name") != name
                    or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name)
                    or len(name) > 64
                ):
                    raise ValueError("invalid_name")
                description = meta.get("description", "")
                if (
                    not isinstance(description, str)
                    or not 1 <= len(description.strip()) <= 1024
                ):
                    raise ValueError("invalid_description")
                if meta.get("allowed-tools"):
                    raise ValueError("requires_tool_runtime")
                body = text[front.end() :].strip()
                if not body or len(body) > self.prompt_limit:
                    raise ValueError("skill_body_exceeds_budget_or_empty")
                references = tuple(
                    str(item.relative_to(path.parent)).replace("\\", "/")
                    for item in sorted((path.parent / "references").rglob("*.md"))
                    if item.resolve().is_relative_to(path.parent.resolve())
                )[:32]
                self.skills[name] = Skill(
                    name,
                    description.strip(),
                    body,
                    path.parent.resolve(),
                    hashlib.sha256(text.encode()).hexdigest()[:12],
                    references,
                )
            except (OSError, ValueError, yaml.YAMLError) as exc:
                reason = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
                self.unavailable[name] = reason
                logger.warning("Skill unavailable: name={} reason={}", name, reason)
        for name in self.config.default_skills:
            if name not in self.skills:
                raise ValueError("Default skill is unavailable: " + name)
        self.initial("")
        logger.info(
            "Skills loaded: names={} unavailable={}",
            list(self.skills),
            self.unavailable,
        )

    @staticmethod
    def _read(path):
        if path.stat().st_size > 64 * 1024:
            raise ValueError("skill_file_too_large")
        return path.read_text(encoding="utf-8-sig").replace("\r\n", "\n")

    def explicit(self, text):
        match = re.match(r"^\s*\$([a-z0-9]+(?:-[a-z0-9]+)*)(?:\s|$)", text)
        return match.group(1) if match else None

    def initial(self, text):
        if not self.config.enabled:
            return ActiveSkills()
        names = list(self.config.default_skills)
        explicit = self.explicit(text)
        if explicit:
            names.append(explicit)
        return self.activate(
            ActiveSkills(), [SkillRequest(name=name) for name in dict.fromkeys(names)]
        )

    def activate(self, current, requests):
        selected = {name: list(refs) for name, refs in current.requests.items()}
        for request in requests:
            if request.name not in self.skills:
                raise ValueError("unknown_or_unavailable_skill")
            refs = selected.setdefault(request.name, [])
            for reference in request.references:
                if reference not in self.skills[request.name].references:
                    raise ValueError("unknown_skill_reference")
                if reference not in refs:
                    refs.append(reference)
        if len(selected) > self.config.max_active:
            raise ValueError("too_many_active_skills")
        blocks = []
        versions = {}
        for name, references in selected.items():
            skill = self.skills[name]
            blocks.append(f"【已加载技能 {name}】\n{skill.body}")
            digest = hashlib.sha256(skill.version.encode())
            for reference in references:
                path = (skill.directory / reference).resolve()
                if not path.is_relative_to(skill.directory):
                    raise ValueError("reference_outside_skill_directory")
                content = self._read(path)
                digest.update(content.encode())
                blocks.append(f"【技能参考 {name}/{reference}】\n{content}")
            versions[name] = digest.hexdigest()[:12]
        instructions = "\n\n".join(blocks)
        if len(instructions) > self.prompt_limit:
            raise ValueError("active_skills_exceed_context_budget")
        return ActiveSkills(selected, instructions, versions)

    def catalog(self, active):
        if not self.skills:
            return ""
        entries = []
        remaining = min(1800, max(0, self.prompt_limit - len(active.instructions)))
        for skill in self.skills.values():
            item = {
                "name": skill.name,
                "description": skill.description,
                "loaded": skill.name in active.requests,
                "references": list(skill.references),
                "loaded_references": active.requests.get(skill.name, []),
            }
            encoded = json.dumps(item, ensure_ascii=False)
            if len(encoded) <= remaining:
                entries.append(encoded)
                remaining -= len(encoded)
        return (
            (
                "【可按需加载的技能目录】\n"
                + "\n".join(entries)
                + "\n技能只提供任务方法与表达规则；没有实际执行的脚本、浏览器或外部服务结果，不能声称执行过。"
            )
            if entries
            else ""
        )

    def describe(self):
        if not self.config.enabled:
            return "技能功能尚未启用。"
        lines = ["可用技能："]
        for skill in self.skills.values():
            mark = "（默认）" if skill.name in self.config.default_skills else ""
            lines.append(f"{skill.name}{mark}：{skill.description}")
        if self.unavailable:
            lines.append("未加载：" + "、".join(self.unavailable))
        lines.append("指定技能：$技能名 你的请求。新增技能文件与配置修改在重启后生效。")
        return "\n".join(lines)
