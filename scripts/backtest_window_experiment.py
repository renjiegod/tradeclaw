#!/usr/bin/env python3
"""窗口对照实验：反抽策略在不同入场窗口下的表现。

动机：上轮用 [高点日→退潮日]（见顶后整段下跌）测，两策略均亏 −5~6%。
用户洞见：起点不必是最终最高点，可以是"第一波主升加速高后的急跌"——
即在主升趋势途中做急跌回踩的反抽，而非在确认下跌段反复接刀。

窗口模式（策略无状态、只在"自近高点急跌"时入场，故起点提前是安全的，
入场 gate 会自动挡掉"买在拉升本身"，只接急跌回踩）：
  - peak_end     : [高点日, 退潮日]    —— 基线（见顶后下跌段）
  - launch_peak  : [启动日, 高点日]    —— 主升途中的急跌回踩（用户假设）
  - launch_end   : [启动日, 退潮日]    —— 全周期

用法：uv run python scripts/backtest_window_experiment.py launch_peak
"""

from __future__ import annotations

import csv
import json
import statistics as st
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.backtest_pullback_regime_matrix import (  # noqa: E402
    CSV_PATH, SD_SHARP, SD_MEDIUM, run_backtest, ROOT,
)

MODE = sys.argv[1] if len(sys.argv) > 1 else "launch_peak"
OUT = ROOT / "tmp" / f"window_exp_{MODE}.json"


def load_rows() -> list[dict]:
    out = []
    with CSV_PATH.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            end = (row.get("行情结束日(退潮)") or "").strip()
            if not end or "未退潮" in end or "进行中" in end:
                continue
            launch = (row.get("启动日") or "").strip()
            peak = (row.get("高点日") or "").strip()
            code = (row.get("代码") or "").strip()
            name = (row.get("名称") or "").strip()
            end_clean = end.split("(")[0].strip()
            if not (code and launch and peak and end_clean):
                continue
            out.append({"code": code, "name": name, "launch": launch,
                        "peak": peak, "end": end_clean})
    return out


def window_for(r: dict) -> tuple[str, str]:
    if MODE == "peak_end":
        return r["peak"], r["end"]
    if MODE == "launch_peak":
        return r["launch"], r["peak"]
    if MODE == "launch_end":
        return r["launch"], r["end"]
    raise SystemExit(f"unknown MODE={MODE}")


def load_cache() -> dict:
    if OUT.exists():
        try:
            return {f"{x['code']}": x for x in json.loads(OUT.read_text())["rows"]}
        except (json.JSONDecodeError, KeyError, OSError):
            return {}
    return {}


def summarize(rows: list[dict]) -> dict:
    done = [r for r in rows if r.get("sharp_ret") is not None and r.get("medium_ret") is not None]
    s = [r["sharp_ret"] for r in done]
    m = [r["medium_ret"] for r in done]

    def stats(xs):
        if not xs:
            return {}
        pos = [x for x in xs if x > 0]
        neg = [x for x in xs if x < 0]
        return {
            "n": len(xs), "avg": round(st.mean(xs), 2), "median": round(st.median(xs), 2),
            "win": len(pos), "loss": len(neg),
            "pf": round(sum(pos) / abs(sum(neg)), 2) if neg else None,
            "best": round(max(xs), 1), "worst": round(min(xs), 1),
        }
    # best-of：每只票取两策略较优（择优组合上限）
    best = [max(r["sharp_ret"], r["medium_ret"]) for r in done]
    return {
        "mode": MODE, "n": len(done),
        "sharp": stats(s), "medium": stats(m), "best_of_two": stats(best),
        "rows": rows,
    }


def main() -> int:
    rows_in = load_rows()
    cache = load_cache()
    print(f"[{MODE}] {len(rows_in)} stocks", file=sys.stderr)
    rows: list[dict] = []
    for i, r in enumerate(rows_in, 1):
        c = cache.get(r["code"])
        if c and c.get("sharp_ret") is not None and c.get("medium_ret") is not None:
            rows.append(c)
            print(f"[{i}/{len(rows_in)}] {r['name']} (cached) "
                  f"sh={c['sharp_ret']} md={c['medium_ret']}", file=sys.stderr)
            continue
        start, end = window_for(r)
        sh = run_backtest(SD_SHARP, r["code"], start, end)
        md = run_backtest(SD_MEDIUM, r["code"], start, end)
        rec = {
            "code": r["code"], "name": r["name"], "start": start, "end": end,
            "sharp_ret": sh.get("return_pct") if sh.get("ok") else None,
            "sharp_trades": sh.get("closed_trades") if sh.get("ok") else None,
            "medium_ret": md.get("return_pct") if md.get("ok") else None,
            "medium_trades": md.get("closed_trades") if md.get("ok") else None,
        }
        rows.append(rec)
        print(f"[{i}/{len(rows_in)}] {r['name']} {start}..{end} "
              f"sh={rec['sharp_ret']}(n={rec['sharp_trades']}) "
              f"md={rec['medium_ret']}(n={rec['medium_trades']})", file=sys.stderr)
        OUT.write_text(json.dumps(summarize(rows), ensure_ascii=False, indent=2), encoding="utf-8")

    rep = summarize(rows)
    OUT.write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in rep.items() if k != "rows"}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
