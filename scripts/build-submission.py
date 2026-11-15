#!/usr/bin/env python3
"""Build and verify a submission from committed sources and original AI records.

This is a packaging tool, not a secret scanner. Review and scan the resulting
staging directory/archive separately before handing it to another person.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import stat
import subprocess
import tempfile
import time
import tomllib
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
AUDIT = ROOT / ".artifacts/final-audit"
MANIFEST_PATH = "submission/SHA256SUMS.json"
FORBIDDEN_PARTS = {
    ".artifacts",
    ".venv",
    "__pycache__",
    "node_modules",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "secrets",
    "uploads",
    "storage",
    "var",
}
EVIDENCE_NAMES = (
    "check-final.log",
    "check-packaging.log",
    "doctor.log",
    "integration-final.log",
    "browser.log",
    "agents-check.log",
    "resilience.log",
    "capacity.log",
    "dependency-audit-runtime.json",
    "dependency-audit-dev.json",
    "gitleaks-history.json",
    "gitleaks-history-final.json",
    "gitleaks-all-objects-final.json",
    "gitleaks-all-objects.json",
    "ubuntu-clean-final.log",
    "ubuntu-bootstrap-final.log",
    "ubuntu-reboot.json",
    "ubuntu-repeat.log",
    "ubuntu-reboot.log",
    "ubuntu-deployment-browser.log",
    "git-fsck.log",
    "ubuntu-vm/base.sha256",
    "runtime-probe.py",
    "https-demo-test.sh",
    "https-demo.log",
    "https-demo.json",
)
BROWSER_NAMES = (
    "report.json",
    "account-security.json",
    "errors.json",
    "experience.json",
    "lifecycle-report.json",
    "miniplayer.json",
    "player-progress.json",
    "watch-desktop.png",
    "watch-mobile.png",
    "home-mobile.png",
    "channel-desktop.png",
    "moderation-desktop.png",
    "moderation-mobile.png",
    "admin-desktop.png",
    "admin-mobile.png",
    "private-denied-guest.png",
    "private-denied-outsider.png",
    "private-owner-desktop.png",
    "private-owner-mobile.png",
    "private-viewer-desktop.png",
)


def git(*args: str, cwd: Path = ROOT) -> bytes:
    environment = dict(os.environ, GIT_OPTIONAL_LOCKS="0")
    return subprocess.run(
        ["git", "-c", "core.fsmonitor=false", *args],
        cwd=cwd,
        env=environment,
        check=True,
        capture_output=True,
    ).stdout


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def safe_relative(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if (
        not name
        or "\\" in name
        or "\x00" in name
        or path.is_absolute()
        or any(part in {".", ".."} for part in path.parts)
        or str(path) != name
    ):
        raise ValueError(f"Небезопасный путь в архиве: {name!r}")
    return path


def ordinary_file(path: Path) -> bytes:
    if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode):
        raise ValueError(f"Ожидался обычный файл: {path}")
    return path.read_bytes()


def put(stage: Path, name: str, data: bytes, mode: int = 0o644) -> None:
    target = stage.joinpath(*safe_relative(name).parts)
    if target.exists():
        raise ValueError(f"Повторное имя в поставке: {name}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    target.chmod(mode)


def put_json(stage: Path, name: str, value: Any) -> None:
    put(
        stage,
        name,
        (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode(),
    )


def require_clean() -> None:
    if git("status", "--porcelain", "--untracked-files=no").strip():
        raise ValueError("Сначала закоммитьте изменения tracked-файлов проекта")
    if not (ROOT / ".git").is_dir() or (ROOT / ".git").is_symlink():
        raise ValueError("Запустите упаковку из основного checkout с каталогом .git")
    for name in ("objects/info/alternates", "commondir"):
        if (ROOT / ".git" / name).exists():
            raise ValueError(f"Git зависит от внешнего хранилища: .git/{name}")


def copy_repository(stage: Path) -> dict[str, Any]:
    require_clean()
    before = git("show-ref", "--head")
    tracked: list[str] = []
    for entry in git("ls-files", "--stage", "-z").split(b"\0"):
        if not entry:
            continue
        metadata, raw_name = entry.split(b"\t", 1)
        mode, _object_id, index_stage = metadata.decode().split()
        name = raw_name.decode("utf-8")
        path = safe_relative(name)
        if mode not in {"100644", "100755"} or index_stage != "0":
            raise ValueError(f"Необычный или неслитый объект Git: {name}")
        if (
            any(part in FORBIDDEN_PARTS for part in path.parts)
            or (path.name.startswith(".env") and path.name != ".env.example")
            or path.parts[0] in {".git", "submission"}
        ):
            raise ValueError(f"Недопустимый tracked-файл: {name}")
        put(stage, name, ordinary_file(ROOT / name), int(mode[-3:], 8))
        tracked.append(name)
    for path in sorted((ROOT / ".git").rglob("*")):
        relative = path.relative_to(ROOT)
        if relative.parts[1:2] == ("worktrees",):
            continue
        if path.is_dir() and not path.is_symlink():
            continue
        if path.name.endswith(".lock") or relative.as_posix() in {
            ".git/COMMIT_EDITMSG",
            ".git/FETCH_HEAD",
        }:
            continue
        put(
            stage,
            relative.as_posix(),
            ordinary_file(path),
            stat.S_IMODE(path.stat().st_mode),
        )
    require_clean()
    if before != git("show-ref", "--head"):
        raise ValueError("Git изменился во время копирования; повторите упаковку")
    return {
        "head": git("rev-parse", "HEAD").decode().strip(),
        "refs": git("show-ref", "--head").decode().splitlines(),
        "object_count": git("count-objects", "-v").decode().splitlines(),
        "reachable_commits": int(git("rev-list", "--all", "--count")),
        "commit_epoch": int(git("show", "-s", "--format=%ct", "HEAD")),
        "tracked_files": len(tracked),
        "excluded_git_files": [
            ".git/worktrees/",
            "*.lock",
            ".git/COMMIT_EDITMSG",
            ".git/FETCH_HEAD",
        ],
    }


def original_snapshot(path: Path) -> bytes:
    """Read a whole, stable, valid original; never cut or reconstruct JSONL."""
    for _attempt in range(8):
        before = path.stat()
        data = ordinary_file(path)
        after = path.stat()
        if (
            before.st_size == after.st_size == len(data)
            and before.st_mtime_ns == after.st_mtime_ns
            and data.endswith(b"\n")
        ):
            try:
                for line in data.splitlines():
                    json.loads(line)
            except (ValueError, UnicodeDecodeError):
                pass
            else:
                return data
        time.sleep(0.1)
    raise ValueError(f"Оригинал сейчас дописывается или повреждён: {path.name}")


def copy_materials(stage: Path) -> dict[str, Any]:
    inventory_path = AUDIT / "materials-inventory.json"
    raw_inventory = ordinary_file(inventory_path)
    inventory = json.loads(raw_inventory)
    captured: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    for item in inventory["original_dialogues"]:
        source = Path(item["path"])
        if not item["exists"]:
            if source.exists():
                raise ValueError("Найден ранее отсутствовавший оригинал: обновите учёт")
            missing.append({"id": item["id"], "source": str(source)})
            continue
        if not item.get("copy_allowed_after_final_rescan"):
            raise ValueError(f"Оригинал не прошёл учётную проверку: {item['id']}")
        data = original_snapshot(source)
        if not item["current"] and digest(data) != item["sha256"]:
            raise ValueError(f"Завершённый оригинал изменён: {item['id']}")
        name = f"submission/dialogues/{source.name}"
        put(stage, name, data)
        captured.append(
            {
                "id": item["id"],
                "source": str(source),
                "archive_path": name,
                "size": len(data),
                "sha256": digest(data),
                "incomplete": bool(item["current"]),
                "content": "whole original bytes; no edits or reconstruction",
                "final_secret_scan": "required separately after capture",
            }
        )
    external: list[dict[str, str]] = []
    for item in inventory["original_agent_materials"]:
        if item["scope"] == "project":
            continue
        source = Path(item["path"])
        data = ordinary_file(source)
        if digest(data) != item["sha256"]:
            raise ValueError(f"Внешний навык изменён после учёта: {source.name}")
        # Keep the original hierarchy inside the skill, without host cache paths.
        parts = source.parts
        index = max(i for i, part in enumerate(parts) if part == "caveman")
        name = "submission/agent-materials/external-skills/caveman/" + "/".join(
            parts[index + 1 :]
        )
        put(stage, name, data)
        external.append({"source": str(source), "archive_path": name})
    global_rules = Path.home() / ".codex/AGENTS.md"
    if global_rules.is_file():
        data = ordinary_file(global_rules)
        if data:
            raise ValueError("Глобальные инструкции больше не пусты: нужен новый аудит")
        put(stage, "submission/agent-materials/global-AGENTS.original.md", data)
    put(
        stage,
        "submission/agent-materials/materials-inventory-at-audit.json",
        raw_inventory,
    )
    if (AUDIT / "materials-report.md").is_file():
        put(
            stage,
            "submission/agent-materials/materials-report.md",
            ordinary_file(AUDIT / "materials-report.md"),
        )
    put_json(stage, "submission/dialogues-manifest.json", captured)
    lines = [
        "# Недостающие материалы",
        "",
        f"Оригинальные файлы {len(missing)} диалогов отсутствуют в текущей среде.",
        "Они не пересозданы из SQLite и не заменены пересказами.",
        "Нужно добавить подлинные исходные файлы или оригинальный экспорт среды.",
        "",
    ]
    lines.extend(f"- `{item['id']}` — `{item['source']}`" for item in missing)
    lines.extend(
        [
            "",
            "Текущие беседы сохранены до момента упаковки и отмечены incomplete.",
            "Для полноты после сдачи можно добавить их завершённые оригиналы.",
            "Исторические версии глобальных настроек и внутренние реализации MCP",
            "недоступны как переносимые материалы; полномочные токены не включаются.",
            "Текущая безопасная выборка настроек явно обозначена как производная.",
            "",
        ]
    )
    put(stage, "submission/MISSING-MATERIALS.md", "\n".join(lines).encode())
    return {"captured": len(captured), "missing": len(missing), "external": external}


def derived_settings(stage: Path) -> None:
    source = Path.home() / ".codex/config.toml"
    if not source.is_file():
        put_json(
            stage,
            "submission/agent-materials/global-settings.derived.json",
            {"kind": "derived inventory", "status": "global settings unavailable"},
        )
        return
    raw = ordinary_file(source)
    config = tomllib.loads(raw.decode())
    selected = {
        key: config[key]
        for key in ("model", "model_reasoning_effort", "service_tier", "personality")
        if key in config and isinstance(config[key], (str, bool, int))
    }
    servers = []
    for name, settings in config.get("mcp_servers", {}).items():
        # No command, args, endpoint, headers or environment VALUES are exported.
        servers.append(
            {
                "name": name,
                "enabled": settings.get("enabled", "application default"),
                "env_names": sorted(
                    set(settings.get("env_vars", []))
                    | set(settings.get("env", {}))
                    | (
                        {settings["bearer_token_env_var"]}
                        if settings.get("bearer_token_env_var")
                        else set()
                    )
                ),
            }
        )
    put_json(
        stage,
        "submission/agent-materials/global-settings.derived.json",
        {
            "kind": "derived safe whitelist; not an original global configuration",
            "source": str(source),
            "source_sha256": digest(raw),
            "settings": selected,
            "mcp_inventory": servers,
            "omitted": [
                "unrelated projects and preferences",
                "commands, args, URLs, headers and environment values",
                "credentials, app connection state and historical settings",
            ],
        },
    )


def copy_evidence(stage: Path) -> dict[str, list[str]]:
    copied = []
    absent = []
    for name in EVIDENCE_NAMES:
        source = AUDIT / name
        if source.is_file():
            put(stage, f"submission/evidence/{name}", ordinary_file(source))
            copied.append(name)
        else:
            absent.append(name)
    for category in ("resilience", "capacity"):
        source = ROOT / ".artifacts" / category / "report.json"
        name = f"{category}/report.json"
        if source.is_file():
            put(stage, f"submission/evidence/{name}", ordinary_file(source))
            copied.append(name)
        else:
            absent.append(name)
    deployment = AUDIT / "ubuntu-deployment"
    if deployment.is_dir():
        for source in sorted(deployment.rglob("*")):
            if source.is_file() and source.suffix in {".json", ".png", ".log"}:
                if any(
                    word in source.name.lower()
                    for word in ("failure", "secret", "cookie", "password", ".env")
                ):
                    continue
                name = source.relative_to(AUDIT).as_posix()
                put(stage, f"submission/evidence/{name}", ordinary_file(source))
                copied.append(name)
    browser = ROOT / ".artifacts/video-browser"
    report_path = browser / "report.json"
    report = json.loads(ordinary_file(report_path))
    if report.get("status") != "passed":
        raise ValueError("Последний полный браузерный отчёт не имеет status=passed")
    report_mtime = report_path.stat().st_mtime
    for name in BROWSER_NAMES:
        source = browser / name
        if not source.is_file():
            absent.append(f"video-browser/{name}")
            continue
        age = report_mtime - source.stat().st_mtime
        if not -2 <= age <= 3600:
            absent.append(f"video-browser/{name}: outside current report window")
            continue
        put(stage, f"submission/evidence/video-browser/{name}", ordinary_file(source))
        copied.append(f"video-browser/{name}")
    return {"included": copied, "not_available_or_not_current": absent}


def file_manifest(stage: Path) -> dict[str, dict[str, Any]]:
    for path in stage.rglob("*"):
        if path.is_symlink() or not (
            stat.S_ISDIR(path.stat().st_mode) or stat.S_ISREG(path.stat().st_mode)
        ):
            raise ValueError(f"Необычный файл в staging: {path.relative_to(stage)}")
    return {
        path.relative_to(stage).as_posix(): {
            "sha256": digest(ordinary_file(path)),
            "size": path.stat().st_size,
            "mode": f"{stat.S_IMODE(path.stat().st_mode):04o}",
        }
        for path in sorted(stage.rglob("*"))
        if path.is_file() and path.relative_to(stage).as_posix() != MANIFEST_PATH
    }


def create_zip(stage: Path, output: Path, epoch: int) -> None:
    stamp = dt.datetime.fromtimestamp(max(epoch, 315532800), dt.UTC)
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(stage.rglob("*")):
            if not path.is_file():
                continue
            name = "FoxTube/" + path.relative_to(stage).as_posix()
            safe_relative(name)
            info = zipfile.ZipInfo(name, stamp.timetuple()[:6])
            info.create_system = 3
            info.external_attr = (
                stat.S_IFREG | stat.S_IMODE(path.stat().st_mode)
            ) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, ordinary_file(path))


def verify_zip(output: Path, expected_git: dict[str, Any]) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="foxtube-unzip-") as temporary:
        extracted = Path(temporary)
        with zipfile.ZipFile(output) as archive:
            if archive.testzip() is not None:
                raise ValueError("Ошибка CRC архива")
            seen: set[str] = set()
            for info in archive.infolist():
                safe_relative(info.filename)
                if not info.filename.startswith("FoxTube/") or info.filename in seen:
                    raise ValueError("Неверный корень или дубликат в ZIP")
                seen.add(info.filename)
                mode = info.external_attr >> 16
                if not stat.S_ISREG(mode):
                    raise ValueError("Архив содержит необычный тип файла")
                put(extracted, info.filename, archive.read(info), stat.S_IMODE(mode))
        repository = extracted / "FoxTube"
        expected = json.loads((repository / MANIFEST_PATH).read_text())
        if file_manifest(repository) != expected:
            raise ValueError("Состав/хэши/права после распаковки не совпали")
        fsck = git("fsck", "--full", cwd=repository).decode().splitlines()
        if git("status", "--porcelain", "--untracked-files=no", cwd=repository):
            raise ValueError("Распакованный checkout имеет tracked-изменения")
        status = git("status", "--porcelain", "--untracked-files=all", cwd=repository)
        if any(
            not line.startswith("?? submission/")
            for line in status.decode().splitlines()
        ):
            raise ValueError("Неожиданные untracked-файлы после распаковки")
        refs = git("show-ref", "--head", cwd=repository).decode().splitlines()
        if refs != expected_git["refs"]:
            raise ValueError("Git refs после распаковки отличаются")
        object_count = git("count-objects", "-v", cwd=repository).decode().splitlines()
        count_keys = {"count", "in-pack", "packs", "prune-packable", "garbage"}
        before_counts = {
            key: value
            for key, value in (
                line.split(": ", 1) for line in expected_git["object_count"]
            )
            if key in count_keys
        }
        after_counts = {
            key: value
            for key, value in (line.split(": ", 1) for line in object_count)
            if key in count_keys
        }
        if after_counts != before_counts:
            raise ValueError("Количество Git-объектов после распаковки отличается")
        return {
            "crc": "passed",
            "fresh_extraction_manifest": "passed",
            "git_fsck_full": "passed",
            "git_fsck_output": fsck,
            "tracked_status": "clean",
            "untracked": "only submission/ delivery materials",
            "refs_match": True,
            "object_count_match": True,
            "object_count": object_count,
            "files_in_zip": len(seen),
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    operation = parser.add_mutually_exclusive_group(required=True)
    operation.add_argument("--stage-only", type=Path, metavar="DIRECTORY")
    operation.add_argument("--pack-stage", type=Path, metavar="DIRECTORY")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    if args.stage_only:
        if args.output:
            raise ValueError("--stage-only не создаёт ZIP; не указывайте --output")
        stage = args.stage_only.expanduser().resolve()
        if stage.exists():
            raise ValueError("Каталог staging уже существует; выберите новый путь")
        stage.mkdir(parents=True)
        git_info = copy_repository(stage)
        materials = copy_materials(stage)
        derived_settings(stage)
        evidence = copy_evidence(stage)
        build = {
            "created_at_utc": dt.datetime.now(dt.UTC).isoformat(),
            "git": git_info,
            "materials": materials,
            "evidence": evidence,
            "secret_scan": "NOT performed by packager; external final scan required",
        }
        put_json(stage, "submission/build-report.json", build)
        put_json(stage, MANIFEST_PATH, file_manifest(stage))
        print(
            json.dumps(
                {
                    "stage": str(stage),
                    "status": "staged; final secret scan required BEFORE --pack-stage",
                    "manifest_sha256": digest((stage / MANIFEST_PATH).read_bytes()),
                    "git": git_info,
                    "materials": materials,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if not args.output:
        raise ValueError("Для --pack-stage требуется --output ZIP")
    stage = args.pack_stage.expanduser().resolve()
    if stage.is_symlink() or not stage.is_dir():
        raise ValueError("--pack-stage должен указывать готовый обычный каталог")
    output = args.output.expanduser().resolve()
    if output == stage or stage in output.parents:
        raise ValueError("ZIP должен находиться за пределами staging")
    if output.suffix.lower() != ".zip":
        raise ValueError("--output должен указывать ZIP")
    if output.exists() and not args.force:
        raise ValueError("Архив уже существует; для замены укажите --force")
    expected = json.loads(ordinary_file(stage / MANIFEST_PATH))
    if file_manifest(stage) != expected:
        raise ValueError("Staging изменился после фиксации manifest")
    build = json.loads(ordinary_file(stage / "submission/build-report.json"))
    git_info, materials = build["git"], build["materials"]
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        prefix=".foxtube-submission-", suffix=".zip", dir=output.parent, delete=False
    ) as file:
        temporary_zip = Path(file.name)
    try:
        create_zip(stage, temporary_zip, git_info["commit_epoch"])
        verification = verify_zip(temporary_zip, git_info)
        temporary_zip.replace(output)
    finally:
        temporary_zip.unlink(missing_ok=True)
    report = {
        "archive": str(output),
        "size": output.stat().st_size,
        "sha256": digest(output.read_bytes()),
        "git": git_info,
        "materials": materials,
        "verification": verification,
        "secret_scan": "required separately; not claimed by this report",
    }
    report_path = output.with_suffix(".report.json")
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    output.with_suffix(".zip.sha256").write_text(f"{report['sha256']}  {output.name}\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        # Avoid printing subprocess stdout/stderr or any original record contents.
        print(
            f"Упаковка не завершена: {type(error).__name__}: {error}",
            file=os.sys.stderr,
        )
        raise SystemExit(1) from None
