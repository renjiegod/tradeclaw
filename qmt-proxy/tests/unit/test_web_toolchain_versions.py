import json
from pathlib import Path


def test_web_toolchain_stays_on_pre_rolldown_major_versions():
    package_json = Path("web/package.json")
    config = json.loads(package_json.read_text(encoding="utf-8"))
    dev_dependencies = config["devDependencies"]

    assert dev_dependencies["vite"].startswith("^7."), dev_dependencies["vite"]
    assert dev_dependencies["vitest"].startswith("^3."), dev_dependencies["vitest"]
    assert dev_dependencies["@vitejs/plugin-react"].startswith("^5."), dev_dependencies["@vitejs/plugin-react"]
