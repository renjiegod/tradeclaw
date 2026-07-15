"""Stock symbol lookup tool.

Backs the same data sources as the frontend ``/stocks`` page:
- ``search_instrument_universe`` (akshare A-share listing — full name↔code map)
- ``list_instrument_catalog`` (locally-synced catalog — what's tradable in this
  instance)

Exposed so the agent stops inventing tickers ("贵州茅台 = 600519.SS" 之类的猜
测) and instead resolves canonical symbols (``600519.SH``) via a real lookup.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from doyoutrade.tools import OperationHandler, ToolResult
from doyoutrade.tools._prose import (
    append_json_payload,
    format_error_text,
    format_unknown_args,
)

if TYPE_CHECKING:
    from doyoutrade.persistence.repositories import SqlAlchemyInstrumentCatalogRepository


def _contract_error_to_result(
    tool: OperationHandler,
    contract_error: dict[str, Any],
    error_kind: str | None,
) -> ToolResult:
    if error_kind == "unknown_arguments":
        text = format_unknown_args(
            list(contract_error.get("unknown", [])),
            sorted(tool._allowed_top_level_kwargs()),
            dict(contract_error.get("suggested_path") or {}),
        )
    else:
        text = format_error_text(
            "validation_error",
            str(
                contract_error.get("message")
                or contract_error.get("error")
                or "validation failed"
            ),
        )
    return ToolResult(text=text, is_error=True)


class LookupStockSymbolTool(OperationHandler):
    name = "lookup_stock_symbol"
    description = (
        "Resolve a Chinese stock to its canonical DoYouTrade symbol "
        "(e.g. ``600519.SH``, ``000001.SZ``, ``430047.BJ``). "
        "Use this BEFORE writing any stock code into a strategy / instance / "
        "task / cron payload — do not guess or pattern-match a suffix yourself. "
        "Two sources, same shape: ``local_catalog`` (default — locally-synced "
        "``instrument_catalog`` table, zero network) and ``akshare_a`` (live "
        "akshare A-share listing, used as a fallback / refresh source). "
        "Accepts Chinese name, partial name, 6-digit numeric code, or "
        "canonical symbol as ``q``. Returns ``items`` with ``symbol`` / "
        "``name`` / ``market``. Known ``error_code``: ``missing_query`` "
        "when ``q`` is empty; ``unknown_source`` when ``source`` is not "
        "a registered listing source; ``lookup_failed`` for upstream "
        "listing errors; ``catalog_unavailable`` when ``source='local_catalog'`` "
        "is requested but no catalog repository is wired into this caller."
    )
    category = "agent"
    parameters = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "q": {
                "type": "string",
                "description": (
                    "Search text — Chinese name, partial name, 6-digit code, "
                    "or canonical ``CODE.EXCHANGE`` symbol."
                ),
            },
            "limit": {
                "type": "integer",
                "description": "Max matches to return (1-50).",
                "default": 20,
                "minimum": 1,
                "maximum": 50,
            },
            "source": {
                "type": "string",
                "description": (
                    "Listing source id. ``local_catalog`` (default) reads "
                    "the locally-synced catalog table. ``akshare_a`` queries "
                    "the live akshare A-share listings (沪/深/京)."
                ),
                "default": "local_catalog",
                "enum": ["local_catalog", "akshare_a"],
            },
        },
        "required": ["q"],
    }

    def __init__(
        self,
        instrument_catalog_repository: "SqlAlchemyInstrumentCatalogRepository | None" = None,
    ) -> None:
        self._instrument_catalog_repository = instrument_catalog_repository

    async def execute(self, **kwargs: Any) -> ToolResult:
        contract = self._enforce_kwargs_contract(kwargs)
        if contract.error is not None:
            return _contract_error_to_result(self, contract.error, contract.error_kind)
        kwargs = contract.kwargs

        q_raw = kwargs.get("q")
        if not isinstance(q_raw, str) or not q_raw.strip():
            return ToolResult(
                text=format_error_text(
                    "missing_query",
                    "q is required — pass a name, partial name, or code.",
                ),
                is_error=True,
            )
        q = q_raw.strip()

        limit_raw = kwargs.get("limit", 20)
        try:
            limit = int(limit_raw)
        except (TypeError, ValueError):
            return ToolResult(
                text=format_error_text(
                    "validation_error",
                    f"limit must be an integer (got {limit_raw!r}).",
                ),
                is_error=True,
            )
        if limit < 1:
            limit = 1
        if limit > 50:
            limit = 50

        source_raw = kwargs.get("source", "local_catalog")
        if not isinstance(source_raw, str) or not source_raw.strip():
            source = "local_catalog"
        else:
            source = source_raw.strip()

        from doyoutrade.data.instrument_universe import (
            ALLOWED_INSTRUMENT_SOURCES,
            search_instrument_universe,
        )

        if source not in ALLOWED_INSTRUMENT_SOURCES:
            allowed = ", ".join(sorted(ALLOWED_INSTRUMENT_SOURCES))
            return ToolResult(
                text=format_error_text(
                    "unknown_source",
                    f"source={source!r} is not registered.",
                    f"allowed sources: {allowed}",
                ),
                is_error=True,
            )

        if source == "local_catalog" and self._instrument_catalog_repository is None:
            return ToolResult(
                text=format_error_text(
                    "catalog_unavailable",
                    "source='local_catalog' requires a wired instrument_catalog_repository.",
                    "this in-process caller did not provide one; retry with source='akshare_a' "
                    "or invoke via doyoutrade-cli (which wires the runtime repository).",
                ),
                is_error=True,
            )

        try:
            result = await search_instrument_universe(
                source=source,
                q=q,
                limit=limit,
                instrument_catalog_repository=self._instrument_catalog_repository,
            )
        except ValueError as exc:
            return ToolResult(
                text=format_error_text("validation_error", str(exc)),
                is_error=True,
            )
        except Exception as exc:
            return ToolResult(
                text=format_error_text(
                    "lookup_failed",
                    f"{type(exc).__name__}: {exc}",
                ),
                is_error=True,
            )

        items = result.get("items") if isinstance(result, dict) else []
        if not isinstance(items, list):
            items = []

        if not items:
            header = (
                f"No matches for q={q!r} in source={source!r}. "
                "Try a different keyword (Chinese name, partial code, or full "
                "6-digit code); do not invent a suffix."
            )
        else:
            lines = [
                f"Found {len(items)} match(es) for q={q!r} (source={source}):"
            ]
            for it in items:
                sym = it.get("symbol", "?")
                name = it.get("name", "")
                market = it.get("market", "")
                lines.append(f"- {sym}  {name} ({market})")
            header = "\n".join(lines)

        payload = {
            "status": "ok",
            "source": source,
            "q": q,
            "limit": limit,
            "items": items,
        }
        return ToolResult(text=append_json_payload(header, payload))
