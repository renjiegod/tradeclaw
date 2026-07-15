"""
SDK 测试共享配置（pytest fixtures）。

说明：
- 本目录测试针对 libs/qmt_proxy_sdk，通过 RecordingTransport / MockTransport 等模拟 HTTP 层，
  断言与 FastAPI 应用 app/routers 中注册的 REST 路径、请求体字段一致。
- autouse fixture 仅负责打日志，便于在 CI 或本地观察用例顺序；不改变被测 SDK 行为。
"""

import logging

import pytest

logger = logging.getLogger("tests.sdk")


@pytest.fixture(autouse=True)
def _log_sdk_test_boundaries(request):
    """每个用例前后记录 nodeid，便于对照服务端日志或失败堆栈定位。"""
    logger.info("开始 %s", request.node.nodeid)
    yield
    logger.info("结束 %s", request.node.nodeid)
