import type { CycleRunRow, PostCycleAccount } from "../types";

/** Shared by the cycle-run list, detail panel and review panel — list rows
 * carry ``details.post_cycle_account``. Arrays are rejected: a malformed
 * non-object payload must surface as "no snapshot", not be cast through. */
export function postCycleAccountFromDetails(
  details: Record<string, unknown> | null | undefined,
): PostCycleAccount | null {
  if (!details || typeof details !== "object") return null;
  const raw = details.post_cycle_account;
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  return raw as PostCycleAccount;
}

/** One cycle run reduced to its account-snapshot equity. Shared by the review
 * panel and the task-detail metric tiles so both read the equity series the
 * same way (a parsing tweak can no longer drift between the two surfaces). */
export type AccountReviewPoint = {
  runId: string;
  cycleTime: string;
  equity: number;
  postCycle: PostCycleAccount;
};

/** Reduce raw cycle runs to the ordered series we can review: keep only rows
 * carrying a ``post_cycle_account`` with a parseable ``account.equity`` and a
 * usable cycle time, then sort ascending by cycle time (run_id breaks ties so
 * the order is deterministic). */
export function buildAccountReviewPoints(rows: CycleRunRow[]): AccountReviewPoint[] {
  const points: AccountReviewPoint[] = [];
  for (const row of rows) {
    const postCycle = postCycleAccountFromDetails(row.details);
    if (!postCycle || !postCycle.account) continue;
    const equity = Number(postCycle.account.equity);
    if (!Number.isFinite(equity)) continue;
    const cycleTime = row.cycle_time ?? row.cycle_time_utc ?? row.wall_started_at ?? null;
    if (cycleTime == null || cycleTime === "") continue;
    points.push({ runId: row.run_id, cycleTime, equity, postCycle });
  }
  points.sort((a, b) => {
    const ta = Date.parse(a.cycleTime);
    const tb = Date.parse(b.cycleTime);
    if (Number.isFinite(ta) && Number.isFinite(tb) && ta !== tb) return ta - tb;
    return a.runId.localeCompare(b.runId);
  });
  return points;
}

/** Headline account metrics for the task-detail summary tiles: first vs latest
 * account-snapshot equity (起始权益 / 当前权益 / 总盈亏). ``startEquity`` is the
 * equity of the FIRST cycle that captured a snapshot — observed starting equity,
 * NOT deposited principal (they diverge if cycles ran before the first snapshot
 * or capital was added/withdrawn). ``null`` when no cycle run carries a usable
 * snapshot — e.g. a freshly configured task that has not run a cycle yet — so
 * the caller can render an explicit "no data" state rather than a misleading 0. */
export type AccountMetricsSummary = {
  startEquity: number;
  endEquity: number;
  change: number;
  changePct: number | null;
  pointCount: number;
};

/** Single source of the headline equity math, over an already-built, sorted
 * point series. Shared by the task-detail tiles and the 复盘 period summary so
 * the two surfaces cannot drift. ``null`` for an empty series. */
export function summarizeAccountPoints(points: AccountReviewPoint[]): AccountMetricsSummary | null {
  if (points.length === 0) return null;
  const first = points[0];
  const last = points[points.length - 1];
  const change = last.equity - first.equity;
  const changePct = first.equity > 0 ? (change / first.equity) * 100 : null;
  return {
    startEquity: first.equity,
    endEquity: last.equity,
    change,
    changePct,
    pointCount: points.length,
  };
}

export function summarizeAccountMetrics(rows: CycleRunRow[]): AccountMetricsSummary | null {
  return summarizeAccountPoints(buildAccountReviewPoints(rows));
}

/** Exact money display for cycle-run cells / metrics: API decimal strings and
 * finite numbers render at 2 decimals; null/blank/non-numeric collapse to "—"
 * (a non-numeric string is echoed back unchanged so bad data stays visible).
 *
 * Distinct from the private ``fmtMoney`` above, which uses the zh-CN locale and
 * a different fallback for the position-cell preview. Kept separate on purpose. */
export function fmtMoneyExact(v: string | number | null | undefined): string {
  if (v == null) return "—";
  if (typeof v === "string") {
    const t = v.trim();
    if (!t) return "—";
    const n = Number(t);
    if (!Number.isFinite(n)) return v;
    return n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }
  if (Number.isNaN(v)) return "—";
  return v.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

/** Currency-style display for list cells; normalizes API decimal strings and JS number noise. */
function fmtMoney(n: string | number): string {
  const x = typeof n === "string" ? Number(n.trim()) : n;
  if (typeof x !== "number" || !Number.isFinite(x)) {
    return typeof n === "string" ? n : "—";
  }
  return x.toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

export type PositionListCellModel = {
  /** First line: equity + position count. */
  primary: string;
  /** Up to three symbols, ``market_value`` desc. */
  previewLine: string | null;
  /** Multi-line tooltip. */
  tooltip: string;
};

export function formatPositionListCell(account: PostCycleAccount | null): PositionListCellModel | null {
  if (!account?.account) return null;
  const eq = account.account.equity;
  const positions = [...(account.positions ?? [])];
  const n = positions.length;
  const primary = `权益 ${fmtMoney(eq)} · ${n}只`;

  positions.sort((a, b) => {
    const va = a.market_value != null ? Number(a.market_value) : 0;
    const vb = b.market_value != null ? Number(b.market_value) : 0;
    return vb - va;
  });
  const top3 = positions.slice(0, 3);
  const previewLine = top3.length ? top3.map((p) => p.symbol).join(" · ") : null;

  const tipParts: string[] = [
    `总资产 ${fmtMoney(eq)}`,
    `持仓 ${n} 只`,
    ...positions.slice(0, 20).map((p) => {
      const name = p.name ? ` ${p.name}` : "";
      const mv = p.market_value != null ? ` · 市值 ${fmtMoney(p.market_value)}` : "";
      return `${p.symbol}${name}${mv}`;
    }),
  ];
  if (positions.length > 20) {
    tipParts.push(`… 共 ${positions.length} 条`);
  }
  const tooltip = tipParts.join("\n");

  return { primary, previewLine, tooltip };
}

export type TradeOperationsCellModel = {
  lines: string[];
};

function asFiniteNumber(v: unknown): number | null {
  if (typeof v === "number" && Number.isFinite(v)) return v;
  if (typeof v === "string" && v.trim() !== "") {
    const n = Number(v.trim());
    return Number.isFinite(n) ? n : null;
  }
  return null;
}

function formatTradeLine(
  verb: "买" | "卖",
  symbol: string,
  shares: number,
  notional: number,
): string {
  const sharesStr = Math.round(shares).toLocaleString("zh-CN");
  const moneyStr = notional.toLocaleString("zh-CN", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
  return `${verb} ${symbol} ${sharesStr}股 ¥${moneyStr}`;
}

function linesFromFills(fillsRaw: unknown): string[] {
  if (!Array.isArray(fillsRaw)) return [];
  const lines: string[] = [];
  for (const raw of fillsRaw) {
    if (!raw || typeof raw !== "object") continue;
    const o = raw as Record<string, unknown>;
    const side = typeof o.side === "string" ? o.side.toLowerCase() : "";
    if (side !== "buy" && side !== "sell") continue;
    const symbol = typeof o.symbol === "string" && o.symbol ? o.symbol : "—";
    const qty = asFiniteNumber(o.quantity);
    const price = asFiniteNumber(o.price);
    if (qty == null || price == null || qty <= 0 || price <= 0) continue;
    lines.push(formatTradeLine(side === "buy" ? "买" : "卖", symbol, qty, qty * price));
  }
  return lines;
}

function linesFromPositionIntents(intentsRaw: unknown): string[] {
  if (!Array.isArray(intentsRaw)) return [];
  const lines: string[] = [];
  for (const raw of intentsRaw) {
    if (!raw || typeof raw !== "object") continue;
    const o = raw as Record<string, unknown>;
    const action = typeof o.action === "string" ? o.action.toLowerCase() : "";
    if (action !== "buy" && action !== "sell") continue;
    const symbol = typeof o.symbol === "string" && o.symbol ? o.symbol : "—";
    const amount = asFiniteNumber(o.amount);
    const priceRef = asFiniteNumber(o.price_reference);
    if (amount == null || priceRef == null || amount <= 0 || priceRef <= 0) continue;
    // OrderIntent.amount semantics: buy = notional (currency), sell = shares.
    const shares = action === "buy" ? amount / priceRef : amount;
    const notional = action === "buy" ? amount : amount * priceRef;
    lines.push(formatTradeLine(action === "buy" ? "买" : "卖", symbol, shares, notional));
  }
  return lines;
}

function linesFromLegacyDecisionExecution(details: Record<string, unknown>): string[] {
  const decisionsRaw = details.decisions;
  const executionRaw = details.decision_execution;
  if (!Array.isArray(decisionsRaw) || !Array.isArray(executionRaw)) return [];
  const lines: string[] = [];
  const count = Math.min(decisionsRaw.length, executionRaw.length);
  for (let i = 0; i < count; i += 1) {
    const decision = decisionsRaw[i];
    const execution = executionRaw[i];
    if (!decision || typeof decision !== "object") continue;
    if (!execution || typeof execution !== "object") continue;
    const d = decision as Record<string, unknown>;
    const e = execution as Record<string, unknown>;
    const action = typeof d.action === "string" ? d.action.toLowerCase() : "";
    if (action !== "buy" && action !== "sell") continue;
    const symbol = typeof d.symbol === "string" && d.symbol ? d.symbol : "—";
    const shares = asFiniteNumber(e.quantity_shares);
    const notional = asFiniteNumber(e.total_notional);
    if (shares == null || notional == null || shares <= 0 || notional <= 0) continue;
    lines.push(formatTradeLine(action === "buy" ? "买" : "卖", symbol, shares, notional));
  }
  return lines;
}

export function formatTradeOperationsFromDetails(
  details: Record<string, unknown> | null | undefined,
): TradeOperationsCellModel {
  const empty: TradeOperationsCellModel = { lines: [] };
  if (!details || typeof details !== "object") return empty;

  const fillLines = linesFromFills(details.fills);
  if (fillLines.length > 0) return { lines: fillLines };

  const intentLines = linesFromPositionIntents(details.position_intents);
  if (intentLines.length > 0) return { lines: intentLines };

  const legacyLines = linesFromLegacyDecisionExecution(details);
  if (legacyLines.length > 0) return { lines: legacyLines };

  return empty;
}
