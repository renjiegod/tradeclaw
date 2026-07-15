"""Tests for ``_extract_signal_timeline`` — per-cycle signal_tag aggregation.

Background (request1.json turn 9-13): a zero-trade backtest sent the agent
chasing the cause via a local pandas reimplementation of MACD using raw
``ewm`` chains — a path the project's own ``strategy-definition-authoring``
skill flags as a hand-rolled indicator anti-pattern. The fix is to surface
each cycle's ``Signal.tag`` decision directly on the debug view payload,
so "21 bars in, all emitted Signal.hold without any 'warmup' tag" is one
``GET /cycle-runs/<id>/debug-view`` away — no recompute needed.

The aggregation lives on ``doyoutrade.platform.service._extract_signal_timeline``.
It walks the persisted spans for the session, finds the
``strategy_runner_cycle`` event each cycle emits, and correlates back to
the owning ``cycle_runs`` row by ``trace_id``. These tests cover:

* happy path: every span carrying an event becomes one timeline entry;
* trace_id correlation: missing cycle_runs entry leaves run_id=None
  (visible absence, not silent drop);
* ordering: timeline sorts by ``cycle_time`` ascending so the agent can
  read the sequence top-to-bottom;
* absence: zero matching events returns ``[]`` (not the field omitted);
* malformed inputs: non-dict spans / non-list events are ignored gracefully.
"""

from __future__ import annotations

import unittest

from doyoutrade.platform.service import (
    _extract_signal_timeline,
    _summarize_signal_timeline,
)


def _span(
    *,
    span_id: str,
    trace_id: str,
    start_time: str = "2026-04-23T07:00:00",
    events: list[dict] | None = None,
) -> dict:
    """Build a minimal serialized-span dict matching ``_serialize_span``'s output."""

    return {
        "span_id": span_id,
        "trace_id": trace_id,
        "parent_span_id": None,
        "session_id": "backtest-test",
        "name": "strategy.runner.run",
        "span_type": "strategy",
        "start_time": start_time,
        "end_time": None,
        "duration_ms": None,
        "attributes": {"_events": events or []},
        "status": "ok",
        "span_source": "scheduled",
    }


def _strategy_runner_event(
    *,
    signals_buy: int = 0,
    signals_sell: int = 0,
    signals_hold: int = 1,
    signals_target_exposure: int = 0,
    signals_target_quantity: int = 0,
    per_symbol_tags: dict[str, str] | None = None,
    universe_size: int = 1,
    strategy_name: str = "macd_cross",
) -> dict:
    return {
        "event_type": "strategy_runner_cycle",
        "payload": {
            "strategy_class": "MacdCross",
            "strategy_name": strategy_name,
            "universe_size": universe_size,
            "signals_buy": signals_buy,
            "signals_sell": signals_sell,
            "signals_hold": signals_hold,
            "signals_target_exposure": signals_target_exposure,
            "signals_target_quantity": signals_target_quantity,
            "per_symbol_tags": per_symbol_tags or {},
        },
    }


def _cycle_run(run_id: str, trace_id: str, cycle_time: str) -> dict:
    return {
        "run_id": run_id,
        "trace_id": trace_id,
        "cycle_time": cycle_time,
        "cycle_time_utc": cycle_time,
    }


class ExtractSignalTimelineTests(unittest.TestCase):
    def test_happy_path_correlates_event_to_cycle_run(self) -> None:
        # One cycle, one span, one event — the timeline entry must carry
        # the buy/sell/hold counts, per_symbol_tags, and the run_id /
        # cycle_time of the owning cycle_run.
        trace = "a" * 32
        spans = [
            _span(
                span_id="s1",
                trace_id=trace,
                start_time="2026-04-23T07:00:00",
                events=[
                    _strategy_runner_event(
                        signals_buy=1,
                        signals_hold=0,
                        per_symbol_tags={"600522.SH": "macd_golden_cross"},
                    )
                ],
            ),
        ]
        cycle_runs = [_cycle_run("run-cycle-1", trace, "2026-04-23T07:00:00")]

        timeline = _extract_signal_timeline(spans, cycle_runs)

        self.assertEqual(len(timeline), 1)
        entry = timeline[0]
        self.assertEqual(entry["run_id"], "run-cycle-1")
        self.assertEqual(entry["cycle_time"], "2026-04-23T07:00:00")
        self.assertEqual(entry["signals_buy"], 1)
        self.assertEqual(entry["signals_hold"], 0)
        self.assertEqual(entry["signals_target_exposure"], 0)
        self.assertEqual(entry["signals_target_quantity"], 0)
        self.assertEqual(entry["per_symbol_tags"], {"600522.SH": "macd_golden_cross"})
        # Span correlation surfaces for callers that want to drill into
        # the full span (e.g. frontend "view raw trace" link).
        self.assertEqual(entry["span_id"], "s1")
        self.assertEqual(entry["trace_id"], trace)

    def test_orders_by_cycle_time_ascending(self) -> None:
        # Three cycles produced out of insertion order; the timeline must
        # come out earliest-first so the agent can read forward in time
        # rather than re-sorting client-side. The request1.json zero-trade
        # diagnosis hinges on reading the tag sequence in order.
        spans = [
            _span(
                span_id="s2",
                trace_id="t2" * 16,
                start_time="2026-04-24T07:00:00",
                events=[_strategy_runner_event()],
            ),
            _span(
                span_id="s1",
                trace_id="t1" * 16,
                start_time="2026-04-23T07:00:00",
                events=[_strategy_runner_event()],
            ),
            _span(
                span_id="s3",
                trace_id="t3" * 16,
                start_time="2026-04-25T07:00:00",
                events=[_strategy_runner_event()],
            ),
        ]
        cycle_runs = [
            _cycle_run("run-2", "t2" * 16, "2026-04-24T07:00:00"),
            _cycle_run("run-1", "t1" * 16, "2026-04-23T07:00:00"),
            _cycle_run("run-3", "t3" * 16, "2026-04-25T07:00:00"),
        ]

        timeline = _extract_signal_timeline(spans, cycle_runs)

        self.assertEqual(
            [entry["run_id"] for entry in timeline],
            ["run-1", "run-2", "run-3"],
        )

    def test_event_without_matching_cycle_run_leaves_run_id_null(self) -> None:
        # ``trace_id`` mismatch (e.g. cycle_runs row not yet persisted, or
        # span belongs to a sibling trace) must NOT silently drop the
        # entry — operators still need to see the signal counts. Surface
        # ``run_id=None`` so the gap is visible rather than swallowed.
        spans = [
            _span(
                span_id="s1",
                trace_id="orphan-trace",
                start_time="2026-04-23T07:00:00",
                events=[_strategy_runner_event(signals_buy=1, signals_hold=0)],
            ),
        ]

        timeline = _extract_signal_timeline(spans, [])

        self.assertEqual(len(timeline), 1)
        entry = timeline[0]
        self.assertIsNone(entry["run_id"])
        self.assertIsNone(entry["cycle_time"])
        self.assertEqual(entry["signals_buy"], 1)
        self.assertEqual(entry["trace_id"], "orphan-trace")

    def test_no_strategy_runner_events_returns_empty_list(self) -> None:
        # Spans that don't carry a ``strategy_runner_cycle`` event (e.g.
        # only data-provider spans) yield an empty timeline — not a
        # missing key. Empty list is the contract; absence would be
        # ambiguous between "old payload" and "strategy never ran".
        spans = [
            _span(
                span_id="data-1",
                trace_id="t1" * 16,
                events=[{"event_type": "data_provider_fetch", "payload": {}}],
            ),
        ]

        timeline = _extract_signal_timeline(spans, [])

        self.assertEqual(timeline, [])

    def test_empty_input_returns_empty_list(self) -> None:
        self.assertEqual(_extract_signal_timeline([], []), [])
        self.assertEqual(_extract_signal_timeline([], None), [])  # type: ignore[arg-type]

    def test_malformed_span_payload_is_ignored_not_raised(self) -> None:
        # Defence in depth: a span with no attributes / events isn't an
        # error, just an empty contribution. Same for an attributes-dict
        # whose ``_events`` is missing or non-list (matches the worst
        # output ``_serialize_span`` can produce when the export pipeline
        # is interrupted mid-flush). The pure helper must not raise.
        spans = [
            {  # missing attributes
                "span_id": "x",
                "trace_id": "t",
                "start_time": "2026-04-23T07:00:00",
            },
            {  # attributes present but _events absent
                "span_id": "y",
                "trace_id": "t",
                "attributes": {"foo": "bar"},
                "start_time": "2026-04-23T07:00:00",
            },
            {  # _events is not a list
                "span_id": "z",
                "trace_id": "t",
                "attributes": {"_events": "not-a-list"},
                "start_time": "2026-04-23T07:00:00",
            },
        ]

        # Cast-violating shape mirrored from production crash modes — pyright
        # doesn't see ``list[dict]`` because of the type ignore at the top
        # of the file. The runtime contract is what matters here.
        timeline = _extract_signal_timeline(spans, [])  # type: ignore[arg-type]
        self.assertEqual(timeline, [])

    def test_cycle_time_falls_back_to_span_start_when_cycle_row_absent(self) -> None:
        # Two orphan spans (no matching cycle_runs row) — ordering must
        # fall back to span_start_time so the timeline is still
        # chronologically readable.
        spans = [
            _span(
                span_id="late",
                trace_id="t-late",
                start_time="2026-04-25T07:00:00",
                events=[_strategy_runner_event()],
            ),
            _span(
                span_id="early",
                trace_id="t-early",
                start_time="2026-04-23T07:00:00",
                events=[_strategy_runner_event()],
            ),
        ]

        timeline = _extract_signal_timeline(spans, [])

        self.assertEqual([entry["span_id"] for entry in timeline], ["early", "late"])


class SummarizeSignalTimelineTests(unittest.TestCase):
    """Compact summary used for the top of debug-view payloads.

    Lives at the head of the dict so it survives tool-result truncation
    (request1.json turn 2 had a 620KB payload cut short before
    ``signal_timeline`` was reached). Summary must stay tiny — counts,
    top-5 tag maps, and time bounds — and be present even for zero-trade
    runs.
    """

    def _entry(
        self,
        *,
        cycle_time: str,
        buy: int = 0,
        sell: int = 0,
        hold: int = 0,
        target_exposure: int = 0,
        target_quantity: int = 0,
        tags: dict[str, str] | None = None,
    ) -> dict:
        return {
            "cycle_time": cycle_time,
            "signals_buy": buy,
            "signals_sell": sell,
            "signals_hold": hold,
            "signals_target_exposure": target_exposure,
            "signals_target_quantity": target_quantity,
            "per_symbol_tags": tags or {},
        }

    def test_empty_timeline_returns_zero_shape(self) -> None:
        # Even with no data, the summary must be present with a shaped
        # zero — callers (frontend, agents) rely on the structure rather
        # than the field being absent.
        summary = _summarize_signal_timeline([])
        self.assertEqual(summary["total_cycles"], 0)
        self.assertEqual(summary["total_signals_buy"], 0)
        self.assertEqual(summary["total_signals_sell"], 0)
        self.assertEqual(summary["total_signals_hold"], 0)
        self.assertEqual(summary["total_signals_target_exposure"], 0)
        self.assertEqual(summary["total_signals_target_quantity"], 0)
        self.assertEqual(summary["top_hold_tags"], {})
        self.assertEqual(summary["top_buy_tags"], {})
        self.assertEqual(summary["top_sell_tags"], {})
        self.assertEqual(summary["top_target_exposure_tags"], {})
        self.assertEqual(summary["top_target_quantity_tags"], {})
        self.assertIsNone(summary["first_cycle_time"])
        self.assertIsNone(summary["last_cycle_time"])
        self.assertIsNone(summary["first_buy_cycle_time"])
        self.assertIsNone(summary["first_sell_cycle_time"])
        self.assertIsNone(summary["first_target_exposure_cycle_time"])
        self.assertIsNone(summary["first_target_quantity_cycle_time"])
        self.assertTrue(summary["zero_trade"])

    def test_zero_trade_run_surfaces_dominant_hold_tag(self) -> None:
        # request1.json turn 4 scenario reformulated: 19 cycles all hold,
        # majority tagged 'warmup'. Summary must put 'warmup' at the top
        # of top_hold_tags so the operator immediately sees the cause.
        timeline = [
            self._entry(
                cycle_time=f"2026-04-{day:02d}T07:00:00",
                hold=1,
                tags={"600522.SH": "warmup"},
            )
            for day in range(1, 16)
        ] + [
            self._entry(
                cycle_time=f"2026-04-{day:02d}T07:00:00",
                hold=1,
                tags={"600522.SH": "no_cross"},
            )
            for day in range(16, 20)
        ]

        summary = _summarize_signal_timeline(timeline)

        self.assertEqual(summary["total_cycles"], 19)
        self.assertEqual(summary["total_signals_hold"], 19)
        self.assertEqual(summary["total_signals_buy"], 0)
        self.assertEqual(summary["total_signals_sell"], 0)
        self.assertEqual(summary["total_signals_target_exposure"], 0)
        self.assertEqual(summary["total_signals_target_quantity"], 0)
        self.assertTrue(summary["zero_trade"])
        # First entry of top_hold_tags is the dominant one.
        first_tag = next(iter(summary["top_hold_tags"]))
        self.assertEqual(first_tag, "warmup")
        self.assertEqual(summary["top_hold_tags"]["warmup"], 15)
        self.assertEqual(summary["top_hold_tags"]["no_cross"], 4)
        self.assertIsNone(summary["first_buy_cycle_time"])
        self.assertIsNone(summary["first_sell_cycle_time"])

    def test_buy_signal_anchors_first_buy_cycle_time(self) -> None:
        # request1.json turn 4 +35% scenario: warmup holds then a single
        # golden cross. ``first_buy_cycle_time`` must point at that cross
        # so an operator can correlate to the trade_fills row.
        timeline = [
            self._entry(
                cycle_time="2026-04-21T07:00:00",
                hold=1,
                tags={"600522.SH": "warmup"},
            ),
            self._entry(
                cycle_time="2026-04-22T07:00:00",
                hold=1,
                tags={"600522.SH": "no_cross"},
            ),
            self._entry(
                cycle_time="2026-04-23T07:00:00",
                buy=1,
                tags={"600522.SH": "macd_golden_cross"},
            ),
            self._entry(
                cycle_time="2026-04-24T07:00:00",
                hold=1,
                tags={"600522.SH": "no_cross"},
            ),
        ]

        summary = _summarize_signal_timeline(timeline)

        self.assertEqual(summary["total_signals_buy"], 1)
        self.assertEqual(summary["total_signals_target_exposure"], 0)
        self.assertEqual(summary["total_signals_target_quantity"], 0)
        self.assertEqual(summary["first_buy_cycle_time"], "2026-04-23T07:00:00")
        self.assertEqual(summary["first_cycle_time"], "2026-04-21T07:00:00")
        self.assertEqual(summary["last_cycle_time"], "2026-04-24T07:00:00")
        self.assertFalse(summary["zero_trade"])
        # Buy tag is bucketed into top_buy_tags, not top_hold_tags.
        self.assertEqual(summary["top_buy_tags"]["macd_golden_cross"], 1)
        self.assertNotIn("macd_golden_cross", summary["top_hold_tags"])

    def test_top_tags_capped_at_five(self) -> None:
        # Sanity bound on payload size: at most 5 entries per tag bucket.
        timeline = [
            self._entry(
                cycle_time=f"2026-04-{i + 1:02d}T07:00:00",
                hold=1,
                tags={"sym": f"tag_{i}"},
            )
            for i in range(8)
        ]

        summary = _summarize_signal_timeline(timeline)
        self.assertEqual(len(summary["top_hold_tags"]), 5)

    def test_target_exposure_signals_get_own_bucket(self) -> None:
        timeline = [
            self._entry(
                cycle_time="2026-04-21T07:00:00",
                target_exposure=1,
                tags={"600522.SH": "grid_l4"},
            ),
            self._entry(
                cycle_time="2026-04-22T07:00:00",
                target_exposure=1,
                tags={"600522.SH": "grid_l3"},
            ),
        ]
        summary = _summarize_signal_timeline(timeline)
        self.assertEqual(summary["total_signals_target_exposure"], 2)
        self.assertEqual(summary["first_target_exposure_cycle_time"], "2026-04-21T07:00:00")
        self.assertEqual(summary["top_target_exposure_tags"]["grid_l4"], 1)
        self.assertFalse(summary["zero_trade"])

    def test_target_quantity_signals_get_own_bucket(self) -> None:
        timeline = [
            self._entry(
                cycle_time="2026-04-21T07:00:00",
                target_quantity=1,
                tags={"600522.SH": "grid_qty_l4"},
            ),
            self._entry(
                cycle_time="2026-04-22T07:00:00",
                target_quantity=1,
                tags={"600522.SH": "grid_qty_l3"},
            ),
        ]
        summary = _summarize_signal_timeline(timeline)
        self.assertEqual(summary["total_signals_target_quantity"], 2)
        self.assertEqual(summary["first_target_quantity_cycle_time"], "2026-04-21T07:00:00")
        self.assertEqual(summary["top_target_quantity_tags"]["grid_qty_l4"], 1)
        self.assertFalse(summary["zero_trade"])

    def test_untagged_hold_does_not_appear_in_top_tags(self) -> None:
        # When per_symbol_tags is empty (legacy payload before the
        # runner fallback landed), the summary still counts cycles but
        # the top_hold_tags map stays empty for that entry — the runner
        # change for N2 makes new payloads always emit a tag, so this
        # only matters for backward compatibility with stored sessions.
        timeline = [
            self._entry(cycle_time="2026-04-21T07:00:00", hold=1, tags={}),
        ]
        summary = _summarize_signal_timeline(timeline)
        self.assertEqual(summary["total_signals_hold"], 1)
        self.assertEqual(summary["top_hold_tags"], {})


if __name__ == "__main__":
    unittest.main()
