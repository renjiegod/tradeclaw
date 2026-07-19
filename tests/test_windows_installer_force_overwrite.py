"""Guards for orphan shim / Executables already exist install failures.

Field failure (uv 0.10.x, Windows 0.1.5 installer):

    error: Executables already exist: doyoutrade-cli.exe, doyoutrade.exe
    (use `--force` to overwrite)

Root cause: after relocating UV_TOOL_DIR off Roaming, ``uv tool list`` is empty
but leftover shims remain in ``%USERPROFILE%\\.local\\bin``. The installer
treated that as a fresh install and called ``uv tool install`` *without*
``--force``, so uv refused to overwrite the orphans.

Unix ``install.sh`` already always passes ``--force``. Windows must match,
and must surface orphan-shim detection so diagnostics explain the overwrite.
"""

from __future__ import annotations

import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_INSTALL_PS1 = _ROOT / "install.ps1"


class ForceOverwriteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ps1 = _INSTALL_PS1.read_text(encoding="utf-8")

    def test_tool_install_always_passes_force(self) -> None:
        # Must be on the install arg list itself — script -Force alone is not enough.
        self.assertRegex(
            self.ps1,
            r'\$installArgs\s*=\s*@\(\s*"tool",\s*"install",\s*"--force"',
            "uv tool install must always include --force (idempotent / orphan shims)",
        )

    def test_failure_detail_mentions_force(self) -> None:
        self.assertIn("uv tool install --force --python", self.ps1)

    def test_aligns_with_unix_install_sh_force(self) -> None:
        sh = (_ROOT / "install.sh").read_text(encoding="utf-8")
        self.assertIn("uv tool install --force", sh)


class OrphanShimDetectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ps1 = _INSTALL_PS1.read_text(encoding="utf-8")

    def test_has_orphan_shim_helper(self) -> None:
        self.assertIn("Get-OrphanDoYouTradeShims", self.ps1)
        self.assertIn("doyoutrade.exe", self.ps1)
        self.assertIn("doyoutrade-cli.exe", self.ps1)

    def test_install_path_detects_orphans_when_tool_list_empty(self) -> None:
        # When not in uv tool list but shims exist, warn before install.
        self.assertRegex(
            self.ps1,
            r"function\s+Install-DoYouTrade[\s\S]*Get-OrphanDoYouTradeShims",
        )
        self.assertIn("孤儿 shim", self.ps1)
        self.assertIn("--force", self.ps1)

    def test_orphan_check_uses_uv_tool_bin_dir(self) -> None:
        # Orphans live in the same bin uv would write shims to.
        self.assertRegex(
            self.ps1,
            r"function\s+Get-OrphanDoYouTradeShims[\s\S]*Get-UvToolBinDir",
        )


if __name__ == "__main__":
    unittest.main()
