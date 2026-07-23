"""Contract tests: end-user installers prefer Release wheels (web UI bundled).

Source installs (``DOYOUTRADE_INSTALL_SOURCE=git+…``) remain supported for
developers; the default path must not require Node.js on the client machine.
"""

from __future__ import annotations

import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_INSTALL_SH = _ROOT / "install.sh"
_INSTALL_PS1 = _ROOT / "install.ps1"
_INSTALL_WIN = _ROOT / "install-win.ps1"
_ISS = _ROOT / "packaging" / "windows" / "doyoutrade-setup.iss"
_HATCH = _ROOT / "hatch_build.py"
_WF = _ROOT / ".github" / "workflows" / "build-windows-installer.yml"
_README = _ROOT / "README.md"


class InstallPs1WheelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ps1 = _INSTALL_PS1.read_text(encoding="utf-8")

    def test_resolves_release_wheel_helper(self) -> None:
        self.assertIn("function Resolve-ReleaseWheelUrl", self.ps1)
        self.assertIn("function Get-LatestReleaseTag", self.ps1)
        self.assertIn("releases/download/", self.ps1)
        self.assertIn("doyoutrade-", self.ps1)
        self.assertIn("-py3-none-any.whl", self.ps1)

    def test_accepts_version_param_and_env(self) -> None:
        self.assertRegex(self.ps1, r"\[string\]\$Version")
        self.assertIn("DOYOUTRADE_INSTALL_VERSION", self.ps1)

    def test_default_path_does_not_advertise_api_only_without_node(self) -> None:
        # Default installs use a prebuilt wheel; Node is no longer required.
        self.assertNotIn("将安装为「API + CLI」模式", self.ps1)

    def test_gitee_and_github_wheel_hosts(self) -> None:
        self.assertIn("releases/download/", self.ps1)
        self.assertIn('GithubOwnerRepo = "renjiegod/doyoutrade"', self.ps1)
        self.assertIn('GiteeOwner = "renjie-god"', self.ps1)
        self.assertIn('GiteeRepo = "doyoutrade"', self.ps1)
        self.assertIn("https://github.com/", self.ps1)
        self.assertIn("https://gitee.com/", self.ps1)


class InstallShWheelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sh = _INSTALL_SH.read_text(encoding="utf-8")

    def test_resolves_release_wheel_helper(self) -> None:
        self.assertIn("resolve_release_wheel_url", self.sh)
        self.assertIn("latest_release_tag", self.sh)
        self.assertIn("releases/download/", self.sh)
        self.assertIn("-py3-none-any.whl", self.sh)

    def test_accepts_version_env(self) -> None:
        self.assertIn("DOYOUTRADE_INSTALL_VERSION", self.sh)

    def test_status_helpers_write_to_stderr(self) -> None:
        # Regression: info/warn/ok used to print on stdout, so
        # SOURCE="$(resolve_default_source)" swallowed "==> 未指定版本…" into the
        # PEP 508 URL and uv failed with "Failed to parse".
        for name in ("info", "warn", "ok"):
            self.assertRegex(
                self.sh,
                rf"{name}\(\)\s*\{{\s*printf .* >&2;\s*\}}",
                msg=f"{name}() must redirect status output to stderr",
            )

    def test_resolve_default_source_stdout_is_clean_url(self) -> None:
        # Behavioral: stdout of resolve_default_source must be ONLY the wheel URL.
        import os
        import subprocess
        import tempfile
        import textwrap

        # Extract helpers from install.sh up through resolve_default_source, then
        # invoke it with a pinned version so we don't hit the network for latest.
        harness = textwrap.dedent(
            r"""
            set -eu
            # shellcheck disable=SC1091
            . "$INSTALL_SH_SNIPPET"
            # Override network helpers — we only care that status lines stay off stdout.
            preferred_mirror() { printf '%s\n' "github"; }
            remote_url_exists() { return 0; }
            latest_release_tag() { printf '%s\n' "v0.1.30"; }
            DOYOUTRADE_INSTALL_VERSION=0.1.30
            resolve_default_source
            """
        ).strip()
        # Build a snippet that defines helpers but does not run main / install.
        stop_at = "if [ -n \"${DOYOUTRADE_INSTALL_SOURCE:-}\" ]; then"
        snippet = self.sh.split(stop_at, 1)[0]
        with tempfile.TemporaryDirectory() as tmp:
            snippet_path = Path(tmp) / "helpers.sh"
            harness_path = Path(tmp) / "harness.sh"
            snippet_path.write_text(snippet, encoding="utf-8")
            harness_path.write_text(harness + "\n", encoding="utf-8")
            env = {**os.environ, "INSTALL_SH_SNIPPET": str(snippet_path)}
            proc = subprocess.run(
                ["sh", str(harness_path)],
                check=False,
                capture_output=True,
                text=True,
                env=env,
            )
        self.assertEqual(proc.returncode, 0, msg=f"stderr={proc.stderr!r} stdout={proc.stdout!r}")
        stdout_lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
        self.assertEqual(
            stdout_lines,
            ["https://github.com/renjiegod/doyoutrade/releases/download/v0.1.30/doyoutrade-0.1.30-py3-none-any.whl"],
            msg=f"stdout polluted: {proc.stdout!r}; stderr={proc.stderr!r}",
        )
        self.assertIn("安装源", proc.stderr)
        self.assertNotIn("==>", proc.stdout)

    def test_default_path_does_not_advertise_api_only_without_node(self) -> None:
        self.assertNotIn("将安装为「API + CLI」模式", self.sh)

    def test_gitee_and_github_wheel_hosts(self) -> None:
        self.assertIn("releases/download/", self.sh)
        self.assertIn('GITHUB_OWNER_REPO="renjiegod/doyoutrade"', self.sh)
        self.assertIn('GITEE_OWNER="renjie-god"', self.sh)
        self.assertIn('GITEE_REPO="doyoutrade"', self.sh)
        self.assertIn("https://github.com/", self.sh)
        self.assertIn("https://gitee.com/", self.sh)


class InstallWinAndIssVersionPinTests(unittest.TestCase):
    def test_install_win_forwards_version(self) -> None:
        text = _INSTALL_WIN.read_text(encoding="utf-8")
        self.assertRegex(text, r"\[string\]\$Version")
        self.assertIn("-Version", text)
        self.assertIn("DOYOUTRADE_INSTALL_VERSION", text)

    def test_iss_passes_app_version_to_installer(self) -> None:
        iss = _ISS.read_text(encoding="utf-8")
        self.assertIn("MyAppVersion", iss)
        self.assertRegex(iss, r"-Version\s+.*MyAppVersion|Version.*\{#MyAppVersion\}")


class HatchRequireFrontendTests(unittest.TestCase):
    def test_require_frontend_env_fails_build_when_missing(self) -> None:
        text = _HATCH.read_text(encoding="utf-8")
        self.assertIn("DOYOUTRADE_REQUIRE_FRONTEND", text)
        self.assertRegex(text, r"DOYOUTRADE_REQUIRE_FRONTEND.*=.*1")


class ReleaseWorkflowTests(unittest.TestCase):
    def test_builds_frontend_and_verifies_wheel(self) -> None:
        wf = _WF.read_text(encoding="utf-8")
        self.assertIn("setup-node", wf)
        self.assertIn("npm ci", wf)
        self.assertIn("npm run build", wf)
        self.assertIn("DOYOUTRADE_REQUIRE_FRONTEND", wf)
        self.assertIn("_frontend", wf)
        self.assertIn(".whl", wf)

    def test_exports_wheel_env_for_same_step_verify(self) -> None:
        # Regression: writing only to GITHUB_ENV left os.environ["WHEEL"] unset
        # in the same step (v0.1.11 release KeyError), so Setup.exe never published.
        wf = _WF.read_text(encoding="utf-8")
        self.assertRegex(wf, r"export\s+WHEEL=")
        self.assertIn('os.environ["WHEEL"]', wf)

    def test_uploads_wheel_to_github_and_gitee(self) -> None:
        wf = _WF.read_text(encoding="utf-8")
        self.assertIn("GITEE_TOKEN", wf)
        self.assertIn("sync_gitee_release", wf)


class ReadmeWheelDocsTests(unittest.TestCase):
    def test_readme_says_end_users_do_not_need_node(self) -> None:
        readme = _README.read_text(encoding="utf-8")
        self.assertIn("预构建", readme)
        # Environment table must not list Node as required for end users.
        self.assertNotRegex(
            readme,
            r"\| Node\.js \+ npm \|[^\n]*\| 仅前端控制台需要 \|",
        )


if __name__ == "__main__":
    unittest.main()
