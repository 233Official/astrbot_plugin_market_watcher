from __future__ import annotations

import json
import unittest

from market_watcher.models import RAW_EXCERPT_MAX_BYTES, SourceKind
from market_watcher.normalize import (
    bounded_excerpt,
    extract_json_code_block,
    fallback_canonical_id,
    normalize_github_repo,
    sanitize_text,
)


class NormalizeTests(unittest.TestCase):
    def test_normalizes_web_ssh_and_api_repository_urls(self) -> None:
        expected = (
            "github:owner/astrbot_plugin_demo",
            "https://github.com/owner/astrbot_plugin_demo",
        )
        variants = [
            "https://GitHub.com/Owner/AstrBot_Plugin_Demo.git?x=1#readme",
            "http://github.com/OWNER/ASTRBOT_PLUGIN_DEMO/",
            "github.com/Owner/AstrBot_Plugin_Demo",
            "git@github.com:Owner/AstrBot_Plugin_Demo.git",
            "ssh://git@github.com/Owner/AstrBot_Plugin_Demo.git",
            "https://api.github.com/repos/Owner/AstrBot_Plugin_Demo",
        ]
        for value in variants:
            with self.subTest(value=value):
                self.assertEqual(normalize_github_repo(value), expected)

    def test_rejects_dots_userinfo_and_non_repository_paths(self) -> None:
        invalid = [
            "https://github.com/./repo",
            "https://github.com/owner/..",
            "https://user:password@github.com/owner/repo",
            "https://api.github.com/repos/owner/repo/issues",
            "https://api.github.com/users/owner",
            "https://github.com/owner/repo/issues",
            "https://gitlab.com/owner/repo",
            "not a url",
        ]
        for value in invalid:
            with self.subTest(value=value):
                self.assertIsNone(normalize_github_repo(value))

    def test_fallback_hash_prevents_lossy_replacement_collisions(self) -> None:
        first = fallback_canonical_id(SourceKind.COLLECTION_ISSUE, "a/b")
        second = fallback_canonical_id(SourceKind.COLLECTION_ISSUE, "a b")
        self.assertNotEqual(first, second)
        self.assertEqual(
            first,
            fallback_canonical_id(SourceKind.COLLECTION_ISSUE, "a/b"),
        )
        self.assertIn(":sha256:", first)

    def test_extracts_json_and_sanitizes_controls(self) -> None:
        body = 'before\n```json\n{"repo":"https://github.com/a/b"}\n```\nafter'
        self.assertEqual(
            extract_json_code_block(body), {"repo": "https://github.com/a/b"}
        )
        self.assertEqual(sanitize_text("a\x00b", 10), "ab")

    def test_bounded_excerpt_never_exceeds_8_kib(self) -> None:
        excerpt = bounded_excerpt({"a": "x" * 9000, "b": "small"})
        encoded = json.dumps(
            excerpt, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
        self.assertLessEqual(len(encoded), RAW_EXCERPT_MAX_BYTES)
        self.assertEqual(excerpt, {"b": "small"})


if __name__ == "__main__":
    unittest.main()
