#!/usr/bin/env python3
"""急跌/中跌反抽策略 — 判断正确率 → 总盈亏比矩阵.

流程：
  1. 从 ~/.doyoutrade/knowledge/cycles/强势股时间线.csv 取每只已退潮票的 [高点日, 退潮日] 窗口。
  2. 客观给回调形态打标签（急跌 / 中跌）：用 akshare OHLCV 量出"高点 → 第一个主要低点"的
     交易日数，≤sharp_max 为急跌，≥medium_min 为中跌（对齐两策略 peak_age 窗口）。
  3. 对每只票在 [高点日, 退潮日] 上分别回测急跌策略与中跌策略（qmt）。
  4. 模拟人工判断正确率 p：判断对→用匹配 regime 的策略，判断错→用另一策略。
     每只票期望收益 r_i(p)=p·matched+(1-p)·mismatched；
     组合平均收益=mean(r_i)，盈亏比=Σ正/|Σ负|，并给 avg盈/avg亏。
  5. 输出 JSON（含逐只明细），并把中间结果增量落盘，便于长跑中断后续看。

数据源拆分：回测走 qmt（backtest 引擎里稳定、最快）；客观打标签走 akshare
（data run 里 qmt 不吃日期区间、baostock 偶发网络错误，akshare 稳定返回 ohlcv_path）。
"""

from __future__ import annotations

import csv
import json
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CSV_PATH = Path.home() / ".doyoutrade/knowledge/cycles/强势股时间线.csv"
OUT_PATH = ROOT / "tmp" / "pullback_regime_matrix.json"

SD_SHARP = "sd-f6587dfcb91d"   # 急跌反抽 sharp_drop_rebound
SD_MEDIUM = "sd-7f3e08739c6b"  # 中跌反抽 medium_drop_rebound

# regime 客观划分（交易日，从高点起算到第一个主要低点）
SHARP_MAX_DAYS = 5    # ≤5 个交易日见底 → 急跌
MEDIUM_MIN_DAYS = 6   # ≥6 → 中跌
CORRECTION_MIN_DROP = 0.06   # 低点相对高点至少跌 6% 才算一次有效回调
MEDIUM_MAX_CAL_DAYS = 35     # 低点出现在高点后这么多自然日内才视为本段回调
REBOUND_WIN = 5              # 局部低点后看几根判断是否出现可交易反弹
REBOUND_MIN = 0.08           # 反弹幅度阈值：低点后收盘需反弹≥8% 才算"可交易反抽底"


@dataclass
class StockWindow:
    code: str
    name: str
    peak_date: str
    end_date: str


@dataclass
class Row:
    code: str
    name: str
    peak_date: str
    end_date: str
    regime: str | None = None          # 'sharp' | 'medium' | None
    correction_days: int | None = None
    drop_pct: float | None = None
    sharp_ret: float | None = None
    medium_ret: float | None = None
    sharp_trades: int | None = None
    medium_trades: int | None = None
    sharp_plr: float | None = None     # profit_loss_ratio (trade-level)
    medium_plr: float | None = None
    sharp_ok: bool = False
    medium_ok: bool = False
    label_note: str = ""

    def as_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


def load_windows() -> list[StockWindow]:
    rows: list[StockWindow] = []
    with CSV_PATH.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            end = (row.get("行情结束日(退潮)") or "").strip()
            if not end or "未退潮" in end or "进行中" in end:
                continue
            peak = (row.get("高点日") or "").strip()
            code = (row.get("代码") or "").strip()
            name = (row.get("名称") or "").strip()
            if not code or not peak:
                continue
            end_clean = end.split("(")[0].strip()
            try:
                datetime.strptime(peak, "%Y-%m-%d")
                datetime.strptime(end_clean, "%Y-%m-%d")
            except ValueError:
                continue
            rows.append(StockWindow(code=code, name=name, peak_date=peak, end_date=end_clean))
    return rows


def _run_cli(cmd: list[str], retries: int = 1, sleep_s: float = 2.0) -> dict | None:
    last = None
    for attempt in range(retries):
        if attempt:
            time.sleep(sleep_s)
        proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
        out = proc.stdout.strip().splitlines()
        if not out:
            last = {"ok": False, "error": proc.stderr[-300:]}
            continue
        try:
            payload = json.loads(out[-1])
        except json.JSONDecodeError:
            last = {"ok": False, "error": out[-1][:300]}
            continue
        if payload.get("ok"):
            return payload
        last = payload
    return last


def label_regime(w: StockWindow) -> tuple[str | None, int | None, float | None, str]:
    """用 akshare OHLCV 客观判定回调形态。返回 (regime, correction_days, drop_pct, note)."""
    peak_dt = datetime.strptime(w.peak_date, "%Y-%m-%d")
    fetch_start = (peak_dt - timedelta(days=8)).strftime("%Y-%m-%d")
    fetch_end = (peak_dt + timedelta(days=MEDIUM_MAX_CAL_DAYS + 10)).strftime("%Y-%m-%d")
    cmd = [
        "uv", "run", "doyoutrade-cli", "data", "run", w.code,
        "--range-start", fetch_start, "--range-end", fetch_end,
        "--data-source", "akshare",
    ]
    # akshare 间歇性网络故障：symbol 级失败也要重试
    path = None
    for attempt in range(6):
        if attempt:
            time.sleep(3.0)
        payload = _run_cli(cmd, retries=1)
        if not payload or not payload.get("ok"):
            continue
        syms = (payload.get("data") or {}).get("symbols") or []
        if syms and syms[0].get("status") == "ok" and syms[0].get("ohlcv_path"):
            path = syms[0]["ohlcv_path"]
            break
    if not path or not Path(path).exists():
        return None, None, None, "fetch_failed"

    bars: list[dict] = []
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            try:
                bars.append({
                    "date": datetime.strptime(r["date"], "%Y-%m-%d"),
                    "high": float(r["high"]),
                    "low": float(r["low"]),
                    "close": float(r["close"]),
                })
            except (ValueError, KeyError):
                continue
    if len(bars) < 4:
        return None, None, None, "too_few_bars"

    # 定位操作高点：peak_date 附近 ±2 个交易日里 high 最大的一根
    nearest = min(range(len(bars)), key=lambda i: abs((bars[i]["date"] - peak_dt).days))
    lo = max(0, nearest - 2)
    hi = min(len(bars), nearest + 3)
    peak_idx = max(range(lo, hi), key=lambda i: bars[i]["high"])
    peak_high = bars[peak_idx]["high"]

    # 高点后 MEDIUM_MAX_CAL_DAYS 自然日内的候选区间
    peak_day = bars[peak_idx]["date"]
    cand = [
        i for i in range(peak_idx + 1, len(bars))
        if (bars[i]["date"] - peak_day).days <= MEDIUM_MAX_CAL_DAYS
    ]
    if not cand:
        return None, None, None, "no_post_peak_bars"

    # 找"第一个可交易的反抽底"：局部低点 + 自高点已跌≥阈值 + 其后 REBOUND_WIN 根内
    # 收盘反弹≥REBOUND_MIN。这一天到高点的交易日数 = correction_days，决定急跌/中跌。
    note = "first_tradeable_low"
    first_low = None
    for i in cand:
        drop_i = bars[i]["close"] / peak_high - 1.0
        if drop_i > -CORRECTION_MIN_DROP:
            continue
        prev_c = bars[i - 1]["close"]
        next_c = bars[i + 1]["close"] if i + 1 < len(bars) else bars[i]["close"]
        is_local_min = bars[i]["close"] <= prev_c and bars[i]["close"] <= next_c
        if not is_local_min:
            continue
        future = [bars[k]["close"] for k in range(i + 1, min(len(bars), i + 1 + REBOUND_WIN))]
        rebound = (max(future) / bars[i]["close"] - 1.0) if future else 0.0
        if rebound >= REBOUND_MIN:
            first_low = i
            break
    if first_low is None:
        # 无有效反抽底 → 退化为全窗最低收盘（仍给出形态，但标注）
        first_low = min(cand, key=lambda i: bars[i]["close"])
        note = "global_min_fallback"

    drop = bars[first_low]["close"] / peak_high - 1.0
    correction_days = first_low - peak_idx

    if drop > -CORRECTION_MIN_DROP:
        return None, correction_days, round(drop * 100, 2), "drop_too_shallow"
    regime = "sharp" if correction_days <= SHARP_MAX_DAYS else "medium"
    return regime, correction_days, round(drop * 100, 2), note


def run_backtest(defn_id: str, symbol: str, start: str, end: str) -> dict:
    cmd = [
        "uv", "run", "doyoutrade-cli", "backtest", "run",
        "--definition", defn_id, "--universe", symbol,
        "--range-start", start, "--range-end", end,
        "--data-provider", "qmt", "--no-debug", "--timeout", "240",
    ]
    payload = _run_cli(cmd, retries=5, sleep_s=6.0)
    if not payload or not payload.get("ok"):
        return {"ok": False, "error": (payload or {}).get("error")}
    summary = (payload.get("data") or {}).get("summary") or {}

    def _f(v):
        return None if v is None else float(v)

    return {
        "ok": True,
        "return_pct": _f(summary.get("return_pct")),
        "win_rate": _f(summary.get("win_rate")),
        "closed_trades": summary.get("trade_count_closed"),
        "profit_loss_ratio": _f(summary.get("profit_loss_ratio")),
        "max_drawdown_pct": _f(summary.get("max_drawdown_pct")),
    }


def compute_report(rows: list[Row]) -> dict:
    labeled = [r for r in rows if r.regime in ("sharp", "medium")]
    valid = [r for r in labeled if r.sharp_ok and r.medium_ok
             and r.sharp_ret is not None and r.medium_ret is not None]
    n = len(valid)

    def matched(r: Row) -> float:
        return r.sharp_ret if r.regime == "sharp" else r.medium_ret  # type: ignore

    def mismatched(r: Row) -> float:
        return r.medium_ret if r.regime == "sharp" else r.sharp_ret  # type: ignore

    def at_accuracy(p: float) -> dict:
        rets = [p * matched(r) + (1.0 - p) * mismatched(r) for r in valid]
        avg = sum(rets) / n if n else 0.0
        pos = [x for x in rets if x > 0]
        neg = [x for x in rets if x < 0]
        pf = (sum(pos) / abs(sum(neg))) if neg else (float("inf") if pos else 0.0)
        avg_win = (sum(pos) / len(pos)) if pos else 0.0
        avg_loss = (sum(neg) / len(neg)) if neg else 0.0
        plr = (avg_win / abs(avg_loss)) if avg_loss else (float("inf") if avg_win else 0.0)
        return {
            "accuracy": p,
            "avg_return_pct": round(avg, 2),
            "win_count": len(pos),
            "loss_count": len(neg),
            "profit_factor": round(pf, 2) if pf != float("inf") else "inf",
            "avg_win_pct": round(avg_win, 2),
            "avg_loss_pct": round(avg_loss, 2),
            "profit_loss_ratio": round(plr, 2) if plr != float("inf") else "inf",
        }

    oracle = (sum(max(r.sharp_ret, r.medium_ret) for r in valid) / n) if n else 0.0  # type: ignore
    worst = (sum(min(r.sharp_ret, r.medium_ret) for r in valid) / n) if n else 0.0   # type: ignore
    always_sharp = (sum(r.sharp_ret for r in valid) / n) if n else 0.0  # type: ignore
    always_medium = (sum(r.medium_ret for r in valid) / n) if n else 0.0  # type: ignore
    avg_matched = (sum(matched(r) for r in valid) / n) if n else 0.0
    avg_mismatched = (sum(mismatched(r) for r in valid) / n) if n else 0.0

    return {
        "n_total_windows": len(rows),
        "n_labeled": len(labeled),
        "n_valid_both_backtests": n,
        "regime_counts": {
            "sharp": sum(1 for r in labeled if r.regime == "sharp"),
            "medium": sum(1 for r in labeled if r.regime == "medium"),
        },
        "avg_matched_ret_pct": round(avg_matched, 2),
        "avg_mismatched_ret_pct": round(avg_mismatched, 2),
        "accuracy_curve": [at_accuracy(p) for p in (0.5, 0.6, 0.7, 0.8, 0.9, 1.0)],
        "oracle_pick_best_pct": round(oracle, 2),
        "worst_pick_pct": round(worst, 2),
        "always_sharp_pct": round(always_sharp, 2),
        "always_medium_pct": round(always_medium, 2),
        "rows": [r.as_dict() for r in rows],
    }


def _load_cache() -> dict[str, dict]:
    """断点续跑：读旧 JSON，把 (code@peak) → 已完成行 索引出来。"""
    if not OUT_PATH.exists():
        return {}
    try:
        prev = json.loads(OUT_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return {f"{r['code']}@{r['peak_date']}": r for r in prev.get("rows", [])}


def label_only() -> int:
    """只跑 akshare 客观打标签（不碰 qmt），产出回调形态分类 + 缓存 regime。"""
    windows = load_windows()
    cache = _load_cache()
    rows: list[Row] = []
    for i, w in enumerate(windows, 1):
        key = f"{w.code}@{w.peak_date}"
        cached = cache.get(key)
        r = Row(code=w.code, name=w.name, peak_date=w.peak_date, end_date=w.end_date)
        if cached and cached.get("regime"):
            r.regime = cached.get("regime")
            r.correction_days = cached.get("correction_days")
            r.drop_pct = cached.get("drop_pct")
            r.label_note = cached.get("label_note") or "cached"
            # 保留已有回测结果
            for fld in ("sharp_ret", "medium_ret", "sharp_trades", "medium_trades",
                        "sharp_plr", "medium_plr", "sharp_ok", "medium_ok"):
                if cached.get(fld) is not None:
                    setattr(r, fld, cached[fld])
        else:
            r.regime, r.correction_days, r.drop_pct, r.label_note = label_regime(w)
        print(f"[{i}/{len(windows)}] {w.name} {w.code} regime={r.regime} "
              f"cdays={r.correction_days} drop={r.drop_pct} ({r.label_note})", file=sys.stderr)
        rows.append(r)
        OUT_PATH.write_text(
            json.dumps(compute_report(rows), ensure_ascii=False, indent=2), encoding="utf-8"
        )
    print(json.dumps(compute_report(rows), ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    if "--label-only" in sys.argv:
        return label_only()
    windows = load_windows()
    cache = _load_cache()
    print(f"Loaded {len(windows)} 已退潮 windows from {CSV_PATH}; cache={len(cache)} rows",
          file=sys.stderr)
    rows: list[Row] = []
    for i, w in enumerate(windows, 1):
        key = f"{w.code}@{w.peak_date}"
        cached = cache.get(key)
        # 已完成（双回测 ok + 标签 ok）→ 直接复用，避免重跑
        if (cached and cached.get("sharp_ok") and cached.get("medium_ok") and cached.get("regime")
                and cached.get("sharp_ret") is not None and cached.get("medium_ret") is not None):
            r = Row(code=w.code, name=w.name, peak_date=w.peak_date, end_date=w.end_date)
            for fld in Row.__dataclass_fields__:
                if fld in cached and cached[fld] is not None:
                    setattr(r, fld, cached[fld])
            rows.append(r)
            print(f"[{i}/{len(windows)}] {w.name} {w.code} (cached) regime={r.regime} "
                  f"sharp={r.sharp_ret} medium={r.medium_ret}", file=sys.stderr)
            OUT_PATH.write_text(
                json.dumps(compute_report(rows), ensure_ascii=False, indent=2), encoding="utf-8"
            )
            continue

        r = Row(code=w.code, name=w.name, peak_date=w.peak_date, end_date=w.end_date)
        # 标签可复用缓存（akshare 抓过且成功）
        if cached and cached.get("regime"):
            r.regime = cached.get("regime")
            r.correction_days = cached.get("correction_days")
            r.drop_pct = cached.get("drop_pct")
            r.label_note = cached.get("label_note") or "cached"
        else:
            r.regime, r.correction_days, r.drop_pct, r.label_note = label_regime(w)
        print(f"[{i}/{len(windows)}] {w.name} {w.code} {w.peak_date}..{w.end_date} "
              f"regime={r.regime} cdays={r.correction_days} drop={r.drop_pct} ({r.label_note})",
              file=sys.stderr)
        sh = run_backtest(SD_SHARP, w.code, w.peak_date, w.end_date)
        md = run_backtest(SD_MEDIUM, w.code, w.peak_date, w.end_date)
        r.sharp_ok, r.medium_ok = sh.get("ok", False), md.get("ok", False)
        r.sharp_ret, r.medium_ret = sh.get("return_pct"), md.get("return_pct")
        r.sharp_trades, r.medium_trades = sh.get("closed_trades"), md.get("closed_trades")
        r.sharp_plr, r.medium_plr = sh.get("profit_loss_ratio"), md.get("profit_loss_ratio")
        print(f"      sharp_ret={r.sharp_ret} (n={r.sharp_trades})  "
              f"medium_ret={r.medium_ret} (n={r.medium_trades})", file=sys.stderr)
        rows.append(r)
        # 增量落盘
        OUT_PATH.write_text(
            json.dumps(compute_report(rows), ensure_ascii=False, indent=2), encoding="utf-8"
        )

    report = compute_report(rows)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
