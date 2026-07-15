"""日志按 client_id 打标 + 幂等配置（去重）测试。"""
import io

from app.utils import logger as logger_module
from app.utils.logger import DEFAULT_CLIENT_TAG, _inject_client_tag, logger


def test_inject_client_tag_inserts_before_message():
    fmt = "{time} | {level} | {name} - {message}"
    out = _inject_client_tag(fmt)
    assert "[{extra[client_id]}] {message}" in out


def test_inject_client_tag_idempotent_if_already_present():
    fmt = "{level} [{extra[client_id]}] {message}"
    assert _inject_client_tag(fmt) == fmt


def test_bound_logger_shows_client_id_and_default_tag():
    sink = io.StringIO()
    # 模拟 configure_logging 的核心：默认 extra + 带标签的 format
    logger.remove()
    logger.configure(extra={"client_id": DEFAULT_CLIENT_TAG})
    sink_id = logger.add(
        sink,
        format=_inject_client_tag("{level} | {name} - {message}"),
        level="INFO",
    )
    try:
        logger.info("general line")  # 无 bind → 默认标签
        logger.bind(client_id="dgzq_real").info("trade line")  # 终端日志
    finally:
        logger.remove(sink_id)

    text = sink.getvalue()
    assert f"[{DEFAULT_CLIENT_TAG}] general line" in text
    assert "[dgzq_real] trade line" in text


def test_configure_logging_is_idempotent(tmp_path):
    # 重置幂等标志，模拟首次配置
    logger_module._CONFIGURED = False
    logger.remove()
    log_file = tmp_path / "app.log"
    err_file = tmp_path / "err.log"

    def _count_handlers() -> int:
        return len(logger._core.handlers)

    logger_module.configure_logging(log_file=str(log_file), error_log_file=str(err_file))
    first = _count_handlers()
    # 第二、三次调用应是 no-op（不再叠加 sink）
    logger_module.configure_logging(log_file=str(log_file), error_log_file=str(err_file))
    logger_module.configure_logging(log_file=str(log_file), error_log_file=str(err_file))
    assert _count_handlers() == first

    # force=True 可强制重配（仍保持同样数量，不叠加）
    logger_module.configure_logging(log_file=str(log_file), error_log_file=str(err_file), force=True)
    assert _count_handlers() == first
