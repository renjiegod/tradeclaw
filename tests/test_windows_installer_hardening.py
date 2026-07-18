"""Guards for Windows GUI installer hardening (A/B/C).

A. Inno Setup must abort when ``install-win.ps1`` exits non-zero, and must
   not offer "立即启动" unless install succeeded.
B. Launchers must resolve ``doyoutrade.exe`` via uv's real tool bin dir
   (marker file + ``uv tool dir --bin``), not only ``%USERPROFILE%\\.local\\bin``.
C. Missing-command copy must steer users to ``uv tool list`` / reinstall,
   not "重启电脑".
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_ISS = _ROOT / "packaging" / "windows" / "doyoutrade-setup.iss"
_INSTALL_PS1 = _ROOT / "install.ps1"
_BAT_SCRIPT = _ROOT / "scripts" / "write_windows_bats_gbk.py"
_PACKAGING_BAT = _ROOT / "packaging" / "windows" / "launch-doyoutrade.bat"
_ROOT_BAT = _ROOT / "启动DoYouTrade.bat"


def _read_gbk(path: Path) -> str:
    return path.read_bytes().decode("gbk")


class InnoInstallAbortTests(unittest.TestCase):
    """A: fail the wizard when the PowerShell installer returns non-zero."""

    def setUp(self) -> None:
        self.iss = _ISS.read_text(encoding="utf-8")

    def test_iss_has_pascal_install_with_exit_code_check(self) -> None:
        self.assertIn("[Code]", self.iss)
        self.assertRegex(
            self.iss,
            r"CurStepChanged\s*\(",
            "install must run from [Code] so exit codes can abort setup",
        )
        self.assertRegex(
            self.iss,
            r"RaiseException\s*\(",
            "non-zero install must RaiseException so the wizard fails",
        )
        self.assertIn("install-win.ps1", self.iss)

    def test_iss_does_not_silently_run_install_from_run_section(self) -> None:
        """A silent [Run] install ignores exit codes and still finishes 'OK'."""
        run_section = re.search(
            r"\[Run\](.*?)(?:\[UninstallRun\]|\[Code\]|\Z)",
            self.iss,
            flags=re.S | re.I,
        )
        self.assertIsNotNone(run_section, "[Run] section missing")
        # Strip comments so a remark like "install-win.ps1 is invoked from
        # [Code]" does not false-positive as an invocation.
        run_body = "\n".join(
            ln
            for ln in run_section.group(1).splitlines()
            if not ln.lstrip().startswith(";")
        )
        self.assertNotRegex(
            run_body,
            r"install-win\.ps1",
            "install-win.ps1 must not be invoked from [Run] "
            "(exit code is ignored there); invoke it from [Code] instead",
        )

    def test_postinstall_launch_gated_on_success(self) -> None:
        self.assertRegex(
            self.iss,
            r"Check\s*:\s*InstallSucceeded",
            'postinstall "立即启动" must be gated on InstallSucceeded',
        )
        self.assertRegex(
            self.iss,
            r"function\s+InstallSucceeded\s*:",
            "InstallSucceeded Check: helper must be defined",
        )


class ShimResolutionTests(unittest.TestCase):
    """B: record and resolve the real uv tool bin directory."""

    def test_install_ps1_writes_tool_bin_dir_marker(self) -> None:
        text = _INSTALL_PS1.read_text(encoding="utf-8")
        self.assertIn("uv tool dir --bin", text)
        self.assertIn("tool-bin-dir.txt", text)
        self.assertIn(r".doyoutrade", text)

    def test_bat_generator_resolves_marker_and_uv_tool_dir(self) -> None:
        src = _BAT_SCRIPT.read_text(encoding="utf-8")
        # Packaging launcher template (LAUNCH) must include both fallbacks.
        self.assertIn("tool-bin-dir.txt", src)
        self.assertIn("uv tool dir --bin", src)
        self.assertIn("found_marker", src)
        self.assertIn("found_uv_bin", src)

    def test_generated_packaging_launcher_has_fallbacks(self) -> None:
        text = _read_gbk(_PACKAGING_BAT)
        self.assertIn("tool-bin-dir.txt", text)
        self.assertIn("uv tool dir --bin", text)
        self.assertIn(r"%USERPROFILE%\.local\bin\doyoutrade.exe", text)


class LauncherErrorCopyTests(unittest.TestCase):
    """C: actionable diagnostics instead of reboot folklore."""

    def test_packaging_launcher_mentions_uv_tool_list_not_reboot(self) -> None:
        text = _read_gbk(_PACKAGING_BAT)
        self.assertIn("uv tool list", text)
        self.assertIn("未找到", text)
        self.assertNotIn("重启电脑", text)
        self.assertNotIn("重新登录", text)

    def test_root_launcher_mentions_uv_tool_list_not_reboot(self) -> None:
        text = _read_gbk(_ROOT_BAT)
        self.assertIn("uv tool list", text)
        self.assertIn("未找到", text)
        self.assertNotIn("重启电脑", text)

    def test_bat_generator_error_copy_matches_policy(self) -> None:
        src = _BAT_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("uv tool list", src)
        self.assertNotIn("重启电脑", src)
        # Keep a light PATH hint without demanding reboot/re-login.
        self.assertIn("新开", src)


if __name__ == "__main__":
    unittest.main()
