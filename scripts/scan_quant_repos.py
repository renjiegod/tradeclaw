#!/usr/bin/env python3

from __future__ import annotations

import concurrent.futures
import json
import os
import pathlib
import re
import subprocess
import sys
from typing import Any


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SURVEY_PATH = REPO_ROOT / "github-open-source-quant-survey.md"
CACHE_ROOT = pathlib.Path(os.environ.get("QUANT_REPO_CACHE", "/tmp/tradeclaw-quant-repos"))
OUTPUT_PATH = pathlib.Path(
    os.environ.get("QUANT_REPO_OUTPUT", "/tmp/tradeclaw-quant-repo-scan.json")
)

ENTRY_PATTERN = re.compile(r"\[[^\]]+\]\(https://github\.com/([^)]+)\)")

IGNORE_TOP_LEVEL = {
    ".github",
    ".devcontainer",
    ".vscode",
    "docs",
    "doc",
    "documentation",
    "examples",
    "example",
    "notebooks",
    "notebook",
    "tests",
    "test",
    "assets",
    "images",
    "img",
    "scripts",
    "benchmarks",
    "benchmark",
    ".circleci",
    ".azure-pipelines",
}

DEPENDENCY_FILES = [
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "requirements.txt",
    "requirements-dev.txt",
    "requirements_test.txt",
    "environment.yml",
    "Pipfile",
    "poetry.lock",
    "Cargo.toml",
    "package.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "go.mod",
    "pom.xml",
    "build.gradle",
    "Makefile",
    "Dockerfile",
]

README_CANDIDATES = [
    "README.md",
    "README.rst",
    "README.txt",
    "readme.md",
    "readme.rst",
]

SIGNAL_PACKAGES = {
    "torch": "PyTorch",
    "pytorch": "PyTorch",
    "tensorflow": "TensorFlow",
    "jax": "JAX",
    "gym": "OpenAI Gym",
    "gymnasium": "Gymnasium",
    "ray": "Ray",
    "stable-baselines": "Stable-Baselines",
    "stable_baselines": "Stable-Baselines",
    "lightgbm": "LightGBM",
    "xgboost": "XGBoost",
    "catboost": "CatBoost",
    "pandas": "pandas",
    "polars": "Polars",
    "numpy": "NumPy",
    "scipy": "SciPy",
    "numba": "Numba",
    "plotly": "Plotly",
    "bokeh": "Bokeh",
    "matplotlib": "Matplotlib",
    "dash": "Dash",
    "fastapi": "FastAPI",
    "flask": "Flask",
    "django": "Django",
    "streamlit": "Streamlit",
    "gradio": "Gradio",
    "celery": "Celery",
    "sqlalchemy": "SQLAlchemy",
    "duckdb": "DuckDB",
    "redis": "Redis",
    "dask": "Dask",
    "ccxt": "CCXT",
    "binance": "Binance API",
    "alpaca": "Alpaca API",
    "ibapi": "IB API",
    "interactivebrokers": "IB API",
    "ib_insync": "IB API",
    "fix": "FIX",
    "websocket": "WebSocket",
    "asyncio": "asyncio",
    "tokio": "Tokio",
    "pyo3": "PyO3",
    "rust": "Rust",
    "cython": "Cython",
    "ta-lib": "TA-Lib",
    "talib": "TA-Lib",
    "transformers": "Transformers",
    "openai": "OpenAI",
    "langchain": "LangChain",
}

SIGNAL_KEYWORDS = {
    "reinforcement learning": "强化学习",
    "deep reinforcement learning": "深度强化学习",
    "event-driven": "事件驱动",
    "backtest": "回测",
    "live trading": "实盘",
    "paper trading": "模拟盘",
    "market making": "做市",
    "arbitrage": "套利",
    "data pipeline": "数据管线",
    "factor": "因子研究",
    "portfolio": "组合管理",
    "execution": "执行层",
    "broker": "券商接入",
    "exchange": "交易所接入",
    "agent": "Agent",
    "llm": "LLM",
}


def run(*args: str, cwd: pathlib.Path | None = None, check: bool = True) -> str:
    proc = subprocess.run(
        args,
        cwd=str(cwd) if cwd else None,
        text=True,
        capture_output=True,
        check=False,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(f"command failed: {' '.join(args)}\n{proc.stderr}")
    return proc.stdout


def read_blob(repo_dir: pathlib.Path, path: str) -> str:
    try:
        return run("git", "-C", str(repo_dir), "show", f"HEAD:{path}")
    except RuntimeError:
        return ""


def list_tree(repo_dir: pathlib.Path, path: str | None = None) -> list[str]:
    target = "HEAD" if path is None else f"HEAD:{path}"
    try:
        output = run("git", "-C", str(repo_dir), "ls-tree", "--name-only", target)
    except RuntimeError:
        return []
    return [line.strip() for line in output.splitlines() if line.strip()]


def looks_like_source_dir(name: str) -> bool:
    lower = name.lower()
    if lower in IGNORE_TOP_LEVEL:
        return False
    if lower.startswith("."):
        return False
    if lower in {"ci", "infra", "deploy", "deployment"}:
        return False
    return True


def normalize_line(line: str) -> str:
    line = re.sub(r"!\[[^\]]*\]\([^)]+\)", "", line)
    line = re.sub(r"\[[^\]]+\]\([^)]+\)", "", line)
    line = re.sub(r"<[^>]+>", "", line)
    line = line.replace("|", " ").strip()
    return re.sub(r"\s+", " ", line)


def readme_summary(repo_dir: pathlib.Path) -> list[str]:
    for candidate in README_CANDIDATES:
        content = read_blob(repo_dir, candidate)
        if not content:
            continue
        lines: list[str] = []
        for raw in content.splitlines():
            stripped = raw.strip()
            if not stripped:
                continue
            if stripped.startswith(("#", "[!", "![", "<!--")):
                continue
            clean = normalize_line(stripped)
            if not clean or len(clean) < 20:
                continue
            lines.append(clean)
            if len(lines) >= 6:
                break
        return lines
    return []


def detect_signals(text: str) -> list[str]:
    found: list[str] = []
    lower = text.lower()
    for token, label in SIGNAL_PACKAGES.items():
        if token in lower and label not in found:
            found.append(label)
    for token, label in SIGNAL_KEYWORDS.items():
        if token in lower and label not in found:
            found.append(label)
    return found


def dependency_summary(repo_dir: pathlib.Path, top_level: list[str]) -> dict[str, Any]:
    files = [name for name in top_level if name in DEPENDENCY_FILES]
    sln_files = [name for name in top_level if name.endswith(".sln")]
    csproj_files = [name for name in top_level if name.endswith(".csproj")]
    selected = files + sln_files[:3] + csproj_files[:3]
    snippets: dict[str, str] = {}
    signals: list[str] = []
    for path in selected[:8]:
        content = read_blob(repo_dir, path)
        if not content:
            continue
        snippet = "\n".join(content.splitlines()[:80])
        snippets[path] = snippet
        for signal in detect_signals(snippet):
            if signal not in signals:
                signals.append(signal)
    return {
        "files": selected,
        "signals": signals,
        "snippets": snippets,
    }


def collect_source_dirs(repo_dir: pathlib.Path, top_level: list[str]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for name in top_level:
        if not looks_like_source_dir(name):
            continue
        children = list_tree(repo_dir, name)
        if not children:
            continue
        result[name] = children[:15]
        if len(result) >= 8:
            break
    return result


def infer_kind(repo: str, top_level: list[str], source_dirs: dict[str, list[str]], readme: list[str]) -> list[str]:
    labels: list[str] = []
    joined = "\n".join(readme + top_level + list(source_dirs.keys())).lower()
    rules = [
        ("reinforcement", "RL/训练框架"),
        ("gym", "环境模拟"),
        ("backtest", "回测引擎"),
        ("event", "事件驱动引擎"),
        ("broker", "券商/交易通道"),
        ("exchange", "交易所接入"),
        ("data", "数据管线"),
        ("factor", "因子研究"),
        ("notebook", "Notebook/教程"),
        ("awesome", "资源索引"),
        ("agent", "Agent/自动化"),
        ("market making", "做市/套利"),
    ]
    for token, label in rules:
        if token in joined and label not in labels:
            labels.append(label)
    if not labels:
        labels.append("通用量化工具")
    return labels


def ensure_repo(repo: str) -> pathlib.Path:
    owner, name = repo.split("/", 1)
    target = CACHE_ROOT / f"{owner}__{name}"
    if target.exists():
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    url = f"https://github.com/{repo}.git"
    run(
        "git",
        "clone",
        "--depth",
        "1",
        "--filter=blob:none",
        url,
        str(target),
    )
    return target


def analyze_repo(repo: str) -> dict[str, Any]:
    repo_dir = ensure_repo(repo)
    top_level = list_tree(repo_dir)
    deps = dependency_summary(repo_dir, top_level)
    readme = readme_summary(repo_dir)
    source_dirs = collect_source_dirs(repo_dir, top_level)
    readme_signals = detect_signals("\n".join(readme))
    commit_date = run("git", "-C", str(repo_dir), "log", "-1", "--format=%cs").strip()
    return {
        "repo": repo,
        "url": f"https://github.com/{repo}",
        "cached_at": str(repo_dir),
        "latest_commit_date": commit_date,
        "top_level": top_level[:30],
        "readme_summary": readme,
        "dependency_files": deps["files"],
        "dependency_signals": deps["signals"],
        "readme_signals": readme_signals,
        "source_dirs": source_dirs,
        "inferred_kinds": infer_kind(repo, top_level, source_dirs, readme),
    }


def parse_repos() -> list[str]:
    text = SURVEY_PATH.read_text()
    repos: list[str] = []
    for match in ENTRY_PATTERN.findall(text):
        if match not in repos:
            repos.append(match)
    return repos


def main() -> int:
    repos = parse_repos()
    results: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        future_map = {pool.submit(analyze_repo, repo): repo for repo in repos}
        for future in concurrent.futures.as_completed(future_map):
            repo = future_map[future]
            try:
                result = future.result()
            except Exception as exc:  # noqa: BLE001
                result = {"repo": repo, "error": str(exc)}
            results.append(result)
            print(f"scanned {repo}", file=sys.stderr)
    results.sort(key=lambda item: item["repo"])
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(results, indent=2, ensure_ascii=False))
    print(str(OUTPUT_PATH))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
