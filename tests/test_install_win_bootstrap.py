"""Windows PowerShell 5.1 ``-File`` must be able to run the installer entrypoint.

``install.ps1`` is UTF-8 *without* BOM so ``irm | iex`` keeps working
(see ``test_installer_no_bom.py``). On Chinese Windows, Windows PowerShell
5.1 ``-File`` reads BOM-less scripts as system ANSI (CP936), which corrupts
Chinese comments/strings and raises ``ParserError`` — the GUI installer and
``安装DoYouTrade.bat`` both use ``-File``, so the install never completes and
the launcher reports ``doyoutrade`` missing.

``install-win.ps1`` is the ASCII-only ``-File`` entrypoint: it re-encodes
``install.ps1`` to a UTF-8-BOM temp copy and re-invokes powershell ``-File``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_INSTALL_PS1 = _ROOT / "install.ps1"
_INSTALL_WIN = _ROOT / "install-win.ps1"


def _powershell() -> str | None:
    system = os.environ.get("SystemRoot", r"C:\Windows")
    candidate = Path(system) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    if candidate.is_file():
        return str(candidate)
    return shutil.which("powershell")


@unittest.skipUnless(os.name == "nt", "Windows-only")
class InstallWinBootstrapTests(unittest.TestCase):
    def test_install_win_exists_and_is_ascii(self) -> None:
        self.assertTrue(_INSTALL_WIN.is_file(), "install-win.ps1 missing")
        raw = _INSTALL_WIN.read_bytes()
        self.assertNotEqual(
            raw[:3],
            b"\xef\xbb\xbf",
            "install-win.ps1 must not start with a UTF-8 BOM",
        )
        try:
            raw.decode("ascii")
        except UnicodeDecodeError as exc:
            self.fail(
                "install-win.ps1 must be pure ASCII so Windows PowerShell "
                f"5.1 -File can parse it on any system ANSI code page: {exc}"
            )

    def test_install_ps1_still_has_no_bom(self) -> None:
        raw = _INSTALL_PS1.read_bytes()
        self.assertNotEqual(raw[:3], b"\xef\xbb\xbf")

    def test_file_dash_cannot_parse_install_ps1_on_chinese_windows(self) -> None:
        """Document the failure mode: -File on BOM-less UTF-8 breaks under CP936.

        Skipped when the process ANSI code page is already UTF-8 (rare),
        because then -File would succeed and the assertion would be wrong.
        """
        shell = _powershell()
        if not shell:
            self.skipTest("powershell.exe not found")

        # Detect system ANSI code page via PowerShell.
        probe = subprocess.run(
            [
                shell,
                "-NoProfile",
                "-Command",
                "[Console]::OutputEncoding.CodePage; "
                "[System.Text.Encoding]::Default.CodePage",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        pages = [ln.strip() for ln in (probe.stdout or "").splitlines() if ln.strip()]
        if not pages:
            self.skipTest("could not read system ANSI code page")
        ansi_page = int(pages[-1])
        if ansi_page in (65001, 1200, 1201):
            self.skipTest(f"system ANSI code page is {ansi_page}, not CP936")

        # ParseFile uses the same encoding rules as -File for BOM-less scripts.
        ps = (
            "$p = $env:DOYOUTRADE_INSTALL_PS1; "
            "$t = $null; $e = $null; "
            "[void][System.Management.Automation.Language.Parser]::ParseFile("
            "$p, [ref]$t, [ref]$e); "
            "if ($e -and $e.Count -gt 0) { exit 2 } else { exit 0 }"
        )
        env = dict(os.environ, DOYOUTRADE_INSTALL_PS1=str(_INSTALL_PS1))
        result = subprocess.run(
            [shell, "-NoProfile", "-Command", ps],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        self.assertEqual(
            result.returncode,
            2,
            "Expected install.ps1 to fail ParseFile under system ANSI "
            f"(cp{ansi_page}); got exit {result.returncode}. "
            "If this starts passing, the no-BOM + Chinese assumption changed.",
        )

    def test_install_win_dash_file_parses(self) -> None:
        shell = _powershell()
        if not shell:
            self.skipTest("powershell.exe not found")
        ps = (
            "$p = $env:DOYOUTRADE_INSTALL_WIN; "
            "$t = $null; $e = $null; "
            "[void][System.Management.Automation.Language.Parser]::ParseFile("
            "$p, [ref]$t, [ref]$e); "
            "if ($e -and $e.Count -gt 0) { "
            "$e | ForEach-Object { Write-Output $_.ToString() }; exit 1 "
            "} else { exit 0 }"
        )
        env = dict(os.environ, DOYOUTRADE_INSTALL_WIN=str(_INSTALL_WIN))
        result = subprocess.run(
            [shell, "-NoProfile", "-Command", ps],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        self.assertEqual(
            result.returncode,
            0,
            f"install-win.ps1 must ParseFile cleanly. stderr={result.stderr!r} "
            f"stdout={result.stdout!r}",
        )

    def test_install_win_reencodes_to_utf8_bom_temp(self) -> None:
        """Dry-run the re-encode step without executing the full installer."""
        shell = _powershell()
        if not shell:
            self.skipTest("powershell.exe not found")

        # Inline the same re-encode logic and assert the temp file parses.
        ps = r"""
$ErrorActionPreference = 'Stop'
$scriptPath = $env:DOYOUTRADE_INSTALL_PS1
$raw = [System.IO.File]::ReadAllBytes($scriptPath)
if ($raw.Length -ge 3 -and $raw[0] -eq 0xEF -and $raw[1] -eq 0xBB -and $raw[2] -eq 0xBF) {
  $text = [System.Text.Encoding]::UTF8.GetString($raw, 3, $raw.Length - 3)
} else {
  $text = [System.Text.Encoding]::UTF8.GetString($raw)
}
$tmp = Join-Path $env:TEMP ('doyoutrade-install-test-' + [guid]::NewGuid().ToString('N') + '.ps1')
$utf8Bom = New-Object System.Text.UTF8Encoding $true
[System.IO.File]::WriteAllText($tmp, $text, $utf8Bom)
try {
  $t = $null; $e = $null
  [void][System.Management.Automation.Language.Parser]::ParseFile($tmp, [ref]$t, [ref]$e)
  if ($e -and $e.Count -gt 0) {
    $e | ForEach-Object { Write-Output $_.ToString() }
    exit 1
  }
  $head = [System.IO.File]::ReadAllBytes($tmp)[0..2]
  if (-not ($head[0] -eq 0xEF -and $head[1] -eq 0xBB -and $head[2] -eq 0xBF)) {
    Write-Output 'missing BOM on temp file'
    exit 1
  }
  exit 0
} finally {
  Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
}
"""
        env = dict(os.environ, DOYOUTRADE_INSTALL_PS1=str(_INSTALL_PS1))
        result = subprocess.run(
            [shell, "-NoProfile", "-Command", ps],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        self.assertEqual(
            result.returncode,
            0,
            f"UTF-8 BOM temp re-encode must ParseFile. stdout={result.stdout!r} "
            f"stderr={result.stderr!r}",
        )


if __name__ == "__main__":
    unittest.main()
