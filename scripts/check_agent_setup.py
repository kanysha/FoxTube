#!/usr/bin/env python3
"""Check project-local agent files without dependencies, installation or network access.

Skill metadata deliberately uses flat YAML with JSON-quoted string values;
the checker validates that small format, not the complete YAML specification.
Native Codex discovery remains the compatibility check for the installed client.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from urllib.parse import unquote, urlsplit


def metadata_lines(text: str) -> dict[str, str]:
    """Parse only the intentionally simple metadata used in this project."""
    result: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        match = re.fullmatch(r"([a-z][a-z0-9_-]*): (.+)", line)
        if not match:
            raise ValueError("метаданные должны содержать плоские пары key: value")
        key, raw = match.groups()
        if key in result:
            raise ValueError(f"повторный ключ {key}")
        value = raw if re.fullmatch(r"[a-z0-9-]+", raw) else json.loads(raw)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{key}: требуется непустая строка")
        result[key] = value
    return result


def check_setup(root: Path) -> tuple[int, int]:
    errors: list[str] = []

    def error(path: Path, message: str) -> None:
        errors.append(f"{path.relative_to(root)}: {message}")

    required = (
        "AGENTS.md",
        "TestTask.md",
        "README.md",
        "Makefile",
        "docs/architecture.md",
        "docs/security.md",
        "docs/testing.md",
        "docs/agents.md",
        "docs/status.md",
        ".codex/config.toml",
    )
    for name in required:
        path = root / name
        if not path.is_file() or not path.read_text(encoding="utf-8").strip():
            error(path, "нет непустого обязательного файла")

    try:
        config = tomllib.loads(
            (root / ".codex/config.toml").read_text(encoding="utf-8")
        )
        agents = config["agents"]
        if not isinstance(agents, dict):
            raise ValueError("agents должен быть TOML-таблицей")
        if agents.get("enabled") is not True:
            raise ValueError("agents.enabled должен быть true")
        limit = agents.get("max_concurrent_threads_per_session")
        if type(limit) is not int or not 1 <= limit <= 3:
            raise ValueError("параллелизм дочерних агентов должен быть от 1 до 3")
        if set(config) != {"agents"} or set(agents) != {
            "enabled",
            "max_concurrent_threads_per_session",
        }:
            raise ValueError("проектная конфигурация меняет лишние настройки")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        error(root / ".codex/config.toml", str(exc))

    profiles = sorted((root / ".codex/agents").glob("*.toml"))
    names: set[str] = set()
    for path in profiles:
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
            for key in ("name", "description", "developer_instructions"):
                if not isinstance(data.get(key), str) or not data[key].strip():
                    raise ValueError(f"нет непустой строки {key}")
            name = data["name"]
            if not re.fullmatch(r"[a-z][a-z0-9_-]*", name) or name != path.stem:
                raise ValueError(
                    "name должен совпадать с именем файла: ASCII snake/kebab case"
                )
            if name in names:
                raise ValueError(f"повторное имя агента {name}")
            names.add(name)
            if set(data) != {"name", "description", "developer_instructions"}:
                raise ValueError(
                    "профиль должен наследовать модель, права и инструменты родителя"
                )
        except (OSError, ValueError) as exc:
            error(path, str(exc))
    if len(profiles) < 2:
        error(root / ".codex/agents", "ожидается несколько специализированных профилей")

    skills = sorted((root / ".agents/skills").glob("*/SKILL.md"))
    for path in skills:
        try:
            text = path.read_text(encoding="utf-8")
            match = re.match(r"\A---\n(.*?)\n---\n(.+)", text, re.DOTALL)
            if not match:
                raise ValueError("нет YAML frontmatter и содержимого навыка")
            data = metadata_lines(match[1])
            if set(data) != {"name", "description"}:
                raise ValueError("frontmatter должен содержать name и description")
            name = data["name"]
            if (
                not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name)
                or len(name) > 64
                or name != path.parent.name
            ):
                raise ValueError("неверное имя навыка или каталога")
            if len(data["description"]) > 1024:
                raise ValueError("description длиннее 1024 символов")
            ui_path = path.parent / "agents/openai.yaml"
            ui_text = ui_path.read_text(encoding="utf-8")
            if not ui_text.startswith("interface:\n"):
                raise ValueError("UI-метаданные должны начинаться с interface")
            lines = ui_text.splitlines()[1:]
            if any(not line.startswith("  ") for line in lines if line.strip()):
                raise ValueError("UI-метаданные требуют отступ в два пробела")
            ui = metadata_lines("\n".join(line[2:] for line in lines))
            if set(ui) != {"display_name", "short_description", "default_prompt"}:
                raise ValueError("неполные UI-метаданные навыка")
            if (
                not 25 <= len(ui["short_description"]) <= 64
                or f"${name}" not in ui["default_prompt"]
            ):
                raise ValueError(
                    "неверная длина short_description или нет $имени в default_prompt"
                )
        except (OSError, ValueError) as exc:
            error(path, str(exc))
    if not skills:
        error(root / ".agents/skills", "нет проектных навыков")

    docs = [
        root / "AGENTS.md",
        root / "README.md",
        *sorted((root / "docs").glob("*.md")),
        *skills,
    ]
    for path in docs:
        if not path.is_file():
            continue
        text = re.sub(
            r"(?ms)^```[^\n]*\n.*?^```[ \t]*$", "", path.read_text(encoding="utf-8")
        )
        for target in re.findall(r"\[[^\]\n]*\]\(([^)\n]+)\)", text):
            parsed = urlsplit(target.strip("<>"))
            if parsed.scheme or parsed.netloc or not parsed.path:
                continue
            dest = (path.parent / unquote(parsed.path)).resolve()
            if not dest.is_relative_to(root) or not dest.exists():
                error(path, f"нерабочая или выходящая из проекта ссылка: {target}")

    for message in errors:
        print(f"FAIL {message}")
    if errors:
        print(f"Обвязка: ошибок {len(errors)}")
        return 1, len(errors)
    print(
        f"OK Обвязка: агентов {len(profiles)}, навыков {len(skills)}, "
        "локальные ссылки целы"
    )
    return 0, 0


def check_tools() -> int:
    checks = (
        ("Python", [sys.executable, "--version"], True),
        ("Git", ["git", "--version"], True),
        ("ripgrep", ["rg", "--version"], True),
        ("Codex", ["codex", "--version"], True),
        ("Docker CLI", ["docker", "--version"], True),
        ("Compose", ["docker", "compose", "version"], True),
        ("Docker Engine", ["docker", "info", "--format", "{{.ServerVersion}}"], True),
        ("uv (опционально на хосте)", ["uv", "--version"], False),
        ("FFmpeg (опционально на хосте)", ["ffmpeg", "-version"], False),
        ("ffprobe (опционально на хосте)", ["ffprobe", "-version"], False),
    )
    missing = 0
    for label, command, required in checks:
        try:
            if shutil.which(command[0]) is None:
                raise OSError("не найден; ничего не устанавливалось")
            result = subprocess.run(
                command, capture_output=True, text=True, timeout=8, check=False
            )
            lines = (result.stdout or result.stderr).strip().splitlines()
            detail = lines[0][:180] if lines else f"код {result.returncode}"
            if result.returncode:
                raise OSError(detail)
            print(f"OK {label}: {detail}")
        except (OSError, subprocess.TimeoutExpired) as exc:
            missing += int(required)
            print(f"WARN {label}: {exc}")
    return missing


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root", type=Path, default=Path(__file__).resolve().parents[1]
    )
    parser.add_argument(
        "--tools",
        action="store_true",
        help="показать доступность инструментов без установки",
    )
    parser.add_argument(
        "--strict-tools",
        action="store_true",
        help="ненулевой выход при отсутствии обязательного инструмента",
    )
    args = parser.parse_args()
    status, _ = check_setup(args.root.resolve())
    missing = check_tools() if args.tools or args.strict_tools else 0
    return status or (2 if args.strict_tools and missing else 0)


if __name__ == "__main__":
    raise SystemExit(main())
