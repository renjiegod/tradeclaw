import tomllib
import unittest
from pathlib import Path


class PyprojectPackagingTests(unittest.TestCase):
    def test_poetry_packages_include_app_module(self):
        pyproject_path = Path(__file__).resolve().parents[2] / "pyproject.toml"
        pyproject = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))

        tool_poetry = pyproject.get("tool", {}).get("poetry", {})
        packages = tool_poetry.get("packages", [])

        self.assertIn(
            {"include": "app"},
            packages,
            "Expected Poetry packaging metadata to include the app package.",
        )


if __name__ == "__main__":
    unittest.main()
