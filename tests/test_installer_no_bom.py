"""PowerShell installer scripts must not start with a UTF-8 BOM.

``Invoke-RestMethod`` keeps a leading ``EF BB BF`` (U+FEFF) in the returned
string, but ``Invoke-Expression`` parses that as a leading expression
statement. Once that happens, ``[CmdletBinding()]`` and ``param()`` are no
longer at the top of the script, so ``irm … install.ps1 | iex`` fails with
``意外的属性"CmdletBinding"`` / ``意外的标记"param"`` — the exact error that
broke the README's one-line Windows install. Running the same script as a
``.ps1`` file works because the PowerShell engine strips the BOM on file load.

This guard scans every tracked ``*.ps1`` file and fails if any carries a BOM,
so the regression cannot be re-introduced silently (git treats a BOM-prefixed
file as binary and hides the diff, so a unit test is the reliable tripwire).
"""

import os
import shutil
import subprocess
import tempfile
import unittest

_BOM = b"\xef\xbb\xbf"


def _tracked_ps1_files() -> list[str]:
    """Return tracked ``*.ps1`` paths via ``git ls-files`` (empty on failure)."""
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z", "*.ps1"],
            capture_output=True,
            text=True,
            check=False,
        )
    except (FileNotFoundError, OSError):
        return []
    if result.returncode != 0:
        return []
    return [p for p in result.stdout.split("\0") if p]


class Ps1BomTests(unittest.TestCase):
    def test_no_tracked_ps1_file_has_utf8_bom(self) -> None:
        paths = _tracked_ps1_files()
        if not paths:
            self.skipTest("git ls-files unavailable or no tracked .ps1 files")
        offenders: list[str] = []
        for path in paths:
            try:
                with open(path, "rb") as fh:
                    head = fh.read(3)
            except OSError as exc:
                offenders.append(f"{path} (unreadable: {exc})")
                continue
            if head == _BOM:
                offenders.append(path)
        self.assertEqual(
            offenders,
            [],
            "These .ps1 files start with a UTF-8 BOM, which breaks "
            "`irm | iex` installs (Invoke-Expression treats U+FEFF as a "
            "leading statement, so [CmdletBinding()]/param() are no longer "
            "at the top of the script): " + ", ".join(offenders),
        )

    def test_install_scripts_parse_via_invoke_expression(self) -> None:
        """The README's `irm | iex` path must actually parse.

        Faithfully simulates ``irm | iex``: write the raw bytes to a temp
        file, have PowerShell read them and decode as UTF-8 (which keeps a
        leading U+FEFF exactly like ``Invoke-RestMethod`` does), then compile
        the result into a script block without executing. A BOM surfaces here
        as a parse error on the ``[CmdletBinding()]`` line. Skipped when no
        PowerShell runtime is on PATH (the BOM check above is the portable
        guard).
        """
        shell = shutil.which("pwsh") or shutil.which("powershell")
        if not shell:
            self.skipTest("no PowerShell runtime available")
        # Read raw bytes, decode as UTF-8 keeping any BOM, compile to a script
        # block. This mirrors `irm` (which preserves U+FEFF) + `iex` parsing.
        # The temp file path is passed via env var because `powershell -Command`
        # does not forward trailing argv into `$args`.
        ps = (
            "$ErrorActionPreference='Stop';"
            "try {"
            "$b=[System.IO.File]::ReadAllBytes($env:DOYOUTRADE_BOM_CHECK_PATH);"
            "$s=[System.Text.Encoding]::UTF8.GetString($b);"
            "[void][scriptblock]::Create($s);"
            "exit 0"
            "} catch { Write-Error $_.Exception.Message; exit 1 }"
        )
        targets = ["install.ps1", "qmt-proxy/installer/install.ps1"]
        for path in targets:
            with self.subTest(path=path):
                self.assertTrue(
                    os.path.exists(path),
                    f"{path} missing — run this test from the repo root",
                )
                with open(path, "rb") as fh:
                    raw = fh.read()
                with tempfile.NamedTemporaryFile(
                    suffix=".bin", delete=False
                ) as tmp:
                    tmp.write(raw)
                    tmp_path = tmp.name
                env = dict(os.environ, DOYOUTRADE_BOM_CHECK_PATH=tmp_path)
                try:
                    result = subprocess.run(
                        [shell, "-NoProfile", "-Command", ps],
                        capture_output=True,
                        text=True,
                        check=False,
                        env=env,
                    )
                finally:
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass
                self.assertEqual(
                    result.returncode,
                    0,
                    f"{path} failed to parse as a script block via the "
                    f"`irm | iex` simulation (BOM check). "
                    f"stderr: {result.stderr.strip()}",
                )


if __name__ == "__main__":
    unittest.main()
