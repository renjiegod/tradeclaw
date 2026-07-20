"""Release wheel URL helpers — installers and in-app updater share these.

End-user installs must prefer a prebuilt wheel that already embeds
``doyoutrade/_frontend`` so machines without Node.js still get the web UI.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from doyoutrade.infra.release_artifacts import (
    gitee_wheel_url,
    github_wheel_url,
    install_requirement_from_wheel,
    normalize_release_tag,
    wheel_filename,
)

_INFRA_INIT = Path(__file__).resolve().parents[1] / "doyoutrade" / "infra" / "__init__.py"


class InfraPackageLazyImportTests(unittest.TestCase):
    def test_infra_init_does_not_eagerly_import_qmt(self) -> None:
        # Windows installer CI runs bare unittest without OpenTelemetry / QMT.
        # Eager ``from doyoutrade.infra.qmt import ...`` in infra.__init__ used to
        # fail ``tests.test_release_wheel`` and skip publishing Setup.exe.
        text = _INFRA_INIT.read_text(encoding="utf-8")
        self.assertIn("__getattr__", text)
        self.assertIsNone(
            re.search(r"(?m)^from doyoutrade\.infra\.qmt import", text),
            "infra.__init__ must not eagerly import qmt (pulls OpenTelemetry)",
        )
        self.assertIsNone(
            re.search(r"(?m)^from doyoutrade\.infra\.qmt_proxy_client import", text),
            "infra.__init__ must not eagerly import qmt_proxy_client",
        )



class WheelNamingTests(unittest.TestCase):
    def test_wheel_filename_matches_hatch_default(self) -> None:
        self.assertEqual(
            wheel_filename("0.1.10"),
            "doyoutrade-0.1.10-py3-none-any.whl",
        )

    def test_normalize_tag_accepts_v_prefix_and_bare(self) -> None:
        self.assertEqual(normalize_release_tag("v0.1.10"), ("v0.1.10", "0.1.10"))
        self.assertEqual(normalize_release_tag("0.1.10"), ("v0.1.10", "0.1.10"))
        self.assertEqual(normalize_release_tag("V1.2.3"), ("v1.2.3", "1.2.3"))


class WheelUrlTests(unittest.TestCase):
    def test_github_wheel_url(self) -> None:
        self.assertEqual(
            github_wheel_url("v0.1.10"),
            "https://github.com/renjiegod/doyoutrade/releases/download/"
            "v0.1.10/doyoutrade-0.1.10-py3-none-any.whl",
        )

    def test_gitee_wheel_url(self) -> None:
        self.assertEqual(
            gitee_wheel_url("v0.1.10"),
            "https://gitee.com/renjie-god/doyoutrade/releases/download/"
            "v0.1.10/doyoutrade-0.1.10-py3-none-any.whl",
        )

    def test_urls_accept_bare_version(self) -> None:
        self.assertTrue(github_wheel_url("0.2.0").endswith("/v0.2.0/doyoutrade-0.2.0-py3-none-any.whl"))
        self.assertTrue(gitee_wheel_url("0.2.0").endswith("/v0.2.0/doyoutrade-0.2.0-py3-none-any.whl"))


class InstallRequirementTests(unittest.TestCase):
    def test_linux_github_wheel(self) -> None:
        self.assertEqual(
            install_requirement_from_wheel(tag="v0.2.0", platform="linux", mirror="github"),
            "doyoutrade @ https://github.com/renjiegod/doyoutrade/releases/download/"
            "v0.2.0/doyoutrade-0.2.0-py3-none-any.whl",
        )

    def test_windows_keeps_qmt_proxy_extra(self) -> None:
        self.assertEqual(
            install_requirement_from_wheel(tag="v0.2.0", platform="win32", mirror="github"),
            "doyoutrade[qmt-proxy] @ https://github.com/renjiegod/doyoutrade/releases/download/"
            "v0.2.0/doyoutrade-0.2.0-py3-none-any.whl",
        )

    def test_gitee_mirror(self) -> None:
        req = install_requirement_from_wheel(tag="v0.2.0", platform="linux", mirror="gitee")
        self.assertTrue(req.startswith("doyoutrade @ https://gitee.com/renjie-god/doyoutrade/"))
        self.assertIn("doyoutrade-0.2.0-py3-none-any.whl", req)


if __name__ == "__main__":
    unittest.main()
