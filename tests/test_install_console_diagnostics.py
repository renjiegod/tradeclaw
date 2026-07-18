"""install.ps1 must dump actionable diagnostics to the console on failure.

GUI installer (Inno) runs PowerShell with a visible window; users need the
failure details *in that window*, including on -Force reinstall. The window
must stay open (pause) so the dump is readable before the process exits.
"""

from __future__ import annotations

import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_INSTALL_PS1 = _ROOT / "install.ps1"
_ISS = _ROOT / "packaging" / "windows" / "doyoutrade-setup.iss"


class InstallConsoleDiagnosticsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ps1 = _INSTALL_PS1.read_text(encoding="utf-8")
        self.iss = _ISS.read_text(encoding="utf-8")

    def test_has_diagnostics_dumper(self) -> None:
        self.assertIn("Write-InstallDiagnostics", self.ps1)
        self.assertIn("[诊断]", self.ps1)
        self.assertIn("uv tool list", self.ps1)
        self.assertIn("uv tool dir --bin", self.ps1)
        self.assertIn("退出码", self.ps1)
        self.assertIn("阶段", self.ps1)

    def test_write_die_dumps_diagnostics_and_pauses(self) -> None:
        # Write-Die must call the dumper and pause so Inno's PS window stays up.
        self.assertRegex(
            self.ps1,
            r"function\s+Write-Die[\s\S]*Write-InstallDiagnostics",
        )
        self.assertRegex(
            self.ps1,
            r"function\s+Write-Die[\s\S]*Pause-OnInstallFailure",
        )
        self.assertIn("Pause-OnInstallFailure", self.ps1)
        self.assertIn("DOYOUTRADE_INSTALL_NO_PAUSE", self.ps1)

    def test_failure_sites_pass_stage(self) -> None:
        for stage in (
            "uv-install",
            "uv-post-install",
            "uv-tool-uninstall",
            "uv-tool-install",
            "shim-verify",
        ):
            self.assertIn(
                stage,
                self.ps1,
                f"expected failure stage label {stage!r} for triage",
            )

    def test_reinstall_prints_context_before_uninstall(self) -> None:
        # -Force / confirmed reinstall should show what is being replaced.
        self.assertIn("重装前 uv tool list", self.ps1)
        self.assertIn("正在卸载现有 doyoutrade", self.ps1)
        self.assertIn("旧版本已卸载，开始重新安装", self.ps1)

    def test_inno_msgbox_points_at_console_diagnostics(self) -> None:
        self.assertIn("命令行", self.iss)
        self.assertIn("诊断", self.iss)


if __name__ == "__main__":
    unittest.main()
