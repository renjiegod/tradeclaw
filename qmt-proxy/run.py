"""
启动脚本 - 同时运行 REST API 和 gRPC 服务
"""
import os
import sys
import threading

import uvicorn

# 添加项目根目录到 Python 路径
sys.path.insert(0, os.path.dirname(__file__))


def configure_stdio_encoding():
    """Force UTF-8 stdout/stderr on Windows consoles."""
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def start_grpc():
    """启动 gRPC 服务"""
    from app.grpc_server import serve as grpc_serve
    grpc_serve()


def get_reload_config(settings):
    """Disable reload because this process also starts the gRPC server."""
    return False, None


def print_banner(settings):
    grpc_info = f"{settings.grpc_host}:{settings.grpc_port}" if settings.grpc_enabled else "未启用"
    """打印启动横幅"""
    print("\n" + "=" * 80)
    print("qmt-proxy 服务启动中...")
    print("=" * 80)
    print(f"应用名称:     {settings.app.name} v{settings.app.version}")
    print(f"运行模式:     {settings.xtquant.mode.value}")
    print(f"调试模式:     {'开启' if settings.app.debug else '关闭'}")
    print(f"允许交易:     {'是' if settings.xtquant.trading.allow_real_trading else '否'}")
    print("-" * 80)
    print(f"REST API:     http://{settings.app.host}:{settings.app.port}")
    print(f"gRPC 服务:    {grpc_info}")
    print(f"API 文档:     http://{settings.app.host}:{settings.app.port}/docs")
    print(f"日志级别:     {settings.logging.level}")
    print("=" * 80)
    print("\n提示: 使用环境变量 APP_MODE 切换运行模式")
    print("   - mock: 模拟模式，不连接 xtquant，返回模拟数据")
    print("   - dev:  开发模式，连接 xtquant，禁止真实交易")
    print("   - prod: 生产模式，连接 xtquant，允许真实交易")
    print("=" * 80 + "\n")


if __name__ == '__main__':
    configure_stdio_encoding()

    # 设置运行模式（如果未设置）
    if not os.getenv("APP_MODE"):
        os.environ["APP_MODE"] = "dev"
    
    # 加载配置（单例模式，仅加载一次）
    from app.config import get_settings
    from app.utils.logger import configure_logging
    settings = get_settings()
    
    # 初始化日志系统
    configure_logging(
        log_level=settings.logging.level,
        log_file=settings.logging.file or "logs/app.log",
        error_log_file=settings.logging.error_file or "logs/error.log",
        log_format=settings.logging.format,
        rotation=settings.logging.rotation,
        retention=settings.logging.retention,
        compression=settings.logging.compression
    )
    
    # 打印启动信息
    print_banner(settings)
    
    # 在单独的线程中启动 gRPC 服务
    if settings.grpc_enabled:
        grpc_thread = threading.Thread(target=start_grpc, daemon=True, name="gRPC-Server")
        grpc_thread.start()
    
    # 主线程运行 FastAPI
    reload_enabled, reload_includes = get_reload_config(settings)

    uvicorn.run(
        "app.main:app",
        host=settings.app.host,
        port=settings.app.port,
        reload=reload_enabled,
        reload_includes=reload_includes,
        log_level=settings.logging.level.lower(),
        access_log=True,
        timeout_keep_alive=settings.uvicorn.timeout_keep_alive,
    )
