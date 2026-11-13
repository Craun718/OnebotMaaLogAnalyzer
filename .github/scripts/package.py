#!/usr/bin/env python3
"""Build the OneBot and AstrBot distribution archives."""

import argparse
from pathlib import Path
import shutil
import sys
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[2]
DIST = ROOT / "dist"
FORBIDDEN_DIRS = {"debug", "tests", "__pycache__", ".github", "astrbot_plugin"}
FORBIDDEN_FILES = {"config.json", "config.local.json", ".gitignore", ".env"}
BARE_REQUIRED = (
    "main.py", "requirements.txt", "README.md", "LICENSE",
    "maa_analyzer/__init__.py", "maa_analyzer/runtime.py", "maa_analyzer/platform.py",
    "maa_analyzer/core/__init__.py", "maa_analyzer/features/__init__.py",
    "maa_analyzer/features/maa/__init__.py", "maa_analyzer/platforms/onebot/__init__.py",
    "maa_analyzer/platforms/astrbot/__init__.py", "maa_analyzer/astrbot/plugin_runtime.py",
    "templates/default/page.html",
)
ASTRBOT_REQUIRED = (
    "main.py", "README.md", "metadata.yaml", "_conf_schema.json", "requirements.txt",
    "maa_analyzer/__init__.py", "maa_analyzer/runtime.py", "maa_analyzer/core/__init__.py",
    "maa_analyzer/features/maa/__init__.py", "maa_analyzer/platforms/astrbot/adapter.py",
    "maa_analyzer/astrbot/plugin_runtime.py", "templates/default/page.html",
)


def clean(package: Path) -> None:
    for path in sorted(package.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if path.is_dir() and path.name in {"__pycache__", "tests"}:
            shutil.rmtree(path)
        elif path.is_file() and (path.suffix in {".pyc", ".pyo", ".pyd", ".log"}
                                 or path.match("test_*.py")):
            path.unlink()


def check(package: Path, label: str, required: tuple[str, ...]) -> None:
    for path in package.rglob("*"):
        if ((path.is_dir() and path.name in FORBIDDEN_DIRS)
                or (path.is_file() and (path.name in FORBIDDEN_FILES
                    or path.suffix in {".pyc", ".pyo", ".pyd", ".log"}
                    or path.match("test_*.py")))):
            raise ValueError(f"{label}中出现禁止的文件：{path}")
    for name in ("bot", "core", "features"):
        if (package / name).exists():
            raise ValueError(f"{label}中出现旧顶层目录：{name}")
    for name in required:
        if not (package / name).exists():
            raise ValueError(f"{label}缺少必需文件：{name}")


def archive(package: Path, output: Path) -> None:
    print(f"{package.name} 内容：")
    with ZipFile(output, "w", compression=ZIP_DEFLATED) as zip_file:
        for path in sorted(package.rglob("*")):
            if path.is_file():
                print(path.relative_to(ROOT))
                zip_file.write(path, path.relative_to(package.parent))
    print(f"已生成 {output.name} ({output.stat().st_size} bytes)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version", help="压缩包版本名")
    version = parser.parse_args().version
    if version in {"", ".", ".."} or "/" in version or "\\" in version:
        parser.error("版本名必须是文件名，不能包含路径分隔符")

    full = DIST / "OnebotMaaLogAnalyzer"
    light = DIST / "OnebotMaaLogAnalyzer-light"
    astrbot_stage = DIST / "astrbot"
    astrbot = astrbot_stage / "OnebotMaaLogAnalyzer"
    for path in (full, light, astrbot_stage):
        shutil.rmtree(path, ignore_errors=True)
    full.mkdir(parents=True)
    for name in ("main.py", "requirements.txt", "README.md", "LICENSE"):
        shutil.copy2(ROOT / name, full / name)
    for name in ("docs", "maa_analyzer", "templates", "tools"):
        shutil.copytree(ROOT / name, full / name)
    clean(full)
    check(full, "普通包", BARE_REQUIRED + ("tools/templates/editor.py", "tools/config/editor.py"))
    for name in ("tools/templates/editor.py", "tools/config/editor.py"):
        if not (full / name).is_file():
            raise ValueError(f"普通包缺少工具：{name}")

    shutil.copytree(full, light)
    shutil.rmtree(light / "tools")
    for path in (light / "templates").iterdir():
        if path.name != "default":
            shutil.rmtree(path) if path.is_dir() else path.unlink()
    check(light, "精简包", BARE_REQUIRED)
    if (light / "tools").exists() or any(p.name != "default" for p in (light / "templates").iterdir()):
        raise ValueError("精简包不应包含 tools/ 或额外的模板")

    shutil.copytree(ROOT / "astrbot_plugin", astrbot)
    for name in ("maa_analyzer", "templates"):
        shutil.copytree(ROOT / name, astrbot / name)
    clean(astrbot)
    check(astrbot, "AstrBot 插件包", ASTRBOT_REQUIRED)
    for package, suffix in ((full, ""), (light, "-light"), (astrbot, "-astrbot")):
        archive(package, ROOT / f"OnebotMaaLogAnalyzer-{version}{suffix}.zip")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError) as exc:
        print(f"::error::{exc}", file=sys.stderr)
        sys.exit(1)
