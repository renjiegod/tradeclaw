"""Windows launcher .bat files must stay intact under cmd.exe parsing.

Chinese Windows ``cmd.exe`` reads ``.bat`` sources using the system ANSI
code page (CP936/GBK). UTF-8 sources — especially with ``chcp 65001`` and
``if (...)`` blocks — desync the parser so fragments of ``echo`` / ``rem``
lines execute as commands (``'ATH' is not recognized``, …).

These launchers are therefore stored as GBK, use ``goto`` instead of
parenthesized ``if`` blocks for Chinese messages, and omit ``chcp 65001``.
Regenerate with ``python scripts/write_windows_bats_gbk.py``.

Runtime tests drive the packaging launcher (and the repo-root twin) with
``doyoutrade`` deliberately absent from PATH, asserting:

1. exit code 1 (command truly missing), and
2. no ``is not recognized as an internal or external command`` lines —
   the failure message itself must stay intact.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_LAUNCHERS = [
    _ROOT / "packaging" / "windows" / "launch-doyoutrade.bat",
    _ROOT / "启动DoYouTrade.bat",
]
_INSTALL_BAT = _ROOT / "安装DoYouTrade.bat"
_ALL_BATS = _LAUNCHERS + [_INSTALL_BAT]

# cmd.exe prints this exact English phrase when a token is executed as a command.
_CMD_NOT_RECOGNIZED = "is not recognized as an internal or external command"


def _cmd_available() -> bool:
    return sys.platform == "win32" and shutil.which("cmd.exe") is not None


def _system_ansi_code_page() -> int | None:
    """Return Windows system ANSI code page (GetACP), or None if unknown."""
    shell = shutil.which("powershell") or shutil.which("powershell.exe")
    if not shell:
        return None
    probe = subprocess.run(
        [
            shell,
            "-NoProfile",
            "-Command",
            "[System.Text.Encoding]::Default.CodePage",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    for ln in (probe.stdout or "").splitlines():
        ln = ln.strip()
        if ln.isdigit():
            return int(ln)
    return None


def _read_bat_text(path: Path) -> str:
    return path.read_bytes().decode("gbk")


def _run_bat_without_doyoutrade(bat: Path) -> subprocess.CompletedProcess[str]:
    """Run ``bat`` in a clean env where ``doyoutrade`` cannot be resolved."""
    # Strip PATH entries that might contain a real doyoutrade, and point
    # USERPROFILE at a temp home so the ``~\\.local\\bin\\doyoutrade.exe``
    # fallback also misses.
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "home"
        home.mkdir()
        (home / ".local" / "bin").mkdir(parents=True)

        # Keep a minimal PATH so cmd.exe itself still works.
        system_root = os.environ.get("SystemRoot", r"C:\Windows")
        minimal_path = os.pathsep.join(
            [
                str(Path(system_root) / "System32"),
                str(Path(system_root)),
                str(Path(system_root) / "System32" / "Wbem"),
            ]
        )
        env = {
            **os.environ,
            "USERPROFILE": str(home),
            "HOME": str(home),
            "PATH": minimal_path,
            # Avoid inheriting a real doyoutrade from the parent process.
            "PATHEXT": ".COM;.EXE;.BAT;.CMD",
        }
        # Feed a newline so ``pause`` does not hang. Decode console output as
        # the system ANSI code page (GBK on Chinese Windows).
        return subprocess.run(
            ["cmd.exe", "/d", "/c", str(bat)],
            input="\n",
            capture_output=True,
            text=True,
            encoding="gbk",
            errors="replace",
            env=env,
            check=False,
        )


@unittest.skipUnless(_cmd_available(), "Windows cmd.exe required")
class LauncherBatEncodingTests(unittest.TestCase):
    def test_launchers_exist(self) -> None:
        for path in _ALL_BATS:
            with self.subTest(path=str(path)):
                self.assertTrue(path.is_file(), f"missing launcher: {path}")

    def test_bats_are_gbk_not_utf8(self) -> None:
        for path in _ALL_BATS:
            with self.subTest(path=str(path)):
                raw = path.read_bytes()
                with self.assertRaises(
                    UnicodeDecodeError,
                    msg=f"{path.name} must not be valid UTF-8; "
                    f"regenerate via scripts/write_windows_bats_gbk.py",
                ):
                    raw.decode("utf-8")
                text = raw.decode("gbk")
                self.assertNotIn(
                    "chcp 65001",
                    text,
                    f"{path.name} must not switch to UTF-8 code page",
                )
                self.assertIn("goto", text.lower())

    def test_missing_doyoutrade_does_not_shatter_echo_lines(self) -> None:
        for path in _LAUNCHERS:
            with self.subTest(path=str(path)):
                result = _run_bat_without_doyoutrade(path)
                combined = (result.stdout or "") + (result.stderr or "")
                self.assertEqual(
                    result.returncode,
                    1,
                    f"{path.name} should exit 1 when doyoutrade is missing; "
                    f"got {result.returncode}. output:\n{combined}",
                )
                self.assertNotIn(
                    _CMD_NOT_RECOGNIZED,
                    combined,
                    f"{path.name} shattered echo/rem lines into fake "
                    f"commands. Full output:\n{combined}",
                )
                self.assertIn(
                    "doyoutrade",
                    combined.lower(),
                    f"{path.name} should still mention doyoutrade in the "
                    f"error message. output:\n{combined}",
                )
                # GBK .bat sources only render as intact Chinese under CP936
                # (Chinese Windows). GitHub Actions windows-latest is en-US
                # (typically CP1252) — require the glyph check only there.
                acp = _system_ansi_code_page()
                if acp == 936:
                    self.assertIn(
                        "未找到",
                        combined,
                        f"{path.name} Chinese error text should render under "
                        f"CP936. output:\n{combined}",
                    )
                else:
                    # Still require the ASCII diagnostic cue we added for all locales.
                    self.assertIn(
                        "uv tool list",
                        combined.lower(),
                        f"{path.name} should mention uv tool list even when "
                        f"CJK glyphs are mojibake under ACP={acp}. "
                        f"output:\n{combined}",
                    )

    def test_launchers_avoid_parenthesized_if_blocks_with_echo(self) -> None:
        """Static guard: Chinese echo must not sit inside ``if (...)`` blocks."""
        for path in _ALL_BATS:
            with self.subTest(path=str(path)):
                text = _read_bat_text(path)
                code_lines = [
                    ln
                    for ln in text.splitlines()
                    if not ln.lstrip().lower().startswith("rem")
                ]
                in_block = False
                block_has_cjk_echo = False
                for ln in code_lines:
                    stripped = ln.strip().lower()
                    if stripped.startswith("if ") and stripped.endswith("("):
                        in_block = True
                        block_has_cjk_echo = False
                        continue
                    if in_block:
                        if stripped.startswith("echo") and any(
                            ord(ch) > 127 for ch in ln
                        ):
                            block_has_cjk_echo = True
                        if stripped == ")" or stripped.startswith(") "):
                            self.assertFalse(
                                block_has_cjk_echo,
                                f"{path.name} has CJK echo inside an "
                                f"if (...) block — use goto labels instead",
                            )
                            in_block = False


if __name__ == "__main__":
    unittest.main()
