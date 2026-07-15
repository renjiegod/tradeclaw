"""Realtime stock monitoring (股票智能盯盘).

A standalone, event-driven daemon that watches a user's stocks tick-by-tick
against declarative condition trees (preset detectors + field predicates) and
pushes alerts to a notification channel when a condition fires.

This package is deliberately decoupled from the bar-based worker cycle: monitor
rules are first-class, stock-scoped entities (``mon-`` ids) that do NOT require a
running trading task. It reuses the realtime quote stream (data layer), the
channel delivery pipeline (飞书), the watchlist symbol pool, and the
run_id/OTel/debug observability machinery — without rebuilding any of them.

Module layout (dependency order, acyclic):

- ``state``        — per-symbol intraday state (seal-volume peak, board-open flag)
- ``evaluator``    — EvalContext + condition-tree walk + field predicates
- ``presets``      — the 6 preset detectors + registry
- ``conditions``   — condition-tree JSON schema + validator (stable error_codes)
- ``observability``— centralized ``monitor.*`` span / debug-event helpers
- ``dedup``        — rising-edge + cooldown gate
- ``daemon``       — MonitorDaemon: ties the above to the quote stream + delivery
"""

PRESET_NAMES = (
    "limit_up",
    "limit_down",
    "limit_up_seal_shrink",
    "limit_down_seal_shrink",
    "limit_up_open",
    "limit_down_open",
)

__all__ = ["PRESET_NAMES"]
