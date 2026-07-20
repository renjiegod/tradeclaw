"""DOYOUTRADE_REQUIRE_FRONTEND=1 must fail the wheel build without UI."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

try:
    from hatch_build import CustomBuildHook
except ModuleNotFoundError:  # pragma: no cover
    CustomBuildHook = None  # type: ignore[misc, assignment]


class _FakeApp:
    def display_info(self, msg: str) -> None:
        pass

    def display_warning(self, msg: str) -> None:
        pass


@unittest.skipIf(CustomBuildHook is None, "hatchling not installed")
class RequireFrontendTests(unittest.TestCase):
    def test_require_frontend_raises_when_dist_missing(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="doyoutrade-require-fe-"))
        (root / "frontend").mkdir()
        (root / "frontend" / "package.json").write_text("{}", encoding="utf-8")
        hook = CustomBuildHook(root=str(root), config={}, build_config=mock.Mock(), metadata=mock.Mock())
        hook.app = _FakeApp()
        build_data: dict[str, Any] = {}
        with mock.patch.dict(os.environ, {"DOYOUTRADE_REQUIRE_FRONTEND": "1", "DOYOUTRADE_SKIP_FRONTEND_BUILD": "1"}):
            with self.assertRaisesRegex(RuntimeError, "DOYOUTRADE_REQUIRE_FRONTEND"):
                hook._include_frontend(root, build_data)


if __name__ == "__main__":
    unittest.main()
