"""Guards for the os error 448 ("untrusted mount point") install fix.

Root cause seen in the field (uv 0.11.29): uv auto-downloaded managed Python
3.14 into ``%APPDATA%\\uv`` (Roaming), which on OneDrive / folder-redirected
machines sits behind a reparse point; uv then failed to query the interpreter
with ``os error 448`` (无法遍历该路径，因为它包含不受信任的装入点).

The installer must therefore, without any extra user action:

A. Pin the interpreter to a stable version (3.12) instead of letting uv grab
   the newest (3.14) — which also has no xtquant wheel.
B. Route uv's python / cache / tool dirs off Roaming to a safe local home
   (LocalAppData, then a system-drive fallback), avoiding reparse points.
C. Recognise the 448 / untrusted-mount failure and retry on the system drive,
   and give targeted (not "check your network") guidance if it still fails.
"""

from __future__ import annotations

import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_INSTALL_PS1 = _ROOT / "install.ps1"


class PythonPinTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ps1 = _INSTALL_PS1.read_text(encoding="utf-8")

    def test_pins_python_312(self) -> None:
        self.assertIn('$script:DoyoutradePythonVersion = "3.12"', self.ps1)

    def test_tool_install_passes_pinned_python(self) -> None:
        # The tool install must forward --python so uv does not resolve 3.14.
        self.assertIn('"--python", $script:DoyoutradePythonVersion', self.ps1)

    def test_preprovisions_pinned_interpreter(self) -> None:
        self.assertIn('"python", "install", $script:DoyoutradePythonVersion', self.ps1)


class SafeUvHomeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ps1 = _INSTALL_PS1.read_text(encoding="utf-8")

    def test_relocates_uv_dirs_off_roaming(self) -> None:
        for var in ("UV_PYTHON_INSTALL_DIR", "UV_TOOL_DIR", "UV_CACHE_DIR"):
            self.assertIn(f"$env:{var}", self.ps1, f"{var} must be routed to a safe home")

    def test_prefers_localappdata_then_system_drive(self) -> None:
        self.assertIn("LOCALAPPDATA", self.ps1)
        self.assertIn("SystemDrive", self.ps1)
        self.assertIn("Resolve-SafeUvHome", self.ps1)

    def test_avoids_reparse_points(self) -> None:
        self.assertIn("Test-PathBehindReparsePoint", self.ps1)
        self.assertIn("ReparsePoint", self.ps1)

    def test_persists_env_to_user_scope(self) -> None:
        # Future `uv tool upgrade` / `uv tool list` must resolve the same dirs.
        self.assertIn(
            'SetEnvironmentVariable("UV_TOOL_DIR"', self.ps1
        )


class UntrustedMountRetryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ps1 = _INSTALL_PS1.read_text(encoding="utf-8")

    def test_detects_os_error_448(self) -> None:
        self.assertIn("Test-IsUntrustedMountError", self.ps1)
        self.assertIn("os error 448", self.ps1)
        self.assertIn("不受信任的装入点", self.ps1)

    def test_retries_on_system_drive(self) -> None:
        self.assertIn("Get-SystemDriveUvHome", self.ps1)
        # There must be a guarded retry path that switches home and re-installs.
        self.assertRegex(self.ps1, r"Test-IsUntrustedMountError[\s\S]{0,400}Set-UvRuntimeEnv")

    def test_targeted_guidance_not_only_network(self) -> None:
        # On a confirmed 448, the failure message must not blame the network.
        self.assertIn("不是网络问题", self.ps1)
        self.assertIn("OneDrive", self.ps1)


if __name__ == "__main__":
    unittest.main()
