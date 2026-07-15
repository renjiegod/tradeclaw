#!/usr/bin/env python3
"""第一波主升加速高 → 急跌反抽：有界窗口、只量这一次反抽（杜绝骑乘拉升的浮盈假象）。

为什么需要它：
  - peak_end [高点→退潮] 测出来两策略都亏（见顶后接刀）。
  - 朴素 launch_peak [启动→高点] 看着 +18%，但几乎全是"闭仓数=0"的浮盈——
    策略在拉升初段买回踩后一路扛到高点按市值计盈，是骑乘拉升 + 幸存者偏差的假象。

正确做法（贴合用户"第一波主升加速高后的急跌"）：
  1. 客观探测每只票启动后的【第一个加速高】：自启动已涨≥ACCEL_GAIN、局部高点、
     其后≤3 根内急跌≥SHARP_DROP。
  2. 窗口 = [加速高日, 加速高日 + HORIZON 个交易日]，有界。只量这一次急跌反抽。
  3. 同时报【已平仓实现收益】与【含浮盈总收益】，浮盈占比高即提示骑乘假象。

⚠️ 幸存者偏差仍在：样本全是事后大涨的强势股，真实实时 edge 会更低；本实验只回答
   "在确认主升中、对第一次加速后急跌做反抽，是否优于见顶后接刀"。
"""

from __future__ import annotations

import csv
import json
import statistics as st
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.backtest_pullback_regime_matrix import (  # noqa: E402
    CSV_PATH, SD_SHARP, SD_MEDIUM, ROOT, _run_cli, run_backtest,
)

ACCEL_GAIN = 0.30     # 自启动到加速高的累计涨幅门槛（确认是主升加速）
SHARP_DROP = 0.10     # 加速高后≤3 根内的急跌幅度门槛
HORIZON_BARS = 20     # 加速高后给多少个交易日的有界反抽窗口
OUT = ROOT / "tmp" / "accel_rebound.json"


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
            if not (code and launch and peak):
                continue
            out.append({"code": code, "name": name, "launch": launch, "peak": peak})
    return out


def fetch_bars(code: str, start: str, end: str) -> list[dict]:
    # 数据源优先级：qmt（与回测同源、前复权）→ baostock → akshare
    path = None
    for src in ("qmt", "baostock", "akshare"):
        cmd = ["uv", "run", "doyoutrade-cli", "data", "run", code,
               "--range-start", start, "--range-end", end, "--data-source", src]
        for attempt in range(3):
            if attempt:
                time.sleep(2.0)
            payload = _run_cli(cmd, retries=1)
            if payload and payload.get("ok"):
                syms = (payload.get("data") or {}).get("symbols") or []
                if syms and syms[0].get("status") == "ok" and syms[0].get("ohlcv_path"):
                    path = syms[0]["ohlcv_path"]
                    break
        if path:
            break
    bars = []
    if path and Path(path).exists():
        with open(path, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                try:
                    bars.append({"date": datetime.strptime(r["date"], "%Y-%m-%d"),
                                 "high": float(r["high"]), "low": float(r["low"]),
                                 "close": float(r["close"])})
                except (ValueError, KeyError):
                    continue
    return bars


def first_accel_high(bars: list[dict], launch: datetime, peak: datetime):
    """返回 (加速高日期, 详情) 或 (None, note)。"""
    if len(bars) < 5:
        return None, "too_few_bars"
    li = min(range(len(bars)), key=lambda i: abs((bars[i]["date"] - launch).days))
    launch_close = bars[li]["close"]
    for i in range(li + 1, len(bars) - 1):
        if bars[i]["date"] > peak:
            break
        gain = bars[i]["high"] / launch_close - 1.0
        if gain < ACCEL_GAIN:
            continue
        lo = max(0, i - 2)
        hi = min(len(bars), i + 3)
        if bars[i]["high"] < max(bars[k]["high"] for k in range(lo, hi)):
            continue  # 非局部高
        fut = [bars[k]["close"] for k in range(i + 1, min(len(bars), i + 4))]
        if not fut:
            continue
        drop = min(fut) / bars[i]["close"] - 1.0
        if drop <= -SHARP_DROP:
            return bars[i]["date"], {"gain_from_launch": round(gain * 100, 1),
                                     "post_drop": round(drop * 100, 1)}
    return None, "no_accel_high"


def horizon_end(bars: list[dict], accel: datetime) -> str | None:
    idx = [i for i, b in enumerate(bars) if b["date"] >= accel]
    if not idx:
        return None
    ai = idx[0]
    ei = min(len(bars) - 1, ai + HORIZON_BARS)
    return bars[ei]["date"].strftime("%Y-%m-%d")


def load_cache() -> dict:
    if OUT.exists():
        try:
            return {x["code"]: x for x in json.loads(OUT.read_text())["rows"]}
        except (json.JSONDecodeError, KeyError, OSError):
            return {}
    return {}


def summarize(rows: list[dict]) -> dict:
    traded = [r for r in rows if r.get("accel_date") and r.get("sharp_ret") is not None
              and r.get("medium_ret") is not None]

    def stats(key_ret, key_trades):
        tot = [r[key_ret] for r in traded]
        if not tot:
            return {}
        pos = [x for x in tot if x > 0]
        neg = [x for x in tot if x < 0]
        # 开仓骑乘残留：0 闭仓但收益≠0 = 仓位扛到窗口末按市值计的浮盈/浮亏
        open_ride = sum(1 for r in traded
                        if (r.get(key_trades) or 0) == 0 and abs(r[key_ret]) > 1e-9)
        return {
            "n": len(tot),
            "total_avg": round(st.mean(tot), 2), "total_median": round(st.median(tot), 2),
            "win": len(pos), "loss": len(neg),
            "pf": round(sum(pos) / abs(sum(neg)), 2) if neg else None,
            "open_ride_count": open_ride,
            "best": round(max(tot), 1), "worst": round(min(tot), 1),
        }
    return {
        "horizon_bars": HORIZON_BARS, "accel_gain": ACCEL_GAIN, "sharp_drop": SHARP_DROP,
        "n_with_accel": len(traded),
        "n_no_accel": sum(1 for r in rows if not r.get("accel_date")),
        "sharp": stats("sharp_ret", "sharp_trades"),
        "medium": stats("medium_ret", "medium_trades"),
        "rows": rows,
    }


def main() -> int:
    rows_in = load_rows()
    cache = load_cache()
    print(f"accel_rebound: {len(rows_in)} stocks, horizon={HORIZON_BARS}", file=sys.stderr)
    rows: list[dict] = []
    for i, r in enumerate(rows_in, 1):
        c = cache.get(r["code"])
        if c and (c.get("accel_date") is None or c.get("sharp_ret") is not None):
            rows.append(c)
            print(f"[{i}/{len(rows_in)}] {r['name']} (cached) accel={c.get('accel_date')} "
                  f"sh={c.get('sharp_ret')} md={c.get('medium_ret')}", file=sys.stderr)
            continue
        launch = datetime.strptime(r["launch"], "%Y-%m-%d")
        peak = datetime.strptime(r["peak"], "%Y-%m-%d")
        bars = fetch_bars(r["code"], (launch - timedelta(days=8)).strftime("%Y-%m-%d"),
                          (peak + timedelta(days=15)).strftime("%Y-%m-%d"))
        accel, detail = first_accel_high(bars, launch, peak) if bars else (None, "fetch_failed")
        rec: dict = {"code": r["code"], "name": r["name"], "launch": r["launch"],
                     "peak": r["peak"], "accel_date": None, "detail": detail}
        end = horizon_end(bars, accel) if accel is not None else None
        if accel is not None and end:
            start = accel.strftime("%Y-%m-%d")
            rec["accel_date"] = start
            rec["window_end"] = end
            rec["detail"] = detail
            sh = run_backtest(SD_SHARP, r["code"], start, end)
            md = run_backtest(SD_MEDIUM, r["code"], start, end)
            rec["sharp_ret"] = sh.get("return_pct") if sh.get("ok") else None
            rec["sharp_trades"] = sh.get("closed_trades") if sh.get("ok") else None
            rec["medium_ret"] = md.get("return_pct") if md.get("ok") else None
            rec["medium_trades"] = md.get("closed_trades") if md.get("ok") else None
            print(f"[{i}/{len(rows_in)}] {r['name']} accel={start}..{end} {detail} "
                  f"sh={rec['sharp_ret']}(n={rec['sharp_trades']}) "
                  f"md={rec['medium_ret']}(n={rec['medium_trades']})", file=sys.stderr)
        else:
            print(f"[{i}/{len(rows_in)}] {r['name']} no accel-high ({detail})", file=sys.stderr)
        rows.append(rec)
        OUT.write_text(json.dumps(summarize(rows), ensure_ascii=False, indent=2), encoding="utf-8")

    rep = summarize(rows)
    OUT.write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in rep.items() if k != "rows"}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
