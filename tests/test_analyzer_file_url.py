"""Verify message file URLs reach the analyzer's download paths."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from maa_analyzer.core.archive_names import parse_archive_part
from maa_analyzer.core.models import AnalysisSettings, ProjectConfig
from maa_analyzer.features.maa.analyzer import MaaAnalyzer


class AnalyzerFileUrlTests(unittest.IsolatedAsyncioTestCase):
    async def test_url_only_file_reaches_downloader(self):
        with tempfile.TemporaryDirectory() as directory:
            service = MagicMock()
            service.add_job.return_value = SimpleNamespace()
            analyzer = MaaAnalyzer(service, llm=SimpleNamespace(), history=None)
            analyzer._build_work_dir = MagicMock(return_value=Path(directory))
            analyzer._restore_repo_latest = AsyncMock()
            analyzer._cleanup_finished_fallback = AsyncMock()
            downloader = AsyncMock(side_effect=RuntimeError("download stopped"))
            with patch("maa_analyzer.features.maa.analyzer.download_group_file", downloader):
                outcome = await analyzer.analyze(
                    config_name="default", settings=AnalysisSettings(),
                    project=ProjectConfig(), group_id=1, file_name="logs.zip",
                    file_id=None, file_size=100, file_url="https://example.test/log.zip",
                )
            self.assertFalse(outcome.ok)
            self.assertEqual(downloader.await_args.args[2], "")
            self.assertEqual(downloader.await_args.kwargs["url"], "https://example.test/log.zip")

    async def test_single_part_fallback_uses_event_url(self):
        analyzer = MaaAnalyzer(MagicMock(), llm=SimpleNamespace(), history=None)
        part = parse_archive_part("logs-part01.zip")
        self.assertIsNotNone(part)
        downloader = AsyncMock(return_value=100)
        with tempfile.TemporaryDirectory() as directory, \
             patch("maa_analyzer.features.maa.analyzer.get_all_files", AsyncMock(return_value=[])), \
             patch("maa_analyzer.features.maa.analyzer.download_group_file", downloader):
            await analyzer._download_archive_parts(
                group_id=1, file_name="logs-part01.zip", file_id="", file_size=100,
                file_url="https://example.test/part.zip", part=part,
                zip_path=Path(directory) / "source.zip", work_dir=Path(directory),
                settings=AnalysisSettings(), semaphore=asyncio.Semaphore(1),
            )
        self.assertEqual(downloader.await_args.kwargs["url"], "https://example.test/part.zip")
