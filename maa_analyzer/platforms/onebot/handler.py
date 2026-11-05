"""
事件处理器：将 OneBot v11 事件桥接到 MaaService。

  消息事件 → handle_message（文件段 / 文本消息）
  通知事件 → handle_notice（群文件上传）
  请求事件 → handle_request（暂不处理）
"""
from __future__ import annotations

import logging

from maa_analyzer.core.service import MaaService
from maa_analyzer.platforms.onebot.adapter import OneBotPlatformAdapter

logger = logging.getLogger("Maa.Handler")


class EventHandler:
    """OneBot v11 事件 → 业务逻辑的桥梁。"""

    def __init__(self, adapter: OneBotPlatformAdapter, service: MaaService):
        self.adapter = adapter
        self.service = service

    async def on_message(self, event: dict) -> bool:
        try:
            return await self.service.handle_message(
                self.adapter.map_message(event)
            )
        except Exception:
            logger.exception("消息处理异常")
            return False

    async def on_notice(self, event: dict) -> bool:
        if event.get("notice_type") != "group_upload":
            return False
        try:
            return await self.service.handle_notice(
                self.adapter.map_notice(event)
            )
        except Exception:
            logger.exception("通知处理异常")
            return False

    async def on_request(self, event: dict) -> bool:
        try:
            return await self.service.handle_request(event)
        except Exception:
            logger.exception("请求处理异常")
            return False
