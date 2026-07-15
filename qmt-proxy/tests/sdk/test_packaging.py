"""
qmt_proxy_sdk 安装与导入形态测试。

验证点与仓库布局对应：
- libs/qmt_proxy_sdk/pyproject.toml：独立包元数据，可被 pip/uv 单独安装。
- 仅将 libs（SDK 父目录）加入 PYTHONPATH 时，应能 import qmt_proxy_sdk 及 models 子模块，
  不要求把仓库根目录加入路径（模拟「只拿到 libs 目录」的消费方环境）。
"""

import logging
import os
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SDK_ROOT = PROJECT_ROOT.parent / "qmt_proxy_sdk"  # canonical location at monorepo root
SDK_PARENT = SDK_ROOT.parent


def test_sdk_has_standalone_pyproject():
    """SDK 目录下必须存在独立 pyproject.toml，与 monorepo 根 pyproject 区分打包边界。"""
    assert (SDK_ROOT / "pyproject.toml").exists(), "Standalone SDK package must define its own pyproject.toml"
    logger.info("SDK pyproject 存在: %s", SDK_ROOT / "pyproject.toml")


def test_sdk_can_import_without_repo_root_on_pythonpath(tmp_path):
    """
    子进程仅设置 PYTHONPATH=libs 父目录（即包含 qmt_proxy_sdk 包的目录），在临时 cwd 下执行 -c 脚本。

    成功则说明包名 qmt_proxy_sdk 与 qmt_proxy_sdk.models 公开符号可被正常解析。
    """
    script = (
        "import qmt_proxy_sdk; "
        "from qmt_proxy_sdk.models import AccountType, MarketDataResponse; "
        "print(qmt_proxy_sdk.__name__); "
        "print(AccountType.SECURITY.value); "
        "print(MarketDataResponse.__name__)"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SDK_PARENT)
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        env=env,
        check=False,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    assert result.stdout.strip().splitlines() == [
        "qmt_proxy_sdk",
        "SECURITY",
        "MarketDataResponse",
    ]
    logger.info("独立 PYTHONPATH=%s 子进程导入成功，stdout=%r", SDK_PARENT, result.stdout.strip())
