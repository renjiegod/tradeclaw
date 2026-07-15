"""Sync xtquant into project venv and QMT site-packages."""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import os
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
VENV_XTQUANT = PROJECT_ROOT / ".venv-windows" / "Lib" / "site-packages" / "xtquant"
QMT_ROOT = Path(os.environ.get("QMT_ROOT", r"C:\你的券商QMT交易端"))
QMT_XTQUANT = QMT_ROOT / "bin.x64" / "Lib" / "site-packages" / "xtquant"
DOWNLOAD_URL = "https://dict.thinktrader.net/packages/xtquant_250807.rar"
SEVEN_ZIP = Path(r"C:\Program Files\7-Zip\7z.exe")


def download(url: str, destination: Path) -> None:
    print(f"Downloading {url} -> {destination}")
    with urllib.request.urlopen(url, timeout=120) as response:
        destination.write_bytes(response.read())


def extract(archive: Path, destination: Path) -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    cmd = [str(SEVEN_ZIP), "x", str(archive), f"-o{destination}", "-y"]
    print("Extracting:", " ".join(cmd))
    subprocess.run(cmd, check=True)

    candidates = [p for p in destination.rglob("xtquant") if (p / "__init__.py").is_file()]
    if not candidates:
        raise FileNotFoundError(f"xtquant package not found under {destination}")
    return candidates[0]


def replace_tree(source: Path, target: Path) -> None:
    backup = target.with_name(target.name + ".bak")
    if target.exists():
        if backup.exists():
            shutil.rmtree(backup)
        print(f"Backing up {target} -> {backup}")
        target.rename(backup)
    print(f"Copying {source} -> {target}")
    shutil.copytree(source, target)


def verify(path: Path) -> None:
    init_file = path / "__init__.py"
    lines = init_file.read_text(encoding="utf-8", errors="ignore").splitlines()
    version_line = next((line for line in lines if "__version__" in line), lines[0])
    print(f"Installed at {path}")
    print(f"Version line: {version_line.strip()}")


def main() -> int:
    if not SEVEN_ZIP.exists():
        print(f"7-Zip not found at {SEVEN_ZIP}", file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory(prefix="xtquant-sync-") as tmp:
        tmp_path = Path(tmp)
        archive = tmp_path / "xtquant_250807.rar"
        extract_root = tmp_path / "extract"

        download(DOWNLOAD_URL, archive)
        source_xtquant = extract(archive, extract_root)

        replace_tree(source_xtquant, VENV_XTQUANT)
        verify(VENV_XTQUANT)

        QMT_XTQUANT.parent.mkdir(parents=True, exist_ok=True)
        replace_tree(source_xtquant, QMT_XTQUANT)
        verify(QMT_XTQUANT)

    print("xtquant sync completed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
