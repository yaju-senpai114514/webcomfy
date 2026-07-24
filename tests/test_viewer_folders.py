from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from web import viewer


class ViewerFolderTests(unittest.TestCase):
    def test_lists_only_immediate_children(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / "a" / "nested").mkdir(parents=True)
            (root / "b").mkdir()
            (root / "a" / "direct.webp").write_bytes(b"x")
            (root / "a" / "nested" / "deep.webp").write_bytes(b"x")

            with patch.object(viewer, "OUTPUT_DIR", root):
                top = viewer.api_folders("")
                self.assertEqual([f["dir"] for f in top["folders"]], ["a", "b"])
                self.assertNotIn("a/nested", [f["dir"] for f in top["folders"]])
                self.assertEqual(top["parent"], None)

                child = viewer.api_folders("a")
                self.assertEqual(child["current"], "a")
                self.assertEqual(child["parent"], "")
                self.assertEqual([f["dir"] for f in child["folders"]], ["a/nested"])
                self.assertEqual(child["folders"][0]["count"], 1)


if __name__ == "__main__":
    unittest.main()
