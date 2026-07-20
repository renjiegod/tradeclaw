"""Guards for China / Gitee install-source selection (C+D).

When GitHub is unreachable (or ``DOYOUTRADE_MIRROR`` forces it), installers
must prefer the Gitee mirror ``https://gitee.com/renjie-god/doyoutrade`` so
China-network users can install without a VPN. Explicit
``DOYOUTRADE_INSTALL_SOURCE`` always wins.
"""

from __future__ import annotations

import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_INSTALL_SH = _ROOT / "install.sh"
_INSTALL_PS1 = _ROOT / "install.ps1"
_INSTALL_WIN = _ROOT / "install-win.ps1"
_BAT_WRITER = _ROOT / "scripts" / "write_windows_bats_gbk.py"
_README = _ROOT / "README.md"

_GITEE_REPO = "https://gitee.com/renjie-god/doyoutrade"
_GITEE_GIT = "git+https://gitee.com/renjie-god/doyoutrade.git"
_GITHUB_GIT = "git+https://github.com/renjiegod/doyoutrade.git"
_GITEE_RAW_PS1 = "https://gitee.com/renjie-god/doyoutrade/raw/main/install.ps1"
_GITEE_RAW_SH = "https://gitee.com/renjie-god/doyoutrade/raw/main/install.sh"


class InstallShMirrorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sh = _INSTALL_SH.read_text(encoding="utf-8")

    def test_documents_gitee_and_mirror_env(self) -> None:
        self.assertIn("DOYOUTRADE_MIRROR", self.sh)
        self.assertIn(_GITEE_GIT, self.sh)
        self.assertIn(_GITEE_RAW_SH, self.sh)

    def test_resolves_default_source_via_helper(self) -> None:
        self.assertIn("resolve_default_source", self.sh)
        self.assertIn("github_reachable", self.sh)
        # Explicit install source still wins over mirror auto-detect.
        self.assertIn("DOYOUTRADE_INSTALL_SOURCE", self.sh)

    def test_mirror_env_forces_gitee_or_github(self) -> None:
        # D: DOYOUTRADE_MIRROR=gitee|github must short-circuit the probe.
        self.assertRegex(self.sh, r'DOYOUTRADE_MIRROR.*gitee')
        self.assertRegex(self.sh, r'DOYOUTRADE_MIRROR.*github')

    def test_network_probe_falls_back_to_gitee(self) -> None:
        # C: short-timeout probe against GitHub; unreachable -> Gitee.
        self.assertRegex(self.sh, r"connect-timeout\s+[1-9]")
        self.assertIn(_GITEE_GIT, self.sh)
        self.assertIn(_GITHUB_GIT, self.sh)


class InstallPs1MirrorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ps1 = _INSTALL_PS1.read_text(encoding="utf-8")

    def test_resolve_helper_exists(self) -> None:
        self.assertIn("function Resolve-DefaultInstallSource", self.ps1)
        self.assertIn("function Test-GitHubReachable", self.ps1)
        self.assertIn("DOYOUTRADE_MIRROR", self.ps1)

    def test_default_source_not_hardcoded_github_only(self) -> None:
        # Param default must not lock forever to GitHub; resolution happens
        # when neither DOYOUTRADE_INSTALL_SOURCE nor -Source is provided.
        self.assertIn(_GITEE_GIT, self.ps1)
        self.assertIn("Resolve-DefaultInstallSource", self.ps1)

    def test_gitee_git_source_converts_to_archive(self) -> None:
        self.assertIn("Convert-GitSourceToArchiveUrl", self.ps1)
        self.assertIn("gitee.com/$owner/$repo/repository/archive/", self.ps1)
        # Message should not say "GitHub 归档" exclusively once Gitee works.
        self.assertRegex(
            self.ps1,
            r"改用.*(归档直链|Gitee|GitHub).*安装",
        )

    def test_mirror_env_values_recognized(self) -> None:
        for token in ("gitee", "github", "cn", "china", "gh"):
            self.assertIn(token, self.ps1)


class InstallWinMirrorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ps1 = _INSTALL_WIN.read_text(encoding="utf-8")

    def test_does_not_hardcode_github_as_only_default(self) -> None:
        # Forward empty Source so install.ps1 can auto-resolve mirror.
        self.assertNotRegex(
            self.ps1,
            r'\$Source\s*=\s*\$\(if \(\$env:DOYOUTRADE_INSTALL_SOURCE\).*github\.com/renjiegod/doyoutrade',
        )
        self.assertIn("DOYOUTRADE_INSTALL_SOURCE", self.ps1)


class BatAndReadmeMirrorTests(unittest.TestCase):
    def test_bat_writer_prefers_gitee_when_github_unreachable(self) -> None:
        writer = _BAT_WRITER.read_text(encoding="utf-8")
        self.assertIn(_GITEE_RAW_PS1, writer)
        self.assertIn("DOYOUTRADE_MIRROR", writer)
        self.assertIn("TimeoutSec", writer)

    def test_readme_documents_gitee_install_paths(self) -> None:
        readme = _README.read_text(encoding="utf-8")
        self.assertIn(_GITEE_REPO, readme)
        self.assertIn("DOYOUTRADE_MIRROR", readme)
        self.assertIn(_GITEE_RAW_SH, readme)
        self.assertIn(_GITEE_RAW_PS1, readme)


if __name__ == "__main__":
    unittest.main()
