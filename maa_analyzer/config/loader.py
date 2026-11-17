"""Configuration loading, validation, and effective runtime resolution."""
from __future__ import annotations

import copy
import shutil
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from ..core.llm_pool import detect_legacy_llm, validate_llm_config
from .constants import ONEBOT_MODES
from .defaults import write_default_config
from .logging_setup import logger
from .migration import _write_json_atomic, load_and_migrate_config
from .panel import apply_panel_overrides
from .panel_schema import panel_changed, panel_value


def load_config_file(path: str | Path) -> dict:
    """Load a JSON object, migrating older formats when needed."""
    return load_and_migrate_config(path)


def validate_common_config(cfg: Mapping[str, Any]) -> list[str]:
    """Validate transport-neutral fields shared by both entry points."""
    errors: list[str] = []
    bot_cfg = cfg.get("bot")
    if not isinstance(bot_cfg, dict):
        errors.append("bot 段必须是对象。")
        bot_cfg = {}
    if not str(bot_cfg.get("data_dir", "data") or "").strip():
        errors.append("bot.data_dir 不能为空。")
    configs = bot_cfg.get("configs")
    if configs is None:
        configs = {}
    if not isinstance(configs, dict):
        errors.append("bot.configs 必须是对象。")
        configs = {}
    legacy = detect_legacy_llm(cfg.get("llm") or {})
    if legacy:
        errors.append(legacy)
    else:
        errors.extend(validate_llm_config(cfg.get("llm") or {}, configs))
    return errors


def validate_onebot_config(cfg: Mapping[str, Any]) -> str:
    """Validate a bare OneBot connection block and return its mode."""
    ob = cfg.get("onebot") or {}
    if not isinstance(ob, dict):
        raise ValueError("onebot 段必须是对象。")
    mode = str(ob.get("mode", "http_ws"))
    if mode not in ONEBOT_MODES:
        raise ValueError(
            f"未知模式: {mode}，可选: {', '.join(ONEBOT_MODES)}"
        )

    def check_url(key: str, schemes: tuple[str, ...], required: bool) -> str:
        raw = str(ob.get(key, "") or "").strip()
        if not raw:
            if required:
                raise ValueError(f"onebot.mode={mode} 需要设置 onebot.{key}")
            return ""
        parts = urlsplit(raw)
        if parts.scheme not in schemes or not parts.hostname:
            raise ValueError(
                f"onebot.{key} 不是合法的 {schemes[0]} 地址: {raw!r}"
            )
        try:
            parts.port
        except ValueError as exc:
            raise ValueError(f"onebot.{key} 端口非法: {raw!r}") from exc
        return raw

    if mode in ("http_ws", "http_ws_reverse"):
        check_url("http_url", ("http", "https"), True)
    if mode in ("ws", "http_ws"):
        check_url("ws_url", ("ws", "wss"), True)
    if mode in ("ws_reverse", "http_ws_reverse"):
        try:
            port = int(ob.get("ws_reverse_port", 0) or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError("onebot.ws_reverse_port 必须是整数") from exc
        if not 1 <= port <= 65535:
            raise ValueError(
                f"onebot.mode={mode} 需要 1-65535 的 ws_reverse_port，"
                f"当前为 {port}"
            )
        if not str(ob.get("ws_reverse_host", "127.0.0.1") or "").strip():
            raise ValueError("onebot.ws_reverse_host 不能为空")
    return mode


def resolve_data_dir(
    cfg: Mapping[str, Any],
    *,
    base_dir: str | Path | None = None,
    override: str | Path | None = None,
) -> Path:
    """Resolve the configured data directory against an optional base."""
    bot_cfg = cfg.get("bot") or {}
    raw = override or bot_cfg.get("data_dir", "data")
    path = Path(str(raw or "data")).expanduser()
    if not path.is_absolute():
        path = Path(base_dir or ".") / path
    return path.resolve()


def load_effective_config(
    panel: Mapping[str, Any] | None,
    *,
    plugin_data_dir: str | Path,
) -> tuple[dict, Path, Path]:
    """Load advanced JSON, apply panel overrides, and resolve runtime paths."""
    panel = panel or {}
    plugin_dir = Path(plugin_data_dir).resolve()
    configured_path = (
        str(panel_value(panel, "config_file") or "").strip() or "config.json"
    )
    config_path = Path(configured_path).expanduser()
    if not config_path.is_absolute():
        config_path = plugin_dir / config_path
    config_path = config_path.resolve()
    created = not config_path.exists()
    write_default_config(config_path)
    raw = load_config_file(config_path)
    effective = apply_panel_overrides(raw, panel)
    errors = validate_common_config(effective)
    if errors:
        raise ValueError("\n".join(errors))
    if panel.get("_persist_llm_migration"):
        _persist_migrated_llm(config_path, raw, effective, panel, created=created)
    data_dir = resolve_data_dir(
        effective,
        base_dir=plugin_dir,
        override=(
            str(panel_value(panel, "data_dir")).strip()
            if panel_changed(panel, "data_dir")
            else None
        ),
    )
    if created:
        logger.info("已生成 AstrBot 插件默认配置：%s", config_path)
    return effective, config_path, data_dir


def _persist_migrated_llm(
    path: Path, raw: dict, effective: dict, panel: Mapping[str, Any], *, created: bool,
) -> None:
    """Persist only the migrated profile candidates and referenced endpoints."""
    if not panel.get("llm_models"):
        raise ValueError("迁移尚未完成，请先在面板填写候选模型。")
    updated = copy.deepcopy(raw)
    effective_configs = effective["bot"]["configs"]
    target = str(panel.get("default_config") or "").strip() or next(iter(effective_configs))
    models = effective_configs[target]["llm"]["models"]
    updated_configs = updated.setdefault("bot", {}).setdefault("configs", {})
    updated_configs.setdefault(target, {}).setdefault("llm", {})["models"] = copy.deepcopy(models)
    names = {item["name"] for item in models}
    providers = updated.setdefault("llm", {}).setdefault("providers", [])
    for provider in effective["llm"]["providers"]:
        if provider.get("name") not in names:
            continue
        index = next((i for i, old in enumerate(providers)
                      if isinstance(old, dict) and old.get("name") == provider["name"]), None)
        if index is None:
            providers.append(copy.deepcopy(provider))
        else:
            providers[index] = copy.deepcopy(provider)
    if updated == raw:
        return
    backup = path.with_name(f"{path.name}.panel-migration.bak")
    if not created and not backup.exists():
        shutil.copy2(path, backup)
    _write_json_atomic(path, updated)
    logger.info("[配置] 已将迁移后的模型候选写入：%s", path)
