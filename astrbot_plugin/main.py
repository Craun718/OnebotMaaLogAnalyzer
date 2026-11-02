"""AstrBot AIOCQHTTP plugin shell for the shared Maa log analyzer."""
from __future__ import annotations

import logging
import sys
import weakref
from pathlib import Path
from typing import Any

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools

logger = logging.getLogger("Maa.AstrBot")
PLUGIN_NAME = "OnebotMaaLogAnalyzer"


def _locate_kernel_root() -> Path | None:
    """Find a source checkout or packaged plugin root with ``maa_analyzer``."""
    here = Path(__file__).resolve().parent
    for candidate in (here, here.parent):
        if (candidate / "maa_analyzer" / "__init__.py").is_file():
            return candidate
    return None


_KERNEL_ROOT = _locate_kernel_root()
_KERNEL_IMPORT_ERROR: BaseException | None = None
if _KERNEL_ROOT is not None:
    kernel_path = str(_KERNEL_ROOT)
    if kernel_path not in sys.path:
        sys.path.insert(0, kernel_path)

try:
    from maa_analyzer.astrbot.plugin_runtime import AstrBotPluginRuntime
except ImportError as exc:  # Reported from initialize() without breaking discovery.
    AstrBotPluginRuntime = None  # type: ignore[assignment,misc]
    _KERNEL_IMPORT_ERROR = exc


_ACTIVE_RUNTIMES: weakref.WeakSet[Any] = weakref.WeakSet()


class MaaEventFilter(filter.CustomFilter):
    """Wake AstrBot only for events owned by an active analyzer runtime."""

    def filter(self, event: AstrMessageEvent, cfg: Any) -> bool:
        for runtime in tuple(_ACTIVE_RUNTIMES):
            checker = getattr(runtime, "is_relevant_event", None)
            if not callable(checker):
                continue
            try:
                if checker(event):
                    return True
            except Exception:
                logger.exception("判断 OnebotMaaLogAnalyzer 事件相关性失败")
        return False


class Main(Star):
    """Register the shared analyzer as an AstrBot AIOCQHTTP plugin."""

    def __init__(
        self,
        context: Context,
        config: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(context, config)
        self.config = dict(config or {})
        self.plugin_data_dir: Path | None = None
        self.plugin_runtime: Any | None = None

    async def initialize(self) -> None:
        """Start the shared analyzer once for this plugin instance."""
        if self.plugin_runtime is not None:
            await self.plugin_runtime.initialize()
            _ACTIVE_RUNTIMES.add(self.plugin_runtime)
            return
        if _KERNEL_IMPORT_ERROR is not None or AstrBotPluginRuntime is None:
            raise RuntimeError(
                "无法导入 maa_analyzer。请确认插件包内包含 "
                "maa_analyzer/ 目录。"
            ) from _KERNEL_IMPORT_ERROR
        if _KERNEL_ROOT is None:
            raise RuntimeError(
                "无法定位 maa_analyzer。请重新安装 AstrBot 插件包。"
            )

        self.plugin_data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        self.plugin_runtime = AstrBotPluginRuntime(
            self.config,
            plugin_data_dir=self.plugin_data_dir,
            template_root=_KERNEL_ROOT / "templates",
        )
        await self.plugin_runtime.initialize()
        _ACTIVE_RUNTIMES.add(self.plugin_runtime)

    async def terminate(self) -> None:
        """Stop background work and release plugin-owned resources."""
        runtime, self.plugin_runtime = self.plugin_runtime, None
        if runtime is not None:
            _ACTIVE_RUNTIMES.discard(runtime)
            try:
                await runtime.terminate()
            finally:
                _ACTIVE_RUNTIMES.discard(runtime)

    @filter.event_message_type(filter.EventMessageType.ALL)
    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.custom_filter(MaaEventFilter)
    async def on_aiocqhttp_event(self, event: AstrMessageEvent) -> None:
        """Dispatch one AIOCQHTTP event through the shared analyzer."""
        runtime = self.plugin_runtime
        if runtime is None:
            return
        try:
            consumed = await runtime.handle_event(event)
        except Exception:
            logger.exception("处理 AstrBot AIOCQHTTP 事件失败")
            return
        if consumed:
            event.stop_event()
