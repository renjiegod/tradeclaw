"""``data_sector`` operation — list boards or fetch a board's constituents.

A sibling of ``data_news`` on the sector axis. Two modes in one tool:

* **List mode** (no ``sector_names`` / ``sector_name``): return the
  available board names (optionally filtered by ``sector_type`` ∈
  ``{industry, concept}``) so the agent can discover what's screenable.
* **Members mode** (board name(s) given): fetch each board's constituents,
  write a per-board ``sector_<name>.csv`` (``code,name``) and a combined,
  de-duplicated **universe CSV** of canonical codes — the exact file
  ``stock screen --universe-file`` consumes, closing the
  sector → screen loop.

Source selection mirrors the OHLCV axis: ``--data-source auto`` walks an
akshare-first → qmt fallback chain (:func:`build_sector_provider`); an
explicit ``qmt`` / ``akshare`` pins one source.

Failure-mode discipline (per CLAUDE.md §错误可见性):

* A persistent provider error surfaces ``sector_fetch_failed`` (carrying
  the exception type); a board that resolves but is empty surfaces
  ``sector_empty`` — distinct modes, never merged.
* Per-board failures land in ``sectors[i].status == "failed"`` and never
  collapse the run. Only top-level validation sets ``is_error=True``.

Debug events (all key steps observable):

* ``operation_data_sector.request`` / ``.rejected`` / ``.failed`` / ``.created``
* ``operation_data_sector.sector.started`` / ``.completed`` / ``.failed``
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from doyoutrade.api.operations.market_data import _get_artifacts_root
from doyoutrade.debug import emit_debug_event
from doyoutrade.tools import OperationHandler, ToolResult
from doyoutrade.tools._coercion import SchemaCoercion
from doyoutrade.tools._prose import append_json_payload, format_error_text, format_unknown_args

logger = logging.getLogger(__name__)

_SUPPORTED_SECTOR_SOURCES = ("auto", "akshare", "qmt")
_SECTOR_TYPES = ("industry", "concept")
_MEMBER_CSV_COLUMNS = ("code", "name")
_PREVIEW_ROWS = 10


class _InvalidDataSectorArgument(ValueError):
    """Structured argument failure carrying a stable ``error_code``."""

    def __init__(
        self,
        error_code: str,
        message: str,
        hint: str | None = None,
        *,
        error_type: str | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.hint = hint
        self.error_type = error_type


def _safe_sector_name(name: str) -> str:
    """Filesystem-safe slug for a board name (keeps CJK, replaces the rest)."""
    slug = re.sub(r"[^\w一-鿿]+", "_", name.strip())
    return slug.strip("_") or "sector"


class DataSectorTool(OperationHandler):
    name = "data_sector"
    description = (
        "List sector / industry / concept boards, or fetch a board's "
        "constituent stocks and write a screenable universe CSV. Mirrors "
        "data_news on the sector axis; --data-source auto walks akshare → qmt."
    )
    category = "data"
    parameters = {
        "type": "object",
        "properties": {
            "sector_name": {
                "type": "string",
                "description": "Single board name to fetch members for (e.g. '白酒').",
            },
            "sector_names": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Board names to fetch members for (CSV string or JSON array).",
            },
            "sector_type": {
                "type": "string",
                "enum": list(_SECTOR_TYPES),
                "description": "Restrict to industry or concept boards. Omit to try both.",
            },
            "data_source": {
                "type": "string",
                "enum": list(_SUPPORTED_SECTOR_SOURCES),
                "default": "auto",
            },
            "limit": {"type": "integer", "minimum": 1},
            "output_path": {
                "type": "string",
                "description": "Combined universe CSV path (members mode). Defaults to the artifacts dir.",
            },
        },
        "additionalProperties": False,
    }
    coercion_rules = (
        SchemaCoercion(
            field="sector_names",
            declared_type="array",
            item_type=str,
            error_code="invalid_sector_names",
        ),
    )

    def __init__(self, *, sector_provider_factory=None) -> None:
        """``sector_provider_factory`` is for tests: ``(data_source) -> provider``.

        When ``None`` we build via ``doyoutrade.data.factory.build_sector_provider``.
        """
        self._sector_provider_factory = sector_provider_factory

    async def execute(self, **kwargs: Any) -> ToolResult:
        contract = self._enforce_kwargs_contract(kwargs)
        if contract.error is not None:
            await emit_debug_event(
                "operation_data_sector.rejected",
                {"tool": self.name, "input_keys": sorted(kwargs.keys()), "error": contract.error},
            )
            return ToolResult(
                text=format_unknown_args(
                    list(contract.error.get("unknown", [])),
                    sorted(self._allowed_top_level_kwargs()),
                    dict(contract.error.get("suggested_path") or {}),
                ),
                is_error=True,
            )
        kwargs = dict(contract.kwargs)

        # The CLI sends ``sector_names`` as a bare comma string; pull it aside
        # so the array coercion does not reject it, then re-attach.
        names_raw = kwargs.get("sector_names")
        names_is_string = isinstance(names_raw, str)
        if names_is_string:
            kwargs.pop("sector_names", None)

        coercion = self._apply_schema_coercion(kwargs)
        if coercion.error is not None:
            err = coercion.error
            await emit_debug_event("operation_data_sector.failed", {"tool": self.name, **err})
            return ToolResult(
                text=format_error_text(
                    str(err.get("error_code") or "validation_error"),
                    str(err.get("error") or "invalid input"),
                    err.get("hint") if isinstance(err.get("hint"), str) else None,
                ),
                is_error=True,
            )
        kwargs = dict(coercion.kwargs)
        if names_is_string:
            kwargs["sector_names"] = names_raw

        await emit_debug_event(
            "operation_data_sector.request",
            {"tool": self.name, "input_keys": sorted(kwargs.keys())},
        )

        try:
            normalized = self._normalize_inputs(kwargs)
        except _InvalidDataSectorArgument as exc:
            await emit_debug_event(
                "operation_data_sector.failed",
                {
                    "tool": self.name,
                    "error_code": exc.error_code,
                    "error_type": exc.error_type,
                    "message": str(exc),
                    "hint": exc.hint,
                },
            )
            return ToolResult(
                text=format_error_text(exc.error_code, str(exc), exc.hint),
                is_error=True,
            )

        # Build the provider once (reused across boards / fallback chain).
        try:
            provider = await self._build_provider(normalized["data_source"])
        except _InvalidDataSectorArgument as exc:
            await emit_debug_event(
                "operation_data_sector.failed",
                {"tool": self.name, "error_code": exc.error_code, "message": str(exc)},
            )
            return ToolResult(
                text=format_error_text(exc.error_code, str(exc), exc.hint),
                is_error=True,
            )

        try:
            if normalized["sector_names"]:
                payload = await self._run_members_mode(provider, normalized)
            else:
                payload = await self._run_list_mode(provider, normalized)
        finally:
            close = getattr(provider, "aclose", None)
            if close is not None:
                try:
                    await close()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("data_sector: provider.aclose() raised: %s", exc)

        if isinstance(payload, ToolResult):
            return payload

        header = str(payload.pop("_header"))
        return ToolResult(text=append_json_payload(header, payload), is_error=False)

    # ------------------------------------------------------------------

    def _normalize_inputs(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        data_source = str(kwargs.get("data_source") or "auto").strip().lower()
        if data_source not in _SUPPORTED_SECTOR_SOURCES:
            raise _InvalidDataSectorArgument(
                "unknown_data_source",
                f"unknown data_source {data_source!r}",
                f"use one of: {', '.join(_SUPPORTED_SECTOR_SOURCES)}",
            )

        sector_type = kwargs.get("sector_type")
        if sector_type is not None:
            sector_type = str(sector_type).strip().lower()
            if sector_type not in _SECTOR_TYPES:
                raise _InvalidDataSectorArgument(
                    "invalid_sector_type",
                    f"sector_type={sector_type!r} not in {list(_SECTOR_TYPES)}",
                )

        names: list[str] = []
        raw_names = kwargs.get("sector_names")
        if isinstance(raw_names, str):
            names.extend(p.strip() for p in raw_names.split(",") if p.strip())
        elif isinstance(raw_names, list):
            names.extend(str(p).strip() for p in raw_names if str(p).strip())
        single = kwargs.get("sector_name")
        if single is not None and str(single).strip():
            names.append(str(single).strip())
        names = list(dict.fromkeys(names))

        limit = kwargs.get("limit")
        if limit is not None:
            limit = int(limit)

        return {
            "data_source": data_source,
            "sector_type": sector_type,
            "sector_names": names,
            "limit": limit,
            "output_path": kwargs.get("output_path"),
        }

    async def _build_provider(self, data_source: str):
        if self._sector_provider_factory is not None:
            return self._sector_provider_factory(data_source)
        from doyoutrade.config import get_config
        from doyoutrade.data.account_resolution import resolve_default_market_account
        from doyoutrade.data.factory import build_sector_provider

        account = await resolve_default_market_account()
        try:
            return build_sector_provider(data_source, get_config().data, account)
        except ValueError as exc:
            raise _InvalidDataSectorArgument(
                "data_source_unavailable", str(exc),
                "configure the source (data.qmt.base_url) or use --data-source akshare",
            ) from exc

    async def _run_list_mode(self, provider, normalized: dict[str, Any]) -> dict[str, Any]:
        sector_type = normalized["sector_type"]
        try:
            names = await provider.list_sectors(sector_type=sector_type)
        except Exception as exc:
            logger.exception("data_sector list_sectors failed source=%s", normalized["data_source"])
            await emit_debug_event(
                "operation_data_sector.failed",
                {
                    "tool": self.name,
                    "error_code": "sector_fetch_failed",
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                },
            )
            return ToolResult(
                text=format_error_text(
                    "sector_fetch_failed",
                    f"failed to list sectors via data_source={normalized['data_source']!r}: {exc}",
                    "check the data_source is reachable (akshare network / qmt base_url)",
                ),
                is_error=True,
            )

        if normalized["limit"] is not None:
            names = names[: normalized["limit"]]
        await emit_debug_event(
            "operation_data_sector.created",
            {"tool": self.name, "mode": "list", "sector_count": len(names)},
        )
        return {
            "_header": (
                f"Listed {len(names)} {sector_type or 'industry+concept'} boards "
                f"via data_source={normalized['data_source']}."
            ),
            "status": "ok",
            "mode": "list",
            "data_source": normalized["data_source"],
            "sector_type": sector_type,
            "sector_count": len(names),
            "sectors": names[:_PREVIEW_ROWS * 5],
        }

    async def _run_members_mode(self, provider, normalized: dict[str, Any]) -> dict[str, Any]:
        results: list[dict[str, Any]] = []
        universe: list[str] = []
        for name in normalized["sector_names"]:
            await emit_debug_event(
                "operation_data_sector.sector.started",
                {"tool": self.name, "sector_name": name},
            )
            try:
                members = await provider.get_sector_members(
                    name, sector_type=normalized["sector_type"]
                )
            except Exception as exc:
                logger.exception("data_sector members failed sector=%s", name)
                await emit_debug_event(
                    "operation_data_sector.sector.failed",
                    {
                        "tool": self.name,
                        "sector_name": name,
                        "error_code": "sector_fetch_failed",
                        "error_type": type(exc).__name__,
                        "message": str(exc),
                    },
                )
                results.append({
                    "sector_name": name,
                    "status": "failed",
                    "error_code": "sector_fetch_failed",
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                })
                continue

            if normalized["limit"] is not None:
                members = members[: normalized["limit"]]

            if not members:
                await emit_debug_event(
                    "operation_data_sector.sector.failed",
                    {
                        "tool": self.name,
                        "sector_name": name,
                        "error_code": "sector_empty",
                    },
                )
                results.append({
                    "sector_name": name,
                    "status": "failed",
                    "error_code": "sector_empty",
                    "message": f"board {name!r} resolved but has no constituents",
                    "hint": "check the board name (use list mode to discover names) or sector_type",
                })
                continue

            csv_path = self._persist_members(name, members)
            codes = [m.code for m in members]
            universe.extend(codes)
            results.append({
                "sector_name": name,
                "status": "ok",
                "data_source": members[0].provider,
                "sector_type": members[0].sector_type,
                "member_count": len(members),
                "members_path": str(csv_path),
                "preview": [{"code": m.code, "name": m.name} for m in members[:_PREVIEW_ROWS]],
            })
            await emit_debug_event(
                "operation_data_sector.sector.completed",
                {"tool": self.name, "sector_name": name, "member_count": len(members)},
            )

        universe = list(dict.fromkeys(universe))
        universe_path = self._write_universe(universe, normalized.get("output_path"))

        successes = [r for r in results if r.get("status") == "ok"]
        failures = [r for r in results if r.get("status") == "failed"]
        status = "ok" if not failures else ("partial" if successes else "failed")

        await emit_debug_event(
            "operation_data_sector.created",
            {
                "tool": self.name,
                "mode": "members",
                "sectors_total": len(results),
                "sectors_succeeded": len(successes),
                "universe_size": len(universe),
                "universe_path": universe_path,
            },
        )
        return {
            "_header": (
                f"Resolved {len(successes)}/{len(results)} boards via "
                f"data_source={normalized['data_source']}: {len(universe)} unique "
                f"symbols → {universe_path}."
            ),
            "status": status,
            "mode": "members",
            "data_source": normalized["data_source"],
            "sectors_total": len(results),
            "sectors_succeeded": len(successes),
            "sectors_failed": len(failures),
            "universe_size": len(universe),
            "universe_path": universe_path,
            "sectors": results,
        }

    # ------------------------------------------------------------------

    def _persist_members(self, sector_name: str, members) -> Path:
        root = _get_artifacts_root()
        root.mkdir(parents=True, exist_ok=True)
        path = root / f"sector_{_safe_sector_name(sector_name)}.csv"
        import csv

        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(_MEMBER_CSV_COLUMNS)
            for m in members:
                writer.writerow([m.code, m.name])
        return path

    def _write_universe(self, codes: list[str], output_path: Any) -> str:
        root = _get_artifacts_root()
        root.mkdir(parents=True, exist_ok=True)
        if output_path:
            path = Path(str(output_path)).expanduser()
            path.parent.mkdir(parents=True, exist_ok=True)
        else:
            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            path = root / f"sector_universe_{ts}.csv"
        # One canonical symbol per line — the shape ``stock screen
        # --universe-file`` consumes.
        path.write_text("\n".join(codes) + ("\n" if codes else ""), encoding="utf-8")
        return str(path)

    def _summary_header(self, payload: dict[str, Any]) -> str:  # pragma: no cover - unused helper kept for parity
        return json.dumps(payload)


__all__ = ["DataSectorTool"]
