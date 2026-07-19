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
D. Field follow-up (0.1.6): relocation alone is NOT enough — the block is
   often process-wide (Redirection Trust mitigation / OneDrive minifilter),
   so a clean ``C:\\doyoutrade\\uv`` fails identically. On 448 the installer
   must stop routing ``--python 3.12`` through uv's minor-version *junction*
   and instead pass an explicit junction-free interpreter path: reuse the
   already-downloaded ``cpython-<full-version>`` plain dir, else a system
   Python 3.12, else a silent per-user python.org install. It must also
   delete the dangling minor-version junction a failed ``uv python install``
   leaves behind, which uv cannot self-heal (astral-sh/uv#19622).
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


class JunctionFreeFallbackTests(unittest.TestCase):
    """0.1.6 field failure: relocation retry still died with 448 on
    ``C:\\doyoutrade\\uv`` — the junction itself is unusable on such machines,
    so the retry must switch to an explicit junction-free interpreter."""

    def setUp(self) -> None:
        self.ps1 = _INSTALL_PS1.read_text(encoding="utf-8")

    def test_retry_triggers_even_when_already_on_system_drive(self) -> None:
        # The old guard skipped the retry when uvHome was already the system
        # drive; the junction-free fallback must run regardless of location.
        self.assertIn(
            "if (($result.ExitCode -ne 0) -and (Test-IsUntrustedMountError $result.Output)) {",
            self.ps1,
        )

    def test_retry_passes_explicit_interpreter_path(self) -> None:
        self.assertIn("Resolve-JunctionFreePython", self.ps1)
        self.assertIn('"--python", $pyExe', self.ps1)

    def test_cleans_dangling_minor_version_junction(self) -> None:
        # uv cannot recover the half-made junction itself (astral-sh/uv#19622);
        # leaving it poisons every later uv command.
        self.assertIn("Remove-DanglingMinorVersionLinks", self.ps1)
        self.assertIn("astral-sh/uv#19622", self.ps1)

    def test_reuses_downloaded_full_version_dir_first(self) -> None:
        # The download succeeds even when the junction fails; the plain
        # cpython-<full-version> dir must be preferred over re-downloading.
        self.assertIn("Get-ManagedJunctionFreePython", self.ps1)
        self.assertRegex(self.ps1, r"cpython-\$\(\$script:DoyoutradePythonVersion\)\.\*")

    def test_falls_back_to_system_python(self) -> None:
        self.assertIn("Get-SystemPython", self.ps1)
        # py launcher covers PEP 514 registry installs.
        self.assertRegex(self.ps1, r"Get-Command py ")

    def test_last_resort_python_org_user_scoped_install(self) -> None:
        self.assertIn("Install-PythonFromPythonOrg", self.ps1)
        self.assertIn('$script:DoyoutradePythonOrgVersion = "3.12.10"', self.ps1)
        self.assertIn("https://www.python.org/ftp/python/", self.ps1)
        # Per-user, no admin, not on PATH.
        self.assertIn("InstallAllUsers=0", self.ps1)
        self.assertIn("PrependPath=0", self.ps1)

    def test_validates_interpreter_version_before_use(self) -> None:
        # Rejects the Microsoft Store alias and wrong-version pythons.
        self.assertIn("Test-PythonExeUsable", self.ps1)
        self.assertIn("sys.version_info[:2]", self.ps1)

    def test_final_guidance_mentions_admin_and_onedrive(self) -> None:
        # If even the fallback fails, point at the two real-world causes.
        self.assertIn("以管理员身份运行", self.ps1)
        self.assertIn("Redirection Trust", self.ps1)

    def test_diagnostics_report_fallback_interpreter(self) -> None:
        self.assertIn("JunctionFreePython", self.ps1)
        self.assertIn("免junction解释器", self.ps1)


if __name__ == "__main__":
    unittest.main()
