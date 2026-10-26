"""Run with: python -m unittest discover -s tests -v."""
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace

from core.models import RepoConfig
from features.maa.attachments import normalize_rel_path, parse_attachments
from features.maa.log_digest import read_zip_member
from features.maa.log_tools import LogArchiveTools
from features.maa.repo import RepoProvider
from features.maa.repo_tools import RepoTools
from features.maa.safety import normalize_tool_path, resolve_repo_path


INVALID_PATHS = (
    "../outside.txt", "sub/../../outside.txt", r"..\outside.txt",
    "sub/../normal.txt", r"sub\..\normal.txt", "sub/..",
    "/absolute.txt", r"\absolute.txt", r"C:\outside.txt", "C:outside.txt",
    "./C:/outside.txt", r"\\server\share\file.txt", "bad\x00name",
)


class NormalizeToolPathTests(unittest.TestCase):
    def test_rejects_absolute_and_parent_paths(self):
        for path in INVALID_PATHS:
            with self.subTest(path=path):
                self.assertIsNone(normalize_tool_path(path))

    def test_preserves_relative_paths(self):
        for path, expected in (
            ("", ""), (".", ""), ("./", ""), ("././debug/", "debug"),
            (r"debug\maafw.log", "debug/maafw.log"),
            ("配置//./日志.txt", "配置/日志.txt"),
            ("sub/..normal.txt", "sub/..normal.txt"),
        ):
            with self.subTest(path=path):
                self.assertEqual(normalize_tool_path(path), expected)


    def test_attachment_paths_reject_parent_components_for_both_sources(self):
        for path in INVALID_PATHS:
            with self.subTest(path=path):
                self.assertIsNone(normalize_rel_path(path))
                for source in ("项目", "日志"):
                    self.assertEqual(parse_attachments(f"[附件@{source}: {path}]"), [])


class RepoToolPathTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.root = base / "repo"
        self.root.mkdir()
        (base / "outside.txt").write_text("OUTSIDE_MARKER\n")
        (self.root / "normal.txt").write_text("NORMAL_MARKER\n")
        (self.root / "linked.txt").symlink_to(base / "outside.txt")
        (self.root / "internal.txt").symlink_to(self.root / "normal.txt")
        (self.root / "external_dir").symlink_to(base, target_is_directory=True)
        (self.root / "secrets.json").write_text("SECRET_MARKER\n")
        (self.root / "alias.txt").symlink_to(self.root / "secrets.json")
        (self.root / ".env").write_text("SECRET_MARKER\n")
        (self.root / ".aws").mkdir()
        (self.root / ".aws" / "settings.txt").write_text("SECRET_MARKER\n")
        # The requested name and resolved target must both pass the policy.
        (self.root / "credentials.txt").symlink_to(self.root / "normal.txt")
        self.tools = RepoTools(SimpleNamespace(root=self.root), RepoConfig(extensions=[]))

    async def test_rejects_invalid_arguments_for_all_path_tools(self):
        for name in ("read_file", "list_dir", "search_repo"):
            for path in INVALID_PATHS:
                with self.subTest(tool=name, path=path):
                    result = await self.tools.execute(name, {"path": path, "pattern": "."})
                    self.assertTrue("路径非法" in result or "已拒绝" in result, result)

    async def test_read_rejects_external_and_sensitive_symlinks(self):
        for path in ("linked.txt", "external_dir/outside.txt", "alias.txt", "credentials.txt"):
            with self.subTest(path=path):
                result = await self.tools.execute("read_file", {"path": path})
                self.assertIn("拒绝", result)
                self.assertNotIn("MARKER", result)

    async def test_search_does_not_leak_external_or_sensitive_content(self):
        result = await self.tools.execute("search_repo", {"pattern": "MARKER"})
        self.assertIn("NORMAL_MARKER", result)
        self.assertNotIn("OUTSIDE_MARKER", result)
        self.assertNotIn("SECRET_MARKER", result)
        self.assertNotIn("credentials.txt", result)

    async def test_listing_hides_rejected_entries(self):
        result = await self.tools.execute("list_dir", {})
        self.assertIn("normal.txt", result)
        self.assertIn("internal.txt", result)
        for name in ("linked.txt", "external_dir", "secrets.json", "alias.txt", ".env", ".aws", "credentials.txt"):
            with self.subTest(name=name):
                self.assertNotIn(name, result)

    async def test_internal_symlinks_and_nested_reads_still_work(self):
        result = await self.tools.execute("read_file", {"path": "internal.txt"})
        self.assertIn("NORMAL_MARKER", result)
        nested = self.root / "sub"
        nested.mkdir()
        (nested / "ok.txt").write_text("NESTED_MARKER\n")
        result = await self.tools.execute("search_repo", {"path": "sub", "pattern": "NESTED"})
        self.assertIn("sub/ok.txt:1: NESTED_MARKER", result)
        result = await self.tools.execute("list_dir", {"path": "sub"})
        self.assertIn("ok.txt", result)


    async def test_parent_paths_are_rejected_even_with_internal_symlinks(self):
        sub = self.root / "sub"
        (sub / "deep").mkdir(parents=True)
        (sub / "normal.txt").write_text("ACTUAL_TARGET\n")
        (self.root / "dir_link").symlink_to(sub / "deep", target_is_directory=True)
        for path in ("sub/deep/../normal.txt", "dir_link/../normal.txt"):
            with self.subTest(path=path):
                result = await self.tools.execute("read_file", {"path": path})
                self.assertIn("路径非法", result)
                self.assertNotIn("ACTUAL_TARGET", result)
                self.assertNotIn("NORMAL_MARKER", result)
        path, error = resolve_repo_path(self.root, "external_dir/../normal.txt")
        self.assertIsNone(path)
        self.assertIn("路径非法", error)

    async def test_prefix_sibling_and_symlink_loop_are_rejected(self):
        sibling = self.root.with_name("repo-backup")
        sibling.mkdir()
        (sibling / "normal.txt").write_text("OUTSIDE_MARKER")
        (self.root / "sibling").symlink_to(sibling, target_is_directory=True)
        (self.root / "loop").symlink_to("loop")
        for path in ("sibling/normal.txt", "loop"):
            with self.subTest(path=path):
                resolved, error = resolve_repo_path(self.root, path)
                self.assertIsNone(resolved)
                self.assertIn("拒绝", error)

    async def test_provider_attachments_and_references_share_policy(self):
        provider = RepoProvider(RepoConfig(path=str(self.root), extensions=[]), self.root.parent)
        paths = ["internal.txt", "linked.txt", "alias.txt", "credentials.txt", *INVALID_PATHS]
        self.assertEqual(provider.find_files(paths), [("internal.txt", b"NORMAL_MARKER\n")])
        for path in ("linked.txt", "alias.txt", "credentials.txt", "../outside.txt"):
            self.assertIsNone(provider._read_line(self.root, path, 1))
        self.assertEqual(provider._read_line(self.root, "normal.txt", 1), "NORMAL_MARKER")
        result = provider._scan(self.root, ["MARKER"])
        self.assertIn("NORMAL_MARKER", result)
        self.assertNotIn("OUTSIDE_MARKER", result)
        self.assertNotIn("SECRET_MARKER", result)

    async def test_provider_rejects_internal_parent_paths(self):
        sub = self.root / "sub"
        sub.mkdir()
        provider = RepoProvider(RepoConfig(path=str(self.root)), self.root.parent)
        self.assertEqual(provider.find_files(["sub/../normal.txt"]), [])
        self.assertIsNone(provider._read_line(self.root, "sub/../normal.txt", 1))


class LogToolPathTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.zip_path = Path(self.temp.name) / "logs.zip"
        with zipfile.ZipFile(self.zip_path, "w") as archive:
            archive.writestr("./debug/maafw.log", "NORMAL_MARKER\n")
            archive.writestr(r"debug\other.txt", "NORMAL_MARKER\n")
            archive.writestr("./images/ok.png", b"image")
            for name in INVALID_PATHS[:-1]:
                archive.writestr(name, "UNSAFE_MARKER\n")
                image_name = name + ("/image.png" if name.endswith("..") else ".png")
                archive.writestr(image_name, b"image")
            archive.writestr(".aws/settings.txt", "SECRET_MARKER\n")
            archive.writestr(".aws/hidden.png", b"image")
        self.tools = LogArchiveTools(str(self.zip_path))
        self.addCleanup(self.tools.close)

    async def test_rejects_invalid_arguments(self):
        for path in INVALID_PATHS:
            for operation in (
                lambda: self.tools.read_file(path),
                lambda: self.tools.list_files(path),
                lambda: self.tools.search("MARKER", path),
            ):
                with self.subTest(path=path):
                    self.assertIn("路径非法", await operation())

    async def test_search_ignores_unsafe_and_sensitive_members(self):
        result = await self.tools.search("MARKER")
        self.assertIn("NORMAL_MARKER", result)
        self.assertNotIn("UNSAFE_MARKER", result)
        self.assertNotIn("SECRET_MARKER", result)

    async def test_listing_ignores_unsafe_and_sensitive_members(self):
        result = await self.tools.list_files()
        self.assertIn("debug/", result)
        for name in ("..", "absolute", "C:", "server", ".aws"):
            with self.subTest(name=name):
                self.assertNotIn(name, result)

    async def test_image_listing_ignores_unsafe_and_sensitive_members(self):
        result = await self.tools.list_images()
        self.assertIn("images/ok.png", result)
        self.assertEqual(result.count("字节"), 1)

    async def test_reads_normalized_member_names(self):
        for path in ("debug/maafw.log", "./debug/maafw.log", r"debug\other.txt"):
            with self.subTest(path=path):
                self.assertIn("NORMAL_MARKER", await self.tools.read_file(path))


    async def test_internal_parent_paths_are_rejected_for_zip_reads(self):
        result = await self.tools.read_file("debug/nested/../maafw.log")
        self.assertIn("路径非法", result)
        self.assertIsNone(read_zip_member(str(self.zip_path), "debug/nested/../maafw.log"))

    async def test_direct_zip_reads_enforce_the_same_policy(self):
        for path in (*INVALID_PATHS, ".aws/settings.txt"):
            with self.subTest(path=path):
                self.assertIsNone(read_zip_member(str(self.zip_path), path))


if __name__ == "__main__":
    unittest.main()
