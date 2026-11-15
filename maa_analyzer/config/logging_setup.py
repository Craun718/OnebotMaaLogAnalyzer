"""Logging setup shared by the standalone and AstrBot entry points."""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from ..core.log_context import ConfigNameFormatter
from .constants import LOG_DATE_FORMAT, LOG_FORMAT

logger = logging.getLogger("Maa")


def _unique_log_file(log_dir: Path) -> Path:
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    path = log_dir / f"{stamp}.log"
    index = 2
    while path.exists():
        path = log_dir / f"{stamp}_{index}.log"
        index += 1
    return path


def setup_logging(
    log_cfg: Mapping[str, Any],
    *,
    isolated: bool = False,
    base_dir: str | Path | None = None,
) -> None:
    """Configure bare-process logging or an isolated plugin logger."""
    level = getattr(
        logging, str(log_cfg.get("log_level", "INFO")).upper(), logging.INFO
    )
    log_to_file = bool(log_cfg.get("log_to_file", True))
    log_dir = Path(str(log_cfg.get("log_dir", "logs"))).expanduser()
    if not log_dir.is_absolute():
        log_dir = Path(base_dir or ".") / log_dir
    target = logging.getLogger("Maa") if isolated else logging.getLogger()

    for handler in list(target.handlers):
        if not isolated or getattr(handler, "_maa_plugin_handler", False):
            target.removeHandler(handler)
            try:
                handler.close()
            except Exception:
                pass
    target.setLevel(level)
    target.propagate = not isolated

    if not isolated or not any(
        getattr(handler, "_maa_astrbot_bridge", False)
        for handler in target.handlers
    ):
        console = logging.StreamHandler()
        console.setLevel(level)
        console.setFormatter(ConfigNameFormatter(LOG_FORMAT, LOG_DATE_FORMAT))
        if isolated:
            setattr(console, "_maa_plugin_handler", True)
        target.addHandler(console)

    if log_to_file:
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            file_handler = logging.FileHandler(
                _unique_log_file(log_dir), encoding="utf-8"
            )
            file_handler.setLevel(level)
            file_handler.setFormatter(
                ConfigNameFormatter(LOG_FORMAT, LOG_DATE_FORMAT)
            )
            if isolated:
                setattr(file_handler, "_maa_plugin_handler", True)
            target.addHandler(file_handler)
            logger.info(
                "日志输出到文件: %s (level=%s)",
                file_handler.baseFilename,
                logging.getLevelName(level),
            )
        except OSError as exc:
            logger.warning("无法创建日志文件：%s", exc)


def teardown_logging(*, isolated: bool = False) -> None:
    """Close handlers installed by :func:`setup_logging`."""
    target = logging.getLogger("Maa") if isolated else logging.getLogger()
    for handler in list(target.handlers):
        if not isolated or getattr(handler, "_maa_plugin_handler", False):
            target.removeHandler(handler)
            try:
                handler.close()
            except Exception:
                pass
