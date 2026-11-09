"""Regression coverage for the public profile card's data boundary and rendering."""

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location("generate_cards", ROOT / "scripts/generate_cards.py")
cards = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cards)


class CardTests(unittest.TestCase):
    def test_fetch_repos_only_keeps_explicitly_public_repositories(self):
        def response(nodes):
            return {"data": {"viewer": {"repositories": {"nodes": nodes}}}}

        repos = [
            {"name": "public", "isPrivate": False},
            {"name": "private", "isPrivate": True},
            {"name": "unknown"},
        ]
        with patch.object(cards, "gh_graphql", side_effect=[response(repos), {}]):
            result = cards.fetch_repos()
        self.assertEqual([r["name"] for r in result["data"]["viewer"]["repositories"]["nodes"]], ["public"])

    def test_mock_render_uses_public_weekly_count_without_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mock = root / "mock.json"
            mock.write_text(json.dumps({
                "repos": {"data": {"viewer": {"repositories": {"nodes": []}}}},
                "username": "example", "public_weekly_commits": 7,
            }))
            with patch.object(cards, "ROOT", root), patch.object(cards, "MOCK_DATA_PATH", mock), \
                 patch.object(cards, "gh_graphql", side_effect=AssertionError("unexpected network")), \
                 patch.object(sys, "argv", ["generate_cards.py", "--mock"]):
                cards.main()
            svg = (root / "card.svg").read_text()
            ElementTree.fromstring(svg)
            self.assertIn("7 commits / 7d", svg)
            self.assertIn("7 public-repository commits", svg)


if __name__ == "__main__":
    unittest.main()
