from pathlib import Path


def test_trading_service_module_compiles():
    source_path = Path(__file__).resolve().parents[2] / "app" / "services" / "trading_service.py"
    source = source_path.read_text(encoding="utf-8")

    compile(source, str(source_path), "exec")
