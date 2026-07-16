"""`doyoutrade-cli portfolio ...` subcommands (功能 6 — 图片 / CSV 智能导入持仓).

``portfolio import-csv``   → import a broker 交割单 CSV into the knowledge
                             ``trades/<broker>/<YYYY-MM>.csv`` partition
                             (pure local filesystem — like ``knowledge`` /
                             ``schema``, the envelope is built locally).
``portfolio import-image`` → vision extraction needs a wired multimodal model
                             adapter, which the local CLI process does not
                             have (model routing lives in the API server, and
                             this iteration adds no API endpoint). The command
                             returns a structured ``not_available_via_cli``
                             error pointing at the assistant tool
                             ``import_positions_from_image``.

待集成 NOTE: this group is NOT yet registered in ``doyoutrade/cli/main.py``
(file owned by a parallel change). Registration diff is listed in the
delivery notes:

    from doyoutrade.cli.commands.portfolio import portfolio
    cli.add_command(portfolio)
"""

from __future__ import annotations

import click

from doyoutrade.cli._envelope import (
    EXIT_FAILURE,
    EXIT_OK,
    Meta,
    error_envelope,
    exit_code_for_error,
    success_envelope,
)
from doyoutrade.cli._format import write_envelope
from doyoutrade.cli._invoke import read_session_meta


def _fmt(ctx: click.Context) -> str:
    root = ctx.find_root()
    return root.obj.get("fmt", "json") if root.obj else "json"


@click.group()
def portfolio() -> None:
    """Portfolio import (持仓截图 / 交割单 CSV → 知识库)."""


@portfolio.command("import-csv")
@click.option("--file", "file_path", required=True, help="交割单 CSV 文件路径。")
@click.option("--broker", "broker", required=True, help="券商名（作为 trades/ 下目录名）。")
@click.option(
    "--dry-run",
    "dry_run",
    is_flag=True,
    default=False,
    help="只预演不落盘：返回将新增 / 重复的计数，不写文件、不刷新索引。",
)
def portfolio_import_csv(file_path: str, broker: str, dry_run: bool) -> None:
    """Import a broker-statement CSV into knowledge ``trades/<broker>/<YYYY-MM>.csv``.

    Reuses the attribution parser (multi-broker column aliases), dedupes on
    ``date+symbol+side+price+qty``, refreshes the knowledge index, and
    smoke-checks readability via ``read_trade_attribution``.

    Example::

        doyoutrade-cli portfolio import-csv --file ~/Downloads/交割单.csv --broker huatai
    """
    ctx = click.get_current_context()
    fmt = _fmt(ctx)
    meta: Meta = read_session_meta()

    from doyoutrade.portfolio_import.csv_import import import_trades_csv

    try:
        result = import_trades_csv(file_path, broker=broker, dry_run=dry_run)
    except Exception as exc:
        envelope = error_envelope(
            error_code="csv_import_failed",
            error_type=type(exc).__name__,
            message=str(exc) or f"{type(exc).__name__} (no message)",
            meta=meta,
        )
        write_envelope(envelope, fmt=fmt)
        ctx.exit(EXIT_FAILURE)
        return

    if result.get("status") != "ok":
        error_code = str(result.get("error_code") or "csv_import_failed")
        envelope = error_envelope(
            error_code=error_code,
            message=str(result.get("message") or "CSV import failed"),
            extra={
                key: value
                for key, value in result.items()
                if key not in ("status", "error_code", "message")
            },
            meta=meta,
        )
        write_envelope(envelope, fmt=fmt)
        ctx.exit(exit_code_for_error(error_code))
        return

    verb = "dry-run: would import" if dry_run else "imported"
    summary = (
        f"{verb} {result['appended_total']} fill(s), "
        f"skipped {result['duplicates_skipped']} duplicate(s), "
        f"{len(result.get('unparsed') or [])} unparsed row(s)"
    )
    envelope = success_envelope(dict(result), summary, meta=meta)
    write_envelope(envelope, fmt=fmt)
    ctx.exit(EXIT_OK)


@portfolio.command("import-image")
@click.option("--file", "file_path", required=True, help="持仓截图路径。")
@click.option("--mime", "mime_type", default=None, help="图片 MIME 类型（可省略）。")
def portfolio_import_image(file_path: str, mime_type: str | None) -> None:
    """Vision position extraction — not available from the local CLI.

    The CLI process has no model routing / multimodal adapter; extraction runs
    in the assistant runtime via the ``import_positions_from_image`` tool.
    This command always returns a structured ``not_available_via_cli`` error
    so callers get a machine-readable redirect instead of a silent no-op.
    """
    ctx = click.get_current_context()
    fmt = _fmt(ctx)
    meta: Meta = read_session_meta()

    envelope = error_envelope(
        error_code="not_available_via_cli",
        message=(
            "portfolio import-image needs a multimodal model adapter, which the "
            "local CLI process does not have"
        ),
        hint=(
            "ask the assistant to run the import_positions_from_image tool on "
            f"this file instead (file_path={file_path!r}"
            + (f", mime_type={mime_type!r}" if mime_type else "")
            + ")"
        ),
        meta=meta,
    )
    write_envelope(envelope, fmt=fmt)
    ctx.exit(EXIT_FAILURE)


__all__ = ["portfolio"]
