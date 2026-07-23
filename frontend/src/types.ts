export type ConsolePageKey =
  | "agents"
  | "cron_jobs"
  | "channels"
  | "assistant"
  | "swarm"
  | "tasks"
  | "accounts"
  | "stocks"
  | "watchlist"
  | "stock_monitor"
  | "market_review"
  | "strategies"
  | "approvals"
  | "model_invocations"
  | "settings_models"
  | "settings"
  | "knowledge"
  | "data_console";

/** Swarm worker（任务）实时状态。与后端 SwarmTaskRecord.status 对齐。 */
export type SwarmWorkerStatus =
  | "pending"
  | "blocked"
  | "in_progress"
  | "completed"
  | "failed"
  | "cancelled";

export type SwarmRunStatus =
  | "pending"
  | "running"
  | "completed"
  | "failed"
  | "cancelled";

/** preset 团队摘要（GET /swarm/presets）。 */
export type SwarmPresetSummary = {
  name: string;
  title: string;
  description: string;
  agent_count: number;
  variables: Array<{ name: string; description?: string; required?: boolean } | string>;
};

/** 一个 swarm run 中的任务节点视图。 */
export type SwarmTaskView = {
  task_id: string;
  agent_id: string;
  status: SwarmWorkerStatus;
  depends_on: string[];
  summary: string | null;
  error: string | null;
  session_id: string | null;
  started_at: string | null;
  completed_at: string | null;
  worker_iterations: number;
};

/** 一个完整 swarm run（GET /swarm/runs/{id}）。 */
export type SwarmRun = {
  id: string;
  preset_name: string;
  status: SwarmRunStatus;
  user_vars: Record<string, string>;
  provider: string | null;
  model: string | null;
  final_report: string | null;
  total_input_tokens: number;
  total_output_tokens: number;
  error: string | null;
  created_at: string;
  completed_at: string | null;
  tasks: SwarmTaskView[];
};

/** SSE 事件 payload（/swarm/runs/{id}/events/stream）。 */
export type SwarmEvent = {
  type: string;
  agent_id: string | null;
  task_id: string | null;
  timestamp: string;
  [key: string]: unknown;
};

/** One mock-account position seed (used when ``mode === "mock"``). */
export type AccountMockPosition = {
  symbol: string;
  quantity: number;
  cost_price: number;
};

/** Row from ``GET /accounts`` — a broker / mock trading account. */
export type Account = {
  id: string;
  name: string;
  mode: "live" | "mock";
  base_url: string;
  token: string | null;
  timeout_seconds: number;
  /** Broker trading account id. */
  qmt_account_id: string | null;
  /** Which QMT terminal (client_id) on a multi-terminal qmt-proxy to route to
   * (sent as the X-QMT-Terminal header). null → proxy's default terminal. */
  qmt_terminal_id: string | null;
  session_id: string | null;
  mock_cash: number;
  mock_equity: number;
  mock_positions: AccountMockPosition[];
  is_default: boolean;
  enabled: boolean;
  created_at: string | null;
  updated_at: string | null;
};

export type AccountListResponse = { items: Account[] };

export type SystemState = {
  kill_switch_enabled: boolean;
  task_count: number;
  running_count: number;
};

export type BacktestSummaryEquityPoint = {
  /** ISO-8601 UTC timestamp of the bar close. */
  t: string;
  /** Decimal-string equity value at the bar close. */
  equity: string;
};

export type BacktestSummaryFinalPosition = {
  symbol: string;
  name: string | null;
  /** Whole-share integer (consistent with ``PostCycleAccountPositionRow``). */
  quantity: number;
  available: number | null;
  cost_price: string;
  last_price: string | null;
  market_value: string | null;
  /** Position weight as a decimal-string percentage (``market_value /
   * ending_equity * 100``), or null when the position is unpriced or
   * ending_equity is zero. */
  weight_pct: string | null;
};

/** Per-symbol closed-trade breakdown emitted by ``summary.by_symbol``.
 *
 * Sorted server-side by descending ``|pnl|`` so the top-impact symbols come
 * first. All money/ratio fields are decimal strings (see
 * ``decimal_to_json_str``). Open lots are intentionally excluded — this is
 * closed-trade-only so PnL/win_rate are fully realized. */
export type BacktestSummarySymbolStat = {
  symbol: string;
  trade_count_closed: number;
  /** Signed money string (gross PnL of closed FIFO round-trips). */
  pnl: string;
  /** ``0..1`` ratio string; render via ``win_rate * 100%``. */
  win_rate: string;
  win_rate_sample_size: number;
  avg_holding_trading_days: string;
};

/** Per-exit-reason closed-trade breakdown emitted by ``summary.by_exit_reason``.
 *
 * Parallel to ``by_symbol`` but keyed by why each position was exited
 * (``signal`` / ``stop_loss`` / ``take_profit`` / ``trailing_stop`` / ``roi`` /
 * ``circuit_breaker``). All money/ratio fields are decimal strings (see
 * ``decimal_to_json_str``). Closed-trade-only so PnL/win_rate are fully
 * realized. */
export type BacktestSummaryExitReasonStat = {
  exit_reason: string;
  trade_count_closed: number;
  /** Signed money string (gross PnL of closed FIFO round-trips). */
  pnl: string;
  /** ``0..1`` ratio string; render via ``win_rate * 100%``. */
  win_rate: string;
  win_rate_sample_size: number;
  avg_holding_trading_days: string;
};

/** Per-entry-tag (factor) closed-trade breakdown emitted by ``summary.by_tag``.
 *
 * Parallel to ``by_symbol`` but keyed by the entry factor identifier each
 * position was tagged with (an author-defined string like
 * ``"ma_cross+rsi_ok"``). All money/ratio fields are decimal strings (see
 * ``decimal_to_json_str``). Closed-trade-only so PnL/win_rate are fully
 * realized. */
export type BacktestSummaryTagStat = {
  tag: string;
  trade_count_closed: number;
  /** Signed money string (gross PnL of closed FIFO round-trips). */
  pnl: string;
  /** ``0..1`` ratio string; render via ``win_rate * 100%``. */
  win_rate: string;
  win_rate_sample_size: number;
  avg_holding_trading_days: string;
};

/** Persisted JSON shape of ``tasks.backtest_summary``.
 *
 * The list endpoint omits ``equity_curve`` to keep ``GET /tasks`` cheap. The
 * detail endpoint (``GET /tasks/{task_id}``) returns the full payload. The
 * ``equity_curve_meta`` breadcrumb is always present so the UI can flag
 * downsampled curves and show the original length. */
export type BacktestSummary = {
  schema_version: 1;
  run_id: string;
  range_start_utc: string;
  range_end_utc: string;
  bar_interval: string;
  completed_at: string;
  starting_equity: string;
  ending_equity: string;
  /** Bare percent string (e.g. ``"0.20"``); divide by 100 only when computing ratios. */
  return_pct: string;
  final_cash: string;
  final_market_value: string;
  final_positions: BacktestSummaryFinalPosition[];
  trade_count_closed: number;
  trade_count_open: number;
  /** Total executed fills (every buy AND sell); the user-facing 「交易次数」. */
  fills_count: number;
  /** ``0..1`` ratio (decimal string); render as ``win_rate * 100%``.
   *
   * Computed mark-to-market: closed FIFO trades plus any still-open lot whose
   * symbol has a known ``last_price`` in ``final_positions``. Use
   * ``win_rate_sample_size`` to render 「—」 when the denominator is 0. */
  win_rate: string;
  /** Closed trades + priced open lots that contributed to ``win_rate``. */
  win_rate_sample_size: number;
  avg_holding_trading_days: string;
  /** Closed trades + every open lot included in the holding-period mean. */
  avg_holding_sample_size: number;
  /** Bare percent string; ``"0"`` when no drawdown. */
  max_drawdown_pct: string;
  max_drawdown_peak_at: string | null;
  max_drawdown_trough_at: string | null;
  max_drawdown_peak_equity: string | null;
  max_drawdown_trough_equity: string | null;
  equity_curve_meta: { downsampled: boolean; raw_length: number };
  /** Detail API returns this; list API strips it. */
  equity_curve?: BacktestSummaryEquityPoint[];

  // ---- Optional risk-adjusted return metrics (additive, may be absent on
  // summaries persisted before the metric extension landed). ``null`` here
  // means "undefined" (e.g. Sharpe is undefined when stdev == 0). Render as
  // 「—」, not ``0``. ----
  annual_return_pct?: string | null;
  volatility_annual_pct?: string | null;
  sharpe?: string | null;
  sortino?: string | null;
  calmar?: string | null;

  // ---- Optional closed-trade aggregates (``null`` when there are no
  // closed trades, or no losses for ``profit_factor``). ----
  profit_factor?: string | null;
  avg_win_pnl?: string | null;
  /** Negative or zero money string when there were losing trades. */
  avg_loss_pnl?: string | null;
  profit_loss_ratio?: string | null;
  max_consecutive_losses?: number;

  /** Per-symbol breakdown (closed FIFO trades only); pre-sorted by
   * descending ``|pnl|``. Absent on summaries persisted before the metric
   * extension landed; empty array when ``trade_count_closed == 0``. */
  by_symbol?: BacktestSummarySymbolStat[];

  /** Per-exit-reason breakdown (closed FIFO trades only); empty array when no
   * exit was categorized, and absent on summaries persisted before the
   * exit_reason feature landed. */
  by_exit_reason?: BacktestSummaryExitReasonStat[];

  /** Per-entry-factor breakdown (closed FIFO trades only); empty array when no
   * entry was tagged, and absent on summaries persisted before the by_tag
   * feature landed. */
  by_tag?: BacktestSummaryTagStat[];

  // ---- Warmup diagnostics (additive; ``null`` when the strategy /
  // runtime did not report them). Kept on the summary for retrospective
  // analysis; not used to fire an anomaly. The legacy
  // ``warmup_insufficient`` flag compared ``bars_total`` (report-window
  // trading days) to ``startup_history`` (bars the strategy needs for
  // indicator warmup) — different things — and produced a false positive
  // any time the user asked for a window shorter than ``startup_history``.
  // The truthful preload-failure signal is the SDK runner's per-cycle
  // ``strategy_base_history_insufficient`` debug event. ----

  /** ``Strategy.startup_history`` for the definition that ran — the
   * number of bars the strategy needs before its indicators are valid. */
  startup_history?: number | null;
  /** Trading-bar count the scheduler actually walked across the
   * requested range. */
  bars_total?: number | null;
};

export type TriggerScheduleKind = "interval" | "cron" | "at" | "backtest_range";
export type ExecutionIntent = "trade" | "signal_only";
export type DeliveryMode = "none" | "card" | "prose";
export type TriggerStatus = "active" | "paused" | "exhausted" | "error";
export type NoSignalMode = "silent" | "brief" | "full";

export interface DeliveryTarget {
  kind: "session" | "channel";
  session_id?: string;
  origin?: boolean;
  /** Registered channel record id (the bot). Required for kind='channel'. */
  channel_id?: string;
  /** Feishu group chat id (``oc_…``); where a channel push actually lands. */
  chat_id?: string;
  /** Display name of the target chat, persisted for UI rendering. */
  chat_name?: string;
  channel_type?: string;
}

/** Backend `/version` response — package + git provenance of the running build, so issue reports can state exactly which build was hit. */
export interface VersionInfo {
  package_version: string;
  engine_version: string;
  git_tag?: string | null;
  git_commit?: string | null;
  git_commit_short?: string | null;
  git_dirty?: boolean | null;
}

/** One selectable Feishu push target = (bot × group it belongs to).
 * Returned by ``GET /assistant/feishu/chats`` to populate the trigger channel
 * picker. ``error`` is set (with empty chat_id) when a bot's chat listing failed
 * (e.g. missing ``im:chat`` scope). */
export interface FeishuChatOption {
  channel_id: string;
  channel_name: string;
  chat_id: string;
  name: string;
  error?: string;
}

export type FeishuChatListResponse = { items: FeishuChatOption[] };

export interface TriggerDelivery {
  mode: DeliveryMode;
  target?: DeliveryTarget;
  no_signal_mode?: NoSignalMode;
  composer_agent_id?: string;
}

/** One row in ``task_triggers`` — a child schedule + execution intent +
 * delivery descriptor owned by a Task. ID prefix ``trg-``. */
export interface TaskTrigger {
  id: string; // trg-...
  task_id: string;
  name: string;
  enabled: boolean;
  status: TriggerStatus;
  schedule_kind: TriggerScheduleKind;
  interval_seconds: number | null;
  cron_expression: string | null;
  timezone: string;
  at_iso: string | null;
  range_start: string | null;
  range_end: string | null;
  bar_interval: string | null;
  trading_session: string | null;
  delete_after_run: boolean;
  execution_intent: ExecutionIntent;
  delivery_json: TriggerDelivery | null;
  last_fired_at: string | null;
  next_fire_at: string | null;
  last_run_id: string | null;
  last_error: string;
  created_at: string;
  updated_at: string;
}

export type TaskTriggerListResponse = { triggers: TaskTrigger[] };

export type TaskStatus = {
  task_id: string;
  name: string;
  mode: "paper" | "live" | "backtest" | "signal_only" | string;
  description: string;
  status: "configured" | "running" | "paused" | "stopped" | "error" | "completed" | string;
  cycles: number | null;
  last_error: string;
  data_provider: string | null;
  data_provider_effective: string;
  /** Tradable universe for data stack / proposals; empty means no symbols this cycle. */
  universe: string[];
  /** Display name of the bound strategy definition; null when unbound or unresolved. */
  strategy_name?: string | null;
  settings: Record<string, unknown> | null;
  /** Populated after a backtest task finalizes (success or failure with partial data). */
  backtest_summary?: BacktestSummary | null;
  created_at: string;
  updated_at: string;
};

export type TaskDuplicatePreset = {
  name: string;
  mode: "paper" | "live" | "backtest" | "signal_only" | string;
  description: string;
  data_provider: string | null;
  universe_symbols: string[];
  strategy?: TaskStrategyBinding;
};

export type TaskListResponse = {
  items: TaskStatus[];
  total: number;
  limit: number;
  offset: number;
};

export type AssistantSession = {
  session_id: string;
  title: string;
  status: "idle" | "running" | "error" | string;
  agent_id: string;
  config: Record<string, unknown>;
  source_channel?: {
    id: string;
    name: string | null;
    type: string | null;
  } | null;
  created_at: string;
  updated_at: string;
  last_attempt_id: string | null;
};

export type AssistantSessionListResponse = {
  items: AssistantSession[];
  total: number;
  limit: number;
  offset: number;
};

// A blocking tool-call approval awaiting a human decision. Delivered live
// via the `approval.requested` SSE event; resolved through
// POST /assistant/approvals/{approval_id}/resolve.
export type AssistantPendingApproval = {
  approval_id: string;
  session_id: string;
  attempt_id?: string;
  run_id?: string;
  tool: string;
  rule_key: string;
  description: string;
  command_preview: string;
  timeout_seconds: number;
  allow_always?: boolean;
  suggested_prefix?: string;
  created_at: string;
};

export type AssistantApprovalAction =
  | "approve_once"
  | "approve_always"
  | "approve_persist"
  | "reject";

export type AssistantUserQuestionOption = {
  label: string;
  description?: string | null;
};

// Content block for an ask_user_question. The web UI renders the options as
// buttons; a click resolves the suspended tool wait via the answer endpoint
// (fizz-style tool_result) — no synthetic user message. Once answered, the
// backend stamps `answered`/`selected`/`custom` onto the block so a page
// reload rebuilds the read-only in-card recap.
export type AssistantUserQuestionBlock = {
  type: "user_question";
  question_id: string;
  question: string;
  header?: string | null;
  options: AssistantUserQuestionOption[];
  multi_select?: boolean;
  answered?: boolean;
  selected?: string[] | null;
  custom?: string | null;
  answer_source?: string | null;
};

// A file attached to a user message. The client never holds the server's
// absolute path — only the opaque `file_id` (returned by /upload) plus display
// metadata. The backend resolves file_id -> absolute path server-side and
// injects it into the model-visible text; it never appears in `content`.
export type MessageAttachment = {
  file_id: string;
  filename: string;
  mime_type?: string | null;
  size_bytes?: number;
};

export type AssistantMessage = {
  message_id: string;
  session_id: string;
  role: "user" | "assistant" | "system" | string;
  content: string;
  created_at: string;
  linked_attempt_id: string | null;
  metadata: Record<string, unknown> & {
    // Files the user attached to this message (rendered as filename chips, never
    // as a raw path). Present on user messages that carried uploads.
    attachments?: MessageAttachment[];
    thinking?: string;
    // Set on the assistant message persisted when a run fails mid-stream (e.g. a
    // model ReadTimeout). `partial` marks that `content` holds streamed-so-far text
    // rather than a synthesized error notice. `error_type` is the wrapped cause
    // (ReadTimeout / APITimeoutError / ...), distinguishing failure modes.
    failed?: boolean;
    partial?: boolean;
    error?: string;
    error_type?: string;
    // Set when the assistant loop exhausted its agent-configured `max_turns`
    // budget while the model was still issuing tool calls (never reached a
    // turn with no further tool_calls). `content` is then a distinct cutoff
    // notice, not a truncated answer.
    max_turns_reached?: boolean;
    thinking_blocks?: Array<{ turn?: number; content: string }>;
    content_blocks?: Array<
      | { type: "thinking"; turn?: number; content: string }
      | {
          type: "tool_call";
          tool_call_id: string;
          name?: string;
          arguments?: Record<string, unknown>;
          category?: string | null;
          status?: "pending" | "running" | "completed" | "error";
          result_preview?: string;
          is_error?: boolean;
        }
      | { type: "text"; content: string }
      | AssistantUserQuestionBlock
    >;
  };
};

export type AssistantEvent = {
  event_id: string;
  session_id: string;
  event_type: string;
  payload: Record<string, unknown>;
  created_at: string;
};

export type AssistantSendMessageResponse = {
  session: AssistantSession;
  messages: AssistantMessage[];
  trace_id: string | null;
  lifecycle_command?: {
    command: "new" | string;
    previous_session_id?: string;
    new_session_id?: string;
  };
};

export type AssistantStopResponse = {
  stopped: boolean;
  /** True when the session was marked running at stop time (had an in-flight attempt). */
  active?: boolean;
};

export type PendingApproval = {
  approval_id: string;
  intent_id: string;
  /** ISO timestamp. May be null for legacy rows persisted before enrichment. */
  created_at?: string | null;
  /** ISO timestamp. */
  expires_at?: string | null;
  /** pending / approved / rejected / expired */
  status?: string | null;
  /** Execution mode, e.g. "live". */
  mode?: string | null;
  task_id?: string | null;
  run_id?: string | null;
  account_id?: string | null;
  symbol?: string | null;
  /** Instrument display name (工商银行) resolved from the catalog; pairs with symbol. */
  symbol_name?: string | null;
  /** "buy" | "sell" */
  action?: string | null;
  /** Decimal money string, e.g. "10000.00". Do not parseFloat for any decision. */
  notional?: string | null;
  resolver_id?: string | null;
  /** web / api / feishu_card */
  decision_source?: string | null;
  /** ISO timestamp. */
  dispatched_at?: string | null;
  /** ISO timestamp. */
  decided_at?: string | null;
  /** ISO timestamp the row reached a terminal state (approved/rejected/expired). */
  resolved_at?: string | null;
  /** Rejection / expiry reason; empty for pending and approved rows. */
  reason?: string | null;
  // --- 信号 + order context, parsed from the held intent + its cycle digest.
  // Display-only; makes the web/Chat card as rich as the pure signal digest and
  // identical to the Feishu card. ---
  /** Human-readable reason the strategy proposed this order. */
  rationale?: string | null;
  /** Per-signal factor tag from the strategy. */
  signal_tag?: string | null;
  /** Strategy class that produced the order. */
  strategy_tag?: string | null;
  /** Limit price reference (decimal string; do not parseFloat for decisions). */
  price_reference?: string | null;
  /** Order type, e.g. "limit" / "market". */
  order_type?: string | null;
  /** Time in force, e.g. "day". */
  tif?: string | null;
  /** Exit categorization on sell orders. */
  exit_reason?: string | null;
  /** Signal-time last price (现价) from the order's cycle snapshot. */
  last_price?: string | null;
  /** Signal-time percent change (涨跌幅), pre-formatted e.g. "+1.2%". */
  pct_change?: string | null;
  /** Signal direction (方向) from the cycle's per-symbol diagnostic. */
  direction?: string | null;
  /** Dispatch failure detail when the broker dispatch attempt errored. */
  dispatch_error?: string | null;
  /** Number of dispatch attempts recorded for this approval. */
  dispatch_attempts?: number | null;
  /** Matched broker fill receipt, when the dispatched order filled. quantity /
   * price / amount are exact decimal strings from trade_fills; never parseFloat
   * / Number for any decision. */
  matched_fill?: {
    quantity: string | null;
    price: string | null;
    amount: string | null;
    filled_at: string | null;
  } | null;
};

/** Filters accepted by GET /approvals (the full history view). All optional;
 * omitted fields are not constrained. */
export type ApprovalQuery = {
  /** Subset of pending / approved / rejected / expired. */
  status?: string[];
  symbol?: string;
  task_id?: string;
  account_id?: string;
  /** web / api / feishu_card */
  decision_source?: string;
  /** Case-insensitive substring over approval_id / intent_id / symbol / task / run. */
  q?: string;
  /** ISO timestamp lower bound (inclusive) on created_at. */
  created_after?: string;
  /** ISO timestamp upper bound (inclusive) on created_at. */
  created_before?: string;
  limit?: number;
  offset?: number;
};

/** Paged envelope from GET /approvals. ``total`` is the full match count BEFORE
 * limit/offset, so the pager can render accurate page counts. */
export type ApprovalListResponse = {
  items: PendingApproval[];
  total: number;
  limit: number;
  offset: number;
};

export type AgentTemplate = {
  name: string;
  default_mode: string;
};

export type AgentPromptTemplate = {
  template_id: string;
  name: string;
  description: string;
  system_prompt: string;
};

export type AssistantAgentPromptTemplate = AgentPromptTemplate;

/** Response from ``GET /data-providers``. */
export type CapabilitySummary = {
  id: string;
  capability_id: string;
  kind: string;
  label: string;
  description: string;
  config_schema: Record<string, unknown>;
  provider_id?: string;
  provider_kind?: string;
  channel_type?: string;
  ui_hints?: Record<string, unknown>;
};

export type RuntimeCapabilitiesResponse = {
  items: CapabilitySummary[];
  total: number;
  kinds: string[];
};

export type RuntimeStatus = {
  health: string;
  capabilities: {
    total: number;
    kinds: string[];
  };
  assistant: {
    available: boolean;
    tool_count: number;
  };
  channels: {
    manager_available: boolean;
    repository_available: boolean;
    registered_ids: string[];
    repository_count: number | null;
  };
  cron: {
    available: boolean;
    run_repository_available: boolean;
  };
  observability: {
    model_invocations_available: boolean;
  };
  checks: Record<string, unknown>;
};

export type DataProvidersResponse = {
  providers: string[];
  items?: CapabilitySummary[];
};

export type ModelInvocationRow = {
  id: number;
  created_at: string;
  /** Resolved model id from API response (may differ from configured model). */
  model_id: string;
  /** Adapter/API family: "anthropic" | "openai_compatible" | "lmstudio" */
  provider_kind: string;
  model: string;
  task_id: string | null;
  run_id: string | null;
  /** OpenTelemetry trace id (hex); absent on legacy rows */
  trace_id?: string | null;
  /** OpenTelemetry span id (hex); absent on legacy rows */
  span_id: string | null;
  call_kind: string;
  first_token_latency_ms: number | null;
  total_latency_ms: number | null;
  input_tokens: number | null;
  output_tokens: number | null;
  total_tokens: number | null;
  cache_read_tokens: number | null;
  cache_write_tokens: number | null;
  ok: boolean;
  error_message: string | null;
  request: Record<string, unknown>;
  response: Record<string, unknown> | null;
  /** Logical route name from invocation context when set. */
  model_route_name?: string | null;
  /** YAML / DB provider key when set. */
  provider_key?: string | null;
};

export type DebugSessionEvent = {
  sequence: number;
  session_id: string;
  event_type: string;
  payload: Record<string, unknown>;
  timestamp: string;
};

export type Span = {
  span_id: string;
  trace_id: string;
  parent_span_id: string | null;
  session_id: string;
  name: string;
  span_type: string;
  start_time: string;
  end_time: string | null;
  duration_ms: number | null;
  attributes: Record<string, unknown>;
  status: "ok" | "error";
  span_source: "debug" | "scheduled" | "manual" | "backtest" | "cron";
};

export type DebugSessionSummary = {
  session_id: string;
  task_id: string;
  status: string;
  run_id: string | null;
  error_message: string;
  /** Exception class name when the session failed (e.g. ``ValueError``). */
  error_type: string | null;
  /** Last ~400 chars of the formatted exception, when available. */
  traceback_tail: string | null;
  input_overrides: Record<string, unknown> | null;
  effective_config: Record<string, unknown> | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  session_type: "debug" | "scheduled" | "manual" | "backtest" | "cron";
};

/** One row in ``runs`` — a multi-bar replay run for a task. */
export type RunRow = {
  run_id: string;
  task_id: string;
  status: string;
  market_profile: string;
  bar_interval: string;
  range_start_utc: string;
  range_end_utc: string;
  session_id: string | null;
  /**
   * Whether this run captured full debug observability. When false the run
   * executed in fast mode: no debug session / spans / cycle traces / model
   * invocations were recorded — an empty debug view is expected, not a fault.
   * Defaults true for runs predating the toggle.
   */
  debug_enabled?: boolean;
  starting_equity: number | null;
  ending_equity: number | null;
  return_pct: number | null;
  error_message: string | null;
  bars_total: number;
  bars_completed: number;
  stop_requested?: boolean;
  ledger_checkpoint_json?: Record<string, unknown> | null;
  reference_starting_equity?: number | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  /** Per-run override; null uses the task default route. */
  model_route_name?: string | null;
};

export type BacktestChartBar = {
  timestamp: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  amount?: number | null;
};

export type BacktestChartTrade = {
  timestamp: string | null;
  side: string;
  price: number | null;
  quantity: number | null;
  intent_id: string | null;
  rationale: string | null;
  cycle_run_id: string;
  // Per-fill factor identifier copied from Signal.tag. Buy fills populate
  // entry_tag; sell fills populate exit_tag. Both are nullable: pre-existing
  // fills (and any HOLD-converted intents) carry no tag.
  entry_tag: string | null;
  exit_tag: string | null;
  exit_reason: string | null;
};

/** A single trade fill row as returned by the API
 * (``trade_fills`` table + ``signal_tag`` propagation). */
export type TradeFill = {
  id: number;
  task_id: string;
  cycle_run_id: string;
  run_id: string | null;
  session_id: string | null;
  symbol: string;
  side: "buy" | "sell";
  quantity: string;
  price: string;
  amount: string | null;
  fee: string | null;
  currency: string | null;
  intent_id: string | null;
  rationale: string | null;
  entry_tag: string | null;
  exit_tag: string | null;
  exit_reason: string | null;
  filled_at: string;
  source_mode: string;
};

export type BacktestChartSnapshot = {
  run: RunRow;
  symbols: string[];
  selected_symbol: string;
  adjust: string;
  bars: BacktestChartBar[];
  volume_mode: "amount_available" | "volume_only";
  trades: BacktestChartTrade[];
  warnings: string[];
};

export type MarketBarSyncState = {
  symbol: string;
  interval: string;
  provider: string;
  adjust: string;
  target_start: string | null;
  target_end: string | null;
  covered_start: string | null;
  covered_end: string | null;
  last_success_at: string | null;
  last_attempt_at: string | null;
  last_error_code: string | null;
  last_error_type: string | null;
  last_error_message: string | null;
  retry_count: number;
  status: string;
};

export type LocalMarketBarsSnapshot = {
  symbol: string;
  interval: string;
  provider: string;
  adjust: string;
  start: string;
  end: string;
  bars: BacktestChartBar[];
  volume_mode: "amount_available" | "volume_only";
  summary: LocalMarketBarsSummary;
  coverage: LocalMarketCoverage;
  available_overlays: LocalMarketOverlayCandidates;
  sync_state: MarketBarSyncState | null;
  warnings: string[];
};

export type LocalMarketBarsSummary = {
  bar_count: number;
  latest_close: number | null;
  window_change: number | null;
  window_change_pct: number | null;
  window_high: number | null;
  window_low: number | null;
  amplitude_pct: number | null;
  total_volume: number;
  total_amount: number | null;
};

export type LocalMarketCoverageSegment = {
  start: string;
  end: string;
  status: "covered" | "missing" | string;
};

export type LocalMarketCoverage = {
  requested_start: string;
  requested_end: string;
  covered_segments: LocalMarketCoverageSegment[];
  missing_segments: LocalMarketCoverageSegment[];
};

export type LocalMarketOverlayCandidate = {
  id: string;
  label: string;
  run_id?: string;
  task_id?: string;
  status?: string;
  run_count?: number;
  item_count?: number;
};

export type LocalMarketOverlayCandidates = {
  backtest_trades: LocalMarketOverlayCandidate[];
  task_fills: LocalMarketOverlayCandidate[];
  signals: LocalMarketOverlayCandidate[];
};

export type LocalMarketOverlayItem = {
  timestamp: string;
  kind: string;
  side: string | null;
  price: number | null;
  label: string;
  details: Record<string, unknown>;
};

export type LocalMarketOverlaySnapshot = {
  overlay_kind: "backtest_trades" | "task_fills" | "signals" | string;
  source: Record<string, unknown>;
  items: LocalMarketOverlayItem[];
  warnings: string[];
};

export type LocalMarketSyncResponse = {
  status: "ok" | "accepted" | string;
  execution_mode: "sync" | "async" | string;
  job_id?: string;
  mode: "fill_gap" | "force_refresh" | string;
  requested_range: { start: string; end: string };
  fetched_segments?: Array<{ start: string; end: string; status: string }>;
  upserted_count?: number;
  adjust_drift_refreshed?: boolean;
  warnings: string[];
};

export type LocalMarketSyncJob = {
  job_id: string;
  status: "pending" | "running" | "ok" | "failed" | string;
  mode: "fill_gap" | "force_refresh" | string;
  symbol: string;
  interval: string;
  provider: string;
  adjust: string;
  requested_range: { start: string; end: string };
  fetched_segments: Array<{ start: string; end: string; status: string }>;
  upserted_count: number;
  adjust_drift_refreshed?: boolean;
  started_at: string | null;
  finished_at: string | null;
  error_code: string | null;
  error_type: string | null;
  error_message: string | null;
  hint: string | null;
};

export type DebugSessionDetail = DebugSessionSummary & {
  spans: Span[];
  model_invocations: ModelInvocationRow[];
};

export type TraceSummary = {
  trace_id: string;
  session_id: string;
  created_at: string;
  status: "ok" | "error";
  duration_ms: number | null;
  span_count: number;
  span_name: string;
  model: string | null;
  input_tokens: number | null;
  output_tokens: number | null;
  cache_read_tokens: number | null;
  cache_write_tokens: number | null;
  error_message: string | null;
};

export type TraceDetail = {
  trace_id: string;
  session_id: string;
  spans: Span[];
  model_invocations: ModelInvocationRow[];
};

/** Snapshot persisted under ``cycle_runs.details.post_cycle_account`` after each cycle. */
export type PostCycleAccountPositionRow = {
  symbol: string;
  name?: string | null;
  /** Whole shares (integer in API JSON). */
  quantity: number;
  /** Whole shares when present (integer in API JSON). */
  available?: number | null;
  /** Full-precision decimal string (no float rounding). */
  cost_price: string;
  last_price?: string | null;
  market_value?: string | null;
  frozen?: number | null;
};

export type PostCycleAccount = {
  source: string;
  captured_at: string;
  account: { cash: string; equity: string };
  total_market_value: string;
  positions: PostCycleAccountPositionRow[];
};

/**
 * One symbol's compact price snapshot persisted under
 * ``cycle_runs.details.market_snapshot[symbol]``. ``pct_change`` is already a
 * percent (e.g. ``2.61`` = +2.61%) and is ``null`` when the prior close is
 * missing/zero. Powers the strategy_signal_alert ``no_signal_mode='full'``
 * push (latest price + 涨跌幅) without re-querying market data.
 */
export type CycleMarketSnapshotEntry = {
  last_price: number | null;
  prev_close: number | null;
  pct_change: number | null;
  open?: number | null;
  high?: number | null;
  low?: number | null;
};

/**
 * One symbol's strategy decision factors persisted under
 * ``cycle_runs.details.signal_diagnostics[symbol]`` (the serialized
 * ``Signal``). Explains *why* a cycle produced no actionable order — surfaced
 * in the ``full`` no-signal push and available to the debug UI.
 */
export type CycleSignalDiagnosticEntry = {
  direction: "buy" | "sell" | "hold" | "target_exposure" | "target_quantity" | string;
  tag: string;
  rationale: string;
  diagnostics: Record<string, unknown>;
  target_exposure?: number;
  target_quantity?: number;
};

/** Task-owned logical holdings snapshot persisted under
 * ``cycle_runs.details.task_budget`` when task-level budget caps are enabled. */
export type CycleTaskBudgetPosition = {
  symbol: string;
  quantity: number;
  market_value: string;
  price: string;
  price_source: string;
};

export type CycleTaskBudgetSnapshot = {
  max_task_position_amount: string | null;
  max_task_position_ratio: number | null;
  budget_cap: string | null;
  current_usage: string;
  remaining_budget: string;
  positions: CycleTaskBudgetPosition[];
  warnings: string[];
};

/**
 * One row of the strategy's per-cycle signal decision, aggregated from the
 * persisted ``strategy_runner_cycle`` span events on the backend. The
 * frontend can render a timeline of these instead of operators (or agents)
 * reimplementing indicators locally to explain a zero-trade run.
 *
 * ``run_id`` / ``cycle_time`` may be ``null`` when the matching
 * ``cycle_runs`` row isn't (yet) persisted — the event payload is still
 * surfaced so the gap is visible. Sort key is ``cycle_time`` ascending
 * (falling back to ``span_start_time`` for orphan rows).
 */
export type SignalTimelineEntry = {
  run_id: string | null;
  cycle_time: string | null;
  cycle_time_utc: string | null;
  span_id: string | null;
  trace_id: string | null;
  span_start_time: string | null;
  signals_buy: number | null;
  signals_sell: number | null;
  signals_hold: number | null;
  signals_target_exposure: number | null;
  signals_target_quantity: number | null;
  universe_size: number | null;
  strategy_name: string | null;
  strategy_class: string | null;
  per_symbol_tags: Record<string, string>;
};

/**
 * Compact summary of the signal_timeline for the top of the debug-view payload.
 * Designed to survive tool-result truncation in agent contexts — kept small
 * (counts + top-5 tag maps) so it fits inside the first ~1KB of the response.
 * Frontend renders this as a one-glance overview before paying for the full
 * timeline render.
 */
export type SignalTimelineSummary = {
  total_cycles: number;
  total_signals_buy: number;
  total_signals_sell: number;
  total_signals_hold: number;
  total_signals_target_exposure: number;
  total_signals_target_quantity: number;
  top_hold_tags: Record<string, number>;
  top_buy_tags: Record<string, number>;
  top_sell_tags: Record<string, number>;
  top_target_exposure_tags: Record<string, number>;
  top_target_quantity_tags: Record<string, number>;
  first_cycle_time: string | null;
  last_cycle_time: string | null;
  first_buy_cycle_time: string | null;
  first_sell_cycle_time: string | null;
  first_target_exposure_cycle_time: string | null;
  first_target_quantity_cycle_time: string | null;
  zero_trade: boolean;
};

/** One pushed assistant message surfaced in a cycle run's push detail. */
export type PushedMessage = {
  message_id: string;
  session_id?: string | null;
  role: string;
  content: string;
  created_at: string | null;
  source: string | null;
  channel_target: string | null;
  delivery_status: string | null;
  run_id?: string | null;
  cron_job_run_id?: string | null;
  /** True when the card content is a deterministic reconstruction (the actual
   * Feishu push left no persisted copy), not the byte-exact delivered message. */
  reconstructed?: boolean;
  /** Human note explaining a reconstructed card (e.g. prose original not stored). */
  note?: string | null;
};

/** Compact assistant-session summary surfaced in a cycle run's push detail. */
export type AssistantSessionSummary = {
  session_id: string;
  title: string | null;
  status: string;
  agent_id: string | null;
};

/** Read-only "what actually got pushed" detail for one cycle run. Nested under
 * ``push_detail`` on {@link CycleRunDebugView}. Every empty subsection carries a
 * non-null ``reason`` string that MUST be rendered (never blank). */
export type PushDetail = {
  resolved_from_kind: string;
  strategy: {
    name: string | null;
    task_id: string | null;
    reason: string | null;
  };
  composer_agent: {
    agent: Agent | null;
    agent_id: string | null;
    compose_mode: string | null;
    reason: string | null;
  };
  assistant_session: {
    session: AssistantSessionSummary | null;
    reason: string | null;
  };
  pushed_messages: {
    items: PushedMessage[];
    reason: string | null;
  };
  approvals: {
    items: PendingApproval[];
    total: number;
    reason: string | null;
  };
};

/** Per-cycle debug UI: session row (if any) + spans for this run's trace + invocations for this run_id. */
export type CycleRunDebugView = {
  cycle_run: CycleRunRow;
  session: DebugSessionSummary | null;
  spans: Span[];
  model_invocations: ModelInvocationRow[];
  /**
   * Per-cycle signal_tag aggregation. Always present (possibly empty) so
   * the frontend can render "no signals recorded yet" distinctly from
   * "old backend payload missing the field".
   */
  signal_timeline: SignalTimelineEntry[];
  /**
   * Compact summary placed FIRST in the backend payload so it survives
   * tool-result truncation (request1.json turn 2 had the full timeline
   * lost to a 620KB tail-truncation). Always present (zeroed when no
   * cycles emitted strategy_runner_cycle).
   */
  signal_timeline_summary: SignalTimelineSummary;
  /**
   * Read-only "what actually got pushed" detail: pushed cards, approvals with
   * dispatch receipt, the composer/cron assistant Agent, the strategy/task
   * name, and the landed assistant session. Optional — older backend builds
   * omit it; consumers must branch on undefined.
   */
  push_detail?: PushDetail;
};

/** One row in `cycle_runs` — each worker `run_cycle` (debug, manual tick, scheduled tick). */
export type CycleRunRow = {
  run_id: string;
  task_id: string;
  agent_name: string;
  session_id: string | null;
  trace_id: string | null;
  run_mode: string;
  run_kind: "debug" | "scheduled" | "manual" | "trigger" | string;
  /** Source trigger (``trg-…``) when this cycle was fired by a Task Trigger. */
  trigger_id?: string | null;
  clock_mode: "wall" | "simulated" | string;
  cycle_time: string | null;
  cycle_time_utc?: string | null;
  wall_started_at: string;
  wall_finished_at: string | null;
  runtime_params: Record<string, unknown> | null;
  status: string;
  /** Per-cycle payload: `universe`, `position_intents`, `fills`, `post_cycle_account`, `market_snapshot` ({@link CycleMarketSnapshotEntry} per symbol), `task_budget` ({@link CycleTaskBudgetSnapshot}), `signal_diagnostics` ({@link CycleSignalDiagnosticEntry} per symbol), `failure_error`, plus extension keys. */
  details: Record<string, unknown> | null;
  cycle_failed: boolean;
  failure_message: string | null;
  completed_phases: string[] | null;
  submitted_count: number | null;
  vetoed_count: number | null;
  pending_approval_count: number | null;
  code_version: string | null;
  code_hash: string | null;
};

/** Backfill data source ids accepted by ``settings.data_cache.source_priority``
 * (mirror of the backend ``_DATA_CACHE_SUBSCHEMA`` enum). */
export type DataCacheSource = "qmt" | "baostock" | "akshare" | "tushare" | "mock";

/** Optional ``settings.data_cache`` block governing the local-DB cache, upstream
 * backfill and gap-continuity policy. Every field is optional; omitting the
 * whole block (or any key) lets the backend apply its defaults. Mirrors the
 * backend ``_DATA_CACHE_SUBSCHEMA`` in ``doyoutrade/api/operations/task_tools.py``. */
export type TaskDataCacheSettings = {
  /** Backfill source priority; default ``["qmt", "baostock", "akshare", "tushare"]``. */
  source_priority?: DataCacheSource[];
  /** Default true: read the local DB before hitting upstream. */
  local_first?: boolean;
  /** Default true: pull from upstream and persist when the local cache is missing. */
  auto_backfill?: boolean;
  continuity?: {
    /** On an unverifiable gap; default ``"fail"`` (reject writes that cannot be proven to be a halt). */
    on_unverifiable_gap?: "fail" | "degrade";
  };
};

export type CreateTaskPayload = {
  name: string;
  mode?: string;
  description?: string;
  data_provider?: string;
  settings?: {
    model_route_name?: string;
    universe?: string[];
    data_provider?: string;
    strategy: TaskStrategyBinding;
    agent?: AgentSettings;
    /** Optional cache / backfill / continuity policy; omit to use backend defaults. */
    data_cache?: TaskDataCacheSettings;
  } | null;
};

export type TaskStrategyBinding = {
  definition_id: string;
  parameter_overrides?: Record<string, unknown>;
  execution_profile?: string;
};

export type StrategyDefinitionRow = {
  definition_id: string;
  name: string;
  class_name: string;
  current_version: string | null;
  api_version: string;
  parameter_schema: Record<string, unknown>;
  default_parameters: Record<string, unknown>;
  capabilities: Record<string, unknown>;
  provenance: Record<string, unknown>;
  code_hash: string;
  status: string;
  created_at: string | null;
  updated_at: string | null;
};

export type StrategyDefinitionFile = {
  path: string;
  content: string | null;
  skipped_reason?: string;
  size_bytes?: number;
};

export type StrategyDefinitionDetail = StrategyDefinitionRow & {
  input_contract: Record<string, unknown> | null;
  generation_prompt: string;
  generation_model: string;
  generation_metadata: Record<string, unknown> | null;
  files: StrategyDefinitionFile[];
};

export type StrategyDefinitionCompileResult = {
  definition_id: string;
  class_name: string;
  success: boolean;
  code_hash: string;
  qualified_name: string | null;
  descriptor: {
    name: string;
    parameter_schema: Record<string, unknown>;
    capabilities: Record<string, unknown>;
  } | null;
  errors: string[];
};

/**
 * Row from ``GET /model-routes`` — a self-contained model config (API key masked).
 * Merged former model_provider + model_route into a single entity.
 */
export type ModelRouteRow = {
  id: string;
  route_name: string;
  provider_kind: string;
  base_url: string | null;
  api_key_masked: string;
  target_model: string | null;
  settings: Record<string, unknown> | null;
  created_at: string;
  updated_at: string;
};

/** ``GET /setup/status`` — whether the default agent has a usable model route. */
export type SetupStatus = {
  configured: boolean;
  /** "cloud" when hosted (renders cloud-only chrome); defaults to "local". */
  deployment_mode?: "local" | "cloud";
};

/**
 * One entry from ``GET /setup/providers`` — the same preset catalog the
 * terminal onboarding wizard (``doyoutrade/onboarding.py``) offers, serialized
 * for the web ``SetupWizard`` overlay.
 */
export type SetupProvider = {
  label: string;
  provider_kind: string;
  base_url: string | null;
  model_hint: string;
  needs_key: boolean;
};

export type SkillFrontmatter = {
  name: string;
  description: string;
  license: string | null;
};

export type Skill = {
  folder_name: string;
  frontmatter: SkillFrontmatter;
  enabled: boolean;
  relative_path: string;
  locked: boolean;
};

export type SkillFileNode = {
  name: string;
  path: string;
  kind: "file" | "dir";
  size: number;
  mtime: string;
  mime: string | null;
  children?: SkillFileNode[];
};

export type SkillDetail = {
  folder_name: string;
  frontmatter: SkillFrontmatter;
  tree: SkillFileNode[];
};

export type SkillFile = {
  path: string;
  content: string;
  encoding: "utf-8" | "base64";
  size: number;
  mtime: string;
  mime: string;
};

export type AssistantTool = {
  name: string;
  description: string;
  category: string;
};

/** One entry in the read-only 复盘 (trading review) journal list. */
export type KnowledgeJournalListItem = {
  /** Journal-relative ``.md`` path, e.g. ``2026/2026-05-30.md``. */
  path: string;
  /** Display title (typically the date stem). */
  title: string;
  /** File size in bytes. */
  size: number;
  /** Last-modified timestamp (ISO 8601, UTC). */
  mtime: string;
};

/**
 * Response of ``GET /knowledge/journals``. ``items`` are newest-first;
 * empty with ``root_exists: false`` when no journals have been recorded yet.
 */
export type KnowledgeJournalList = {
  items: KnowledgeJournalListItem[];
  root_exists: boolean;
};

/** Response of ``GET /knowledge/journal`` — a single journal's markdown body. */
export type KnowledgeJournal = {
  /** Journal-relative ``.md`` path. */
  path: string;
  /** Display title (typically the date stem). */
  title: string;
  /** Raw markdown source (may carry YAML frontmatter). */
  content: string;
  /** File size in bytes. */
  size: number;
  /** Last-modified timestamp (ISO 8601, UTC). */
  mtime: string;
};

// ---------------------------------------------------------------------------
// Full-base browser (top-level Knowledge page)
// ---------------------------------------------------------------------------

/** One file entry in the structured knowledge index. */
export type KnowledgeIndexEntry = {
  /** Partition-relative path, e.g. ``2026-05/_overview.md``. */
  rel_path: string;
  /** One-line title (from the first ``# `` heading or ``summary:`` front-matter). */
  title: string;
  /** ``true`` for an ``_overview.md`` entry-point file (⭐). */
  is_overview: boolean;
  /** ``true`` when the title fell back to the stem (no heading) — degrades the map. */
  weak: boolean;
  /** Lowercased file suffix, e.g. ``.md`` / ``.csv``. */
  suffix: string;
};

/** A named bucket of entries (a month, a year, a strategy dir). */
export type KnowledgeIndexGroup = {
  name: string;
  entries: KnowledgeIndexEntry[];
};

/** One partition in the structured index. */
export type KnowledgeIndexPartition = {
  name: string;
  label: string;
  file_count: number;
  groups: KnowledgeIndexGroup[];
};

/** Response of ``GET /knowledge/index`` — the navigation map. */
export type KnowledgeIndex = {
  root_exists: boolean;
  total_files: number;
  weak_title_count: number;
  skipped_count: number;
  weak_titles: string[];
  generated_at: string;
  partitions: KnowledgeIndexPartition[];
};

/** Response of ``GET /knowledge/file`` — a single file (markdown or CSV). */
export type KnowledgeFile =
  | {
      partition: string;
      path: string;
      title: string;
      size: number;
      mtime: string;
      suffix: string;
      kind: "markdown";
      content: string;
    }
  | {
      partition: string;
      path: string;
      title: string;
      size: number;
      mtime: string;
      suffix: string;
      kind: "csv";
      columns: string[];
      rows: string[][];
      row_count: number;
      truncated: boolean;
    };

// ---------------------------------------------------------------------------
// Sentiment cycle timeline (daily emotional-cycle memory, from daily_review)
// ---------------------------------------------------------------------------

/**
 * One trading day's sentiment-cycle datapoint, as accumulated by the daily
 * 复盘 into the private knowledge base. ``label`` is the rule-based
 * single-day emotion label from the backend's ``_classify_sentiment`` — one of
 * ``退潮/低迷`` / ``中性`` / ``发酵/活跃`` / ``高潮/亢奋`` / ``分歧加剧``. It
 * describes the day only and is never a prediction / buy-sell signal.
 */
export type SentimentTimelinePoint = {
  /** Trading day (``YYYY-MM-DD``). */
  date: string;
  /** Rule-based single-day sentiment label (see ``_classify_sentiment``). */
  label: string;
  /** 涨停家数. */
  limit_up_count: number;
  /** 跌停家数. */
  limit_down_count: number;
  /** 炸板家数. */
  broken_board_count: number;
  /** 炸板率 (0..1). */
  broken_board_rate: number;
  /** 最高连板. */
  max_streak: number;
};

/**
 * Response of ``GET /knowledge/sentiment-timeline`` — the per-day emotional
 * cycle memory. ``items`` are ordered by ``date`` ascending; empty
 * (``{ items: [] }``) when no daily reviews have been recorded yet.
 */
export type SentimentTimeline = {
  items: SentimentTimelinePoint[];
};

// ---------------------------------------------------------------------------
// Symbol role cards (个股角色) — the user's own role tags on a symbol, kept in
// the private knowledge base (e.g. "把这票记成龙头"). This is descriptive
// long-term memory, never a prediction / buy-sell recommendation.
// ---------------------------------------------------------------------------

/**
 * One symbol's role card, as tagged by the user into the private knowledge
 * base. Every field is authored text — the UI never fabricates a value:
 * missing / blank strings render as ``—``.
 */
export type SymbolRoleCard = {
  /** Canonical symbol, e.g. ``600519``. */
  symbol: string;
  /** Instrument name. */
  name: string;
  /**
   * Role tag the user assigned, e.g. ``龙头`` / ``龙二`` / ``中军`` / ``补涨`` /
   * ``杂毛`` / ``事件型``. Unknown / new labels fall back to a neutral grey.
   */
  role: string;
  /** Free-text note the user wrote about this symbol. */
  note: string;
  /** Optional strategy-matching hint the user recorded. */
  strategy_hint: string;
  /** Last-updated timestamp (ISO string). */
  updated_at: string;
};

/**
 * Response of ``GET /knowledge/symbol-roles`` — the per-symbol role tags the
 * user keeps in the private knowledge base. ``items`` come back ordered by
 * ``updated_at`` descending and empty (``{ items: [] }``) when nothing has been
 * tagged yet.
 */
export type SymbolRoles = {
  items: SymbolRoleCard[];
};

// ---------------------------------------------------------------------------
// 知识图谱 (knowledge graph) — the entity-relation layer projected over the
// private knowledge base (kg_nodes / kg_edges). Facts are bi-temporal: a
// superseded judgment survives as an *expired* edge, so the UI can show "当时
// 怎么看" without polluting the current view. This is descriptive long-term
// memory, never a prediction / buy-sell recommendation.
// ---------------------------------------------------------------------------

/**
 * One knowledge-graph entity node. Identity is the natural key
 * ``(node_type, name)`` — for ``symbol`` nodes ``name`` is the canonical
 * symbol code and ``display_name`` the instrument name; for ``cycle`` nodes
 * ``name`` is the ``YYYY-MM`` month.
 */
export type KgNode = {
  id: string;
  /** ``symbol`` / ``theme`` / ``role`` / ``cycle`` / ``playbook`` / ``signal``. */
  node_type: string;
  name: string;
  display_name: string | null;
  attrs: Record<string, unknown> | null;
  status?: "active" | "retired" | "merged";
  retired_at?: string | null;
  redirect_to_id?: string | null;
};

/**
 * One knowledge-graph fact edge. ``provenance`` grades the fact:
 * ``deterministic`` = hard-data projection (roles / trades / signals),
 * ``llm`` = 观点候选 extracted from 复盘 journals (weight by ``confidence``),
 * ``manual`` = local-user edit or individually approved Agent proposal.
 * ``expired_at`` non-null = superseded history (bi-temporal), shown only when
 * the user asks for it.
 */
export type KgEdge = {
  id: string;
  src_id: string;
  dst_id: string;
  relation: string;
  /** Natural-language fact sentence — the primary display text. */
  fact: string;
  attrs: Record<string, unknown> | null;
  provenance: "deterministic" | "llm" | "manual";
  confidence: number | null;
  /** Where the fact came from: ``kb:<relpath>`` or ``db:<table>/<id>``. */
  source_ref: string | null;
  valid_at: string | null;
  invalid_at: string | null;
  created_at: string | null;
  expired_at: string | null;
};

/**
 * Response of ``GET /knowledge/graph?entity=...`` — the resolved center node,
 * other same-name candidates, and its N-hop neighborhood subgraph.
 */
export type KnowledgeGraphNeighborhood = {
  /** Monotonic revision used as the optimistic-lock token for manual edits. */
  revision: number;
  center: KgNode;
  candidates: KgNode[];
  nodes: KgNode[];
  edges: KgEdge[];
};

/**
 * Response of ``GET /knowledge/graph/path`` — the shortest chain between two
 * resolved entities. ``found=false`` means no path within ``max_hops``
 * (``nodes``/``edges``/``path_node_ids`` empty). ``path_node_ids`` is the
 * ordered node chain source→target; ``edges`` are the connecting edges.
 */
export type KnowledgeGraphPath = {
  revision: number;
  source: KgNode;
  target: KgNode;
  found: boolean;
  hops: number;
  path_node_ids: string[];
  nodes: KgNode[];
  edges: KgEdge[];
};

export type KnowledgeGraphEntityTypeDefinition = {
  key: string;
  label: string;
  parent_key: string | null;
  protected: boolean;
  /** Identity color (``#rrggbb``) for graph rendering; custom types set their own. */
  color?: string | null;
  namespace?: "system" | "custom";
  status?: "active" | "deprecated";
  version?: number;
};

export type KnowledgeGraphRelationTypeDefinition = {
  key: string;
  label: string;
  source_type: string;
  target_type: string;
  symmetric: boolean;
  transitive: boolean;
  inverse_key: string | null;
  protected: boolean;
  namespace?: "system" | "custom";
  status?: "active" | "deprecated";
  version?: number;
};

export type KnowledgeGraphPropertyDefinition = {
  key: string;
  label: string;
  owner_kind: "entity_type" | "relation_type";
  owner_key?: string;
  value_type: string;
  required: boolean;
  multiple: boolean;
  constraints: Record<string, unknown> | null;
  protected: boolean;
  namespace?: "system" | "custom";
  status?: "active" | "deprecated";
  version?: number;
};

export type KnowledgeGraphSchema = {
  namespace: string;
  version: number;
  revision?: number;
  entity_types: KnowledgeGraphEntityTypeDefinition[];
  relation_types: KnowledgeGraphRelationTypeDefinition[];
  property_definitions: KnowledgeGraphPropertyDefinition[];
};

export type KnowledgeGraphCreateRelationOperation = {
  op: "create_relation";
  source: { type: string; name: string; display_name?: string | null };
  relation: string;
  target: { type: string; name: string; display_name?: string | null };
  fact: string;
  attrs?: Record<string, unknown> | null;
  confidence?: number | null;
  valid_at?: string | null;
  invalid_at?: string | null;
  edge_id?: string;
  revision?: number;
};

export type KnowledgeGraphReviseRelationOperation = {
  op: "revise_relation";
  edge_id: string;
  fact?: string;
  attrs?: Record<string, unknown> | null;
  confidence?: number | null;
  valid_at?: string | null;
  invalid_at?: string | null;
};

export type KnowledgeGraphRetractRelationOperation = {
  op: "retract_relation";
  edge_id: string;
  reason?: string;
};

export type KnowledgeGraphCreateEntityOperation = {
  op: "create_entity";
  type: string;
  name: string;
  display_name?: string | null;
  attrs?: Record<string, unknown> | null;
};

export type KnowledgeGraphUpdateEntityOperation = {
  op: "update_entity";
  entity_id: string;
  display_name?: string | null;
  attrs?: Record<string, unknown> | null;
  type?: string;
};

export type KnowledgeGraphRetireEntityOperation = {
  op: "retire_entity";
  entity_id: string;
  reason?: string;
};

export type KnowledgeGraphMergeEntitiesOperation = {
  op: "merge_entities";
  survivor_id: string;
  merge_ids: string[];
  reason?: string;
};

export type KnowledgeGraphOverrideRelationOperation = {
  op: "override_relation";
  edge_id?: string;
  dedupe_key?: string;
  fact: string;
  attrs?: Record<string, unknown> | null;
  confidence?: number | null;
};

export type KnowledgeGraphResolveConflictOperation = {
  op: "resolve_conflict";
  conflict_id: string;
  decision: "keep_left" | "keep_right" | "override" | "dismiss";
  override?: Record<string, unknown> | null;
};

export type KnowledgeGraphAttachEvidenceOperation = {
  op: "attach_evidence";
  target_kind: "node" | "edge";
  target_id: string;
  kind: "kb_ref" | "url" | "quote" | "file";
  uri: string;
  excerpt?: string;
  attrs?: Record<string, unknown> | null;
};

export type KnowledgeGraphDetachEvidenceOperation = {
  op: "detach_evidence";
  evidence_id: string;
};

export type KnowledgeGraphSaveLayoutOperation = {
  op: "save_layout";
  scope_key: string;
  positions: Record<string, { x: number; y: number }>;
  locked_ids?: string[];
  highlight_ids?: string[];
};

export type KnowledgeGraphChangeOperation =
  | KnowledgeGraphCreateRelationOperation
  | KnowledgeGraphReviseRelationOperation
  | KnowledgeGraphRetractRelationOperation
  | KnowledgeGraphCreateEntityOperation
  | KnowledgeGraphUpdateEntityOperation
  | KnowledgeGraphRetireEntityOperation
  | KnowledgeGraphMergeEntitiesOperation
  | KnowledgeGraphOverrideRelationOperation
  | KnowledgeGraphResolveConflictOperation
  | KnowledgeGraphAttachEvidenceOperation
  | KnowledgeGraphDetachEvidenceOperation
  | KnowledgeGraphSaveLayoutOperation;

export type KnowledgeGraphConflict = {
  id: string;
  conflict_type: string;
  status: "open" | "resolved" | "dismissed";
  subject_key: string;
  left: Record<string, unknown>;
  right: Record<string, unknown>;
  detected_at: string;
  resolved_at: string | null;
  resolution: Record<string, unknown> | null;
};

export type KnowledgeGraphEvidence = {
  id: string;
  target_kind: "node" | "edge";
  target_id: string;
  kind: "kb_ref" | "url" | "quote" | "file";
  uri: string;
  excerpt: string;
  attrs: Record<string, unknown> | null;
  status: "active" | "detached";
};

export type KnowledgeGraphCanvasLayout = {
  id: string;
  scope_key: string;
  version: number;
  positions: Record<string, { x: number; y: number }>;
  locked_ids: string[];
  highlight_ids: string[];
  actor_id: string;
  change_set_id: string;
  created_at: string;
};

export type KnowledgeGraphChangeSet = {
  id: string;
  status: "pending" | "applied" | "rejected" | "stale" | "cancelled";
  actor_type: "local_user" | "agent" | "system";
  actor_id: string;
  base_revision: number;
  revision: number | null;
  proposal_hash: string;
  summary: string;
  created_at: string;
  applied_at: string | null;
  edge_ids: string[];
  operations?: KnowledgeGraphChangeOperation[];
};

/**
 * Response of ``POST /knowledge/graph/sync`` — one idempotent deterministic
 * re-projection pass. ``skipped: true`` means every source's content hash was
 * unchanged since the last sync (nothing to do — not "import succeeded").
 */
export type KnowledgeGraphSyncResult = {
  skipped: boolean;
  forced?: boolean;
  changed_sources: string[];
  /** Human-readable outcome; prefer this over inferring from ``skipped``. */
  message?: string;
  projected_nodes?: number;
  projected_edges?: number;
  warnings?: unknown[];
  apply?: {
    nodes_created: number;
    nodes_updated: number;
    edges_created: number;
    edges_unchanged: number;
    edges_expired: number;
  } | null;
  counts?: {
    nodes: number;
    active_edges: number;
    expired_edges: number;
  };
};

/**
 * Response of ``GET /knowledge/graph/summary`` — size snapshot + sample
 * entry-point nodes for the empty-state exploration chips.
 */
export type KnowledgeGraphSummary = {
  counts: {
    nodes: number;
    active_edges: number;
    expired_edges: number;
  };
  entry_points: KgNode[];
};

// ---------------------------------------------------------------------------
// 打板模式库 (playbook) — the user's own 战法 / 打法 summaries kept in the
// private knowledge base's ``playbook`` partition (e.g. "把这个打法记进模式
// 库"). Each entry is authored text — the UI never fabricates a value: missing
// / blank fields render as ``—``. This is descriptive long-term memory,
// never a prediction / buy-sell recommendation.
// ---------------------------------------------------------------------------

/**
 * One playbook (打法) entry, as authored by the user into the private
 * knowledge base's ``playbook`` partition. ``path`` is the relative file
 * path used to fetch the full markdown via
 * {@link import("./api").getKnowledgeFile}. All descriptive fields may be
 * ``null`` when the author left them blank — the UI shows ``—`` and never
 * fabricates a value.
 */
export type PlaybookEntry = {
  /** Relative file path within the ``playbook`` partition (used to fetch full text). */
  path: string;
  /** Title of the note (fallback for the 打法名 when ``pattern`` is blank). */
  title: string;
  /** One-line summary / 摘要 of the 打法. */
  summary: string | null;
  /** The 打法名 (pattern name), e.g. ``首板打板`` / ``低吸``. */
  pattern: string | null;
  /**
   * Sentiment-cycle stage this 打法 applies to, e.g. ``退潮/低迷`` /
   * ``中性`` / ``发酵/活跃`` / ``分歧`` / ``高潮/亢奋`` / ``全周期``.
   * Coloured to match the sentiment palette; unknown / null falls back to blue.
   */
  stage: string | null;
  /** Free-form tags the user attached. */
  tags: string[] | null;
  /** Last-updated timestamp (ISO string). */
  updated_at: string;
};

/**
 * Response of ``GET /knowledge/playbook`` — the 打板模式库 (战法 / 打法
 * summaries) the user keeps in the private knowledge base. ``items`` come back
 * ordered by ``updated_at`` descending and empty (``{ items: [] }``) when
 * nothing has been recorded yet.
 */
export type Playbook = {
  items: PlaybookEntry[];
};

// ---------------------------------------------------------------------------
// 交割单归因 (trade attribution) — realized-PnL round-trips reconstructed from
// the broker settlement statements (交割单) the user dropped into the private
// knowledge base's ``trades/`` partition. Money fields are decimal strings
// (possibly negative); the UI converts via ``Number()`` only for display and
// never fabricates a value (missing → ``—``). This is a descriptive review of
// the user's own executed trades, never a prediction / buy-sell recommendation.
// ---------------------------------------------------------------------------

/**
 * The best / worst single round-trip highlighted in the attribution summary.
 * ``realized_pnl`` is a signed decimal string; ``return_pct`` is a percent
 * number (e.g. ``12.5`` = +12.5%) or null when it can't be computed.
 */
export type TradeAttributionExtreme = {
  symbol: string;
  name: string;
  /** Signed realized PnL as a decimal string (may be negative). */
  realized_pnl: string;
  /** Percent return number (e.g. ``12.5``) or null when uncomputable. */
  return_pct: number | null;
};

/**
 * Headline aggregates over all reconstructed round-trips. Money fields are
 * decimal strings (possibly negative); ratio / count fields may be ``null``
 * (render as ``—``, never a fabricated ``0``). An empty account comes back with
 * ``round_trips: 0`` and the null-able fields set to null.
 */
export type TradeAttributionSummary = {
  round_trips: number;
  win_count: number;
  loss_count: number;
  /** ``0..1`` ratio, or null when there are no closed round-trips. */
  win_rate: number | null;
  /** Signed total realized PnL as a decimal string. */
  total_realized_pnl: string;
  /** Average winning-trade PnL (decimal string) or null. */
  avg_win: string | null;
  /** Average losing-trade PnL (decimal string, ≤ 0) or null. */
  avg_loss: string | null;
  /** Gross profit / gross loss ratio, or null when there are no losses. */
  profit_factor: number | null;
  /** Mean holding period in days, or null. */
  avg_hold_days: number | null;
  best: TradeAttributionExtreme | null;
  worst: TradeAttributionExtreme | null;
  open_positions: number;
};

/**
 * One reconstructed FIFO round-trip (open → close) over a single symbol.
 * ``avg_buy`` / ``avg_sell`` / ``realized_pnl`` are decimal strings;
 * ``return_pct`` / ``hold_days`` may be null when uncomputable.
 */
export type TradeAttributionRoundTrip = {
  symbol: string;
  name: string;
  /** Open (first buy) date, ``YYYY-MM-DD``. */
  open_date: string;
  /** Close (last sell that flattened the lot) date, ``YYYY-MM-DD``. */
  close_date: string;
  /** Calendar holding period in days, or null. */
  hold_days: number | null;
  /** Round-trip share quantity. */
  qty: number;
  /** Volume-weighted average buy price (decimal string). */
  avg_buy: string;
  /** Volume-weighted average sell price (decimal string). */
  avg_sell: string;
  /** Signed realized PnL as a decimal string (may be negative). */
  realized_pnl: string;
  /** Percent return number (e.g. ``12.5``) or null when uncomputable. */
  return_pct: number | null;
};

/** Per-symbol aggregate across that symbol's round-trips. */
export type TradeAttributionBySymbol = {
  symbol: string;
  name: string;
  round_trips: number;
  /** Signed cumulative realized PnL as a decimal string (may be negative). */
  realized_pnl: string;
  /** ``0..1`` ratio, or null when there are no closed round-trips. */
  win_rate: number | null;
};

/**
 * One settlement-statement file the backend could not parse — surfaced
 * honestly so the user knows some data was excluded from the attribution.
 */
export type TradeAttributionUnparsed = {
  /** File path (relative to the ``trades/`` partition). */
  path: string;
  /** Human-readable reason the file was skipped. */
  reason: string;
};

/**
 * Response of ``GET /knowledge/trade-attribution?months=N`` — realized-PnL
 * attribution reconstructed from the user's broker settlement statements. An
 * empty account comes back with a zeroed summary and empty arrays.
 */
export type TradeAttribution = {
  summary: TradeAttributionSummary;
  round_trips: TradeAttributionRoundTrip[];
  by_symbol: TradeAttributionBySymbol[];
  unparsed: TradeAttributionUnparsed[];
};

// ---------------------------------------------------------------------------
// 券商交割单 CSV 导入 (portfolio statement imports) — upload a broker-exported
// settlement CSV, preview the parsed fills (with duplicate detection), then
// commit into the knowledge base's ``trades/<broker>/<month>.csv`` partition.
// Money / price / qty fields are decimal strings, same discipline as the trade
// attribution types above.
// ---------------------------------------------------------------------------

/** One selectable broker from ``GET /portfolio/imports/brokers``. */
export type PortfolioImportBrokerItem = {
  /** Broker key used as the ``broker`` form field on parse / commit. */
  broker: string;
  /** Human-readable broker name shown in the picker. */
  display_name: string;
  /** True when this broker already has imported statements on disk. */
  existing: boolean;
};

/** Response of ``GET /portfolio/imports/brokers``. */
export type PortfolioImportBrokersResponse = {
  items: PortfolioImportBrokerItem[];
};

/**
 * One parsed fill row previewed from the uploaded CSV
 * (``POST /portfolio/imports/csv/parse`` → ``records[]``). ``price`` / ``qty``
 * / ``amount`` are decimal strings; ``duplicate`` marks rows already present in
 * the knowledge base (they would be skipped on commit).
 */
export type PortfolioImportParseRecord = {
  /** Trade date, ``YYYY-MM-DD``. */
  date: string;
  /** Trade time, ``HH:MM:SS``. */
  time: string;
  /** Canonical symbol, e.g. ``600519.SH``. */
  symbol: string;
  /** Instrument display name, e.g. ``贵州茅台``. */
  name: string;
  /** Fill direction. */
  side: "buy" | "sell";
  /** Fill price as a decimal string. */
  price: string;
  /** Fill quantity as a decimal string. */
  qty: string;
  /** Fill amount as a decimal string. */
  amount: string;
  /** Target month partition, ``YYYY-MM``. */
  month: string;
  /** True when this fill already exists in the knowledge base. */
  duplicate: boolean;
};

/** Response of ``POST /portfolio/imports/csv/parse`` (multipart file+broker). */
export type PortfolioImportParseResponse = {
  status: string;
  /** Broker key the rows were parsed as. */
  broker: string;
  /** Total parsed fills in the file. */
  fills_total: number;
  /** Fills that would be newly appended on commit. */
  new_count: number;
  /** Fills already present in the knowledge base (skipped on commit). */
  duplicate_count: number;
  /** Lines the parser could not understand. */
  unparsed_count: number;
  records: PortfolioImportParseRecord[];
  /** True when ``records`` was truncated for preview (counts stay complete). */
  records_truncated: boolean;
  /** Unparsed entries, same honest path+reason shape as trade attribution. */
  unparsed: TradeAttributionUnparsed[];
};

/**
 * Post-commit review block (``POST /portfolio/imports/csv/commit`` →
 * ``review``; null on ``dry_run``) — which months were touched and the
 * refreshed attribution summary over them.
 */
export type PortfolioImportReview = {
  /** ``YYYY-MM`` months whose trade files were appended to. */
  affected_months: string[];
  /** Refreshed attribution summary (same shape as the 交割单归因 board). */
  attribution_summary: TradeAttributionSummary | null;
  /** Non-null when re-running attribution after the import failed. */
  attribution_error: string | null;
};

/** Response of ``POST /portfolio/imports/csv/commit`` (multipart file+broker+dry_run). */
export type PortfolioImportCommitResponse = {
  status: string;
  broker: string;
  /** Echo of the requested ``dry_run`` flag. */
  dry_run: boolean;
  /** Fills actually (or, on dry-run, would-be) appended. */
  appended_total: number;
  /** Fills skipped because they already existed. */
  duplicates_skipped: number;
  /** Total parsed fills in the file. */
  fills_total: number;
  /** Rows appended per target file, e.g. ``{"trades/huatai/2026-06.csv": 10}``. */
  written: Record<string, number>;
  unparsed_count: number;
  /** Unparsed entries, same honest path+reason shape as trade attribution. */
  unparsed: TradeAttributionUnparsed[];
  /** Post-import review; null when ``dry_run`` is true. */
  review: PortfolioImportReview | null;
};

export type InstrumentUniverseItem = {
  symbol: string;
  name: string;
  market?: string;
};

export type InstrumentUniverseSearchResponse = {
  source: string;
  items: InstrumentUniverseItem[];
};

export type InstrumentCatalogRow = {
  symbol: string;
  display_name: string | null;
  market: string | null;
  instrument_type: string | null;
  is_tradable: boolean | null;
  last_sync_source: string;
  last_sync_at: string | null;
  raw: Record<string, unknown> | null;
  created_at: string | null;
  updated_at: string | null;
};

export type InstrumentCatalogListResponse = {
  items: InstrumentCatalogRow[];
  total: number;
  limit: number;
  offset: number;
};

/** One entry in the single watchlist pool (``GET /watchlist``). ID prefix ``wl-``. */
export type WatchlistEntry = {
  id: string;
  symbol: string;
  display_name: string | null;
  tags: string[];
  note: string;
  sort_order: number;
  created_at: string | null;
  updated_at: string | null;
};

export type WatchlistListResponse = { items: WatchlistEntry[] };

/** Distinct tag with its member count (``GET /watchlist/tags``). */
export type WatchlistTagCount = {
  tag: string;
  count: number;
};

export type WatchlistTagsResponse = { items: WatchlistTagCount[] };

/** Body for ``POST /watchlist``. */
export type CreateWatchlistPayload = {
  symbol: string;
  display_name?: string | null;
  tags?: string[];
  note?: string;
  sort_order?: number;
};

/** Partial patch body for ``PUT /watchlist/{id}``; omitted fields are untouched. */
export type UpdateWatchlistPayload = {
  display_name?: string | null;
  tags?: string[];
  note?: string;
  sort_order?: number;
};

// ---------------------------------------------------------------------------
// 股票智能盯盘 (stock intelligent monitoring) — /monitors
// Mirrors the backend MonitorRuleSnapshot / MonitorAlertSnapshot EXACTLY.
// ---------------------------------------------------------------------------

/** How a monitor rule resolves its symbol universe each evaluation. */
export type MonitorScopeKind = "watchlist_tag" | "symbols";

/** A monitor rule's lifecycle status (server-derived). */
export type MonitorStatus = "active" | "paused" | "error";

/** Built-in condition presets (中文 labels rendered in the UI). */
export type MonitorPreset =
  | "limit_up"
  | "limit_down"
  | "limit_up_seal_shrink"
  | "limit_down_seal_shrink"
  | "limit_up_open"
  | "limit_down_open";

/** Comparison operators allowed in a predicate leaf. */
export type MonitorPredicateOp = ">" | ">=" | "<" | "<=" | "==" | "!=";

/** Whitelisted quote fields usable in a predicate leaf. */
export type MonitorPredicateField =
  | "price"
  | "change_pct"
  | "bid_vol1"
  | "ask_vol1"
  | "limit_up_price"
  | "limit_down_price"
  | "seal_peak_bid"
  | "seal_peak_ask"
  | "volume"
  | "amount";

/** A preset leaf in the condition tree. */
export type ConditionPresetLeaf = {
  preset: MonitorPreset;
  params?: Record<string, number>;
};

/** A field-comparison leaf in the condition tree. */
export type ConditionPredicateLeaf = {
  predicate: {
    field: string;
    op: MonitorPredicateOp;
    value: number;
  };
};

/** A condition leaf is either a preset or a predicate. */
export type ConditionLeaf = ConditionPresetLeaf | ConditionPredicateLeaf;

/** A logical node (and/or) over child condition nodes. */
export type ConditionLogicalNode = {
  op: "and" | "or";
  children: ConditionNode[];
};

/** A node in the condition tree: a logical node or a leaf. */
export type ConditionNode = ConditionLogicalNode | ConditionLeaf;

/** Delivery descriptor persisted on a monitor rule (or ``null``). */
export type MonitorDelivery = {
  mode?: string;
  target?: {
    kind: "channel" | "session";
    channel_id?: string;
    chat_id?: string;
    session_id?: string;
  };
} | null;

/** One monitor rule (matches the backend MonitorRuleSnapshot EXACTLY). */
export type MonitorRule = {
  id: string;
  name: string;
  enabled: boolean;
  status: MonitorStatus;
  scope_kind: MonitorScopeKind;
  scope_json: { tag?: string; symbols?: string[] };
  condition_json: ConditionNode;
  delivery_json: MonitorDelivery;
  cooldown_seconds: number;
  last_error: string;
  created_at: string;
  updated_at: string;
};

/** One monitor alert row (matches the backend MonitorAlertSnapshot EXACTLY). */
export type MonitorAlert = {
  id: number;
  monitor_rule_id: string;
  symbol: string;
  condition_name: string;
  transition_key: string;
  triggered_at: string;
  last_price: number | null;
  limit_price: number | null;
  diagnostics_json: Record<string, unknown>;
  run_id: string | null;
  delivery_status: string;
  delivered_at: string | null;
  created_at: string;
};

export type MonitorListResponse = { items: MonitorRule[]; total: number };

export type MonitorAlertListResponse = { items: MonitorAlert[]; total: number };

/** Body for ``POST /monitors``. */
export type CreateMonitorPayload = {
  name: string;
  scope_kind: MonitorScopeKind;
  scope: { tag?: string; symbols?: string[] };
  condition_json: ConditionNode;
  channel_id?: string;
  chat_id?: string;
  session_id?: string;
  cooldown_seconds?: number;
  enabled?: boolean;
};

/** Partial patch body for ``PUT /monitors/{id}``; send only changed fields. */
export type UpdateMonitorPayload = {
  name?: string;
  scope_kind?: MonitorScopeKind;
  scope?: { tag?: string; symbols?: string[] };
  condition_json?: ConditionNode;
  channel_id?: string;
  chat_id?: string;
  session_id?: string;
  cooldown_seconds?: number;
  enabled?: boolean;
  status?: MonitorStatus;
};

/** Per-symbol result of ``POST /monitors/{id}/run-once``. */
export type MonitorRunOnceSymbolResult = {
  symbol: string;
  matched: boolean;
  status: string;
  matched_leaves?: string[];
  quote?: QuoteSnapshot;
};

/** Response of ``POST /monitors/{id}/run-once``. */
export type MonitorRunOnceResponse = {
  monitor_id: string;
  evaluated_at: string;
  matched_count: number;
  symbols: MonitorRunOnceSymbolResult[];
};

/**
 * Live (or one-shot) quote for one symbol. Every numeric field may be ``null``
 * when QMT is disconnected or has no data for the symbol.
 *
 * ``status`` mirrors the backend quote/stream status frames:
 * - ``ok``: a real tick was resolved.
 * - ``qmt_disconnected``: no default QMT account / upstream unavailable.
 * - ``no_data``: connected but the symbol has no tick yet.
 * - ``suspended``: 停牌/无成交 — either qmt's last_price sentinel (<=0) or the
 *   backend's suspension-event overlay (flat last_price==prev_close tick);
 *   price and change_pct are null (no fake -100%/0%), prev_close is kept.
 *   Render 停牌.
 */
export type QuoteStatus = "ok" | "qmt_disconnected" | "no_data" | "suspended";

export type QuoteSnapshot = {
  symbol: string;
  price: number | null;
  prev_close: number | null;
  change: number | null;
  change_pct: number | null;
  open: number | null;
  high: number | null;
  low: number | null;
  volume: number | null;
  amount: number | null;
  timestamp: string | null;
  status: QuoteStatus;
  // Level-1 seal volumes (封单量) + computed A-share limit prices, surfaced for
  // realtime monitoring (涨停/跌停/打开/大减). Null for providers without an
  // order book or before prev_close is known.
  bid_vol1: number | null;
  ask_vol1: number | null;
  limit_up_price: number | null;
  limit_down_price: number | null;
};

export type QuotesResponse = { items: QuoteSnapshot[] };

export type InstrumentCatalogSyncResponse = {
  inserted: number;
  updated: number;
  rows_seen: number;
};

export type InstrumentCatalogDeleteResponse = {
  deleted: number;
};

export type AgentSettings = {
  react_max_turns: number;
  signal_tool_names: string[];
  enabled_skills: string[];
  position_constraints: {
    /** Omit or null = no per-order cap (review T = equity × f only). */
    max_single_order_amount: number | null;
    max_position_ratio: number;
    review_equity_fraction: number;
    /** Omit or null = no task-level total position amount cap. */
    max_task_position_amount?: number | null;
    /** Omit or null = no task-level total position ratio cap. */
    max_task_position_ratio?: number | null;
    /** Exchange board lot (shares) for the explicit target_quantity /
     * target_exposure rebalance paths. 1 = whole-share trading; A股 grids use
     * 100. Buy / partial-sell deltas are floored to lot multiples; full exits
     * are exempt and clear odd lots. */
    lot_size?: number;
    /** Rebalance dead band in lots for the explicit-target paths; a sub-band
     * rebalance is skipped to avoid grid churn. 0 = disabled. */
    rebalance_hysteresis_lots?: number;
  };
  approval: {
    min_notional_for_approval: number;
    timeout_seconds: number;
  };
};

export interface UploadResult {
  status: "ok";
  // Opaque server-side id (the on-disk storage name). The absolute path is NOT
  // returned to the client — the backend re-derives it from file_id.
  file_id: string;
  filename: string;
  mime_type?: string | null;
  size_bytes?: number;
}

export type AgentToolConfig = {
  name: string;
  load_mode: "base" | "deferred";
};

export type Agent = {
  id: string;
  name: string;
  status: "active" | "inactive";
  system_prompt: string;
  system_prompt_template_id?: string | null;
  /** Backend-rendered prompt — when ``system_prompt_template_id`` is set this
   * reflects the current .j2 file, so UI previews stay in sync with edits to
   * the on-disk template instead of the stored snapshot. */
  resolved_system_prompt?: string;
  model_route_name: string;
  tool_configs?: AgentToolConfig[];
  tool_names: string[];
  skill_names: string[];
  max_turns: number;
  context_compaction: AgentContextCompaction;
  is_default: boolean;
  /** True for the code-fixed builtin main agent. The builtin agent's name,
   * system prompt, tools and skills are code-controlled; only the runtime
   * knobs in ``editable_fields`` may be edited and it can never be deleted. */
  is_builtin: boolean;
  /** Whitelist of editable fields. For the builtin agent this is
   * ``["model_route_name", "context_compaction", "max_turns"]``; for custom
   * agents the backend returns the full set (or omits it entirely). */
  editable_fields?: string[];
  created_at: string;
  updated_at: string;
};

export type AgentListResponse = {
  items: Agent[];
  total: number;
};

/** Pre-action executed before the agent dispatch on each cron fire.
 *
 * `kind` is the executor key registered in ``JobExecutorRegistry`` (e.g.
 * ``"noop"`` / ``"strategy_cycle"``). ``params`` is the executor-specific
 * payload; ``strategy_cycle`` requires ``{instance_id: "<task_id>"}``. */
export type CronPreAction = {
  kind: "noop" | "strategy_cycle" | string;
  params: Record<string, unknown>;
};

/** One row in ``cron_job_runs`` — per cron fire. Surfaces the pre_action
 * outcome (``pre_run_id`` should match ``cycle_runs.run_id`` when the
 * pre-action is ``strategy_cycle``) so the UI can link the fire to the
 * resulting cycle/debug session. */
export type CronJobRun = {
  id: string;
  job_id: string;
  fired_at: string;
  started_at: string | null;
  finished_at: string | null;
  status:
    | "running"
    | "success"
    | "pre_failed"
    | "agent_failed"
    | "error"
    | "skipped"
    | "cancelled"
    | string;
  /** OTel trace id of the ``cron.job.fire`` for this run; null when no
   * trace was exported (e.g. legacy rows or suppressed fires). */
  trace_id: string | null;
  pre_kind: string | null;
  pre_status: "ok" | "error" | string | null;
  pre_run_id: string | null;
  pre_debug_session_id: string | null;
  pre_result_json: Record<string, unknown> | null;
  pre_error: string | null;
  agent_session_id: string | null;
  agent_error: string | null;
  /** Which JobTaskExecutor kind handled the fire (``agent_chat_reply`` /
   * ``strategy_signal_alert`` / null for legacy pre-Task-3 rows). */
  cron_task_kind: string | null;
  /** User-push outcome: ``delivered`` / ``suppressed`` / ``skipped`` /
   * ``failed`` / ``none`` / null for legacy rows. */
  delivery_status: string | null;
  created_at: string;
};

/** Cron-run trace aggregation: ``GET /assistant/cron-job-runs/{run_id}/trace``.
 * The shape mirrors ``TraceDetail`` so the UI can feed ``spans`` straight
 * into ``TraceViewer``, with ``related`` carrying per-task run_id
 * links from ``strategy_signal_alert``. */
export type CronJobRunTrace = {
  run_id: string;
  session_ids: string[];
  spans: Span[];
  model_invocations: ModelInvocationRow[];
  related: Array<{
    task_id: string | null;
    run_id: string | null;
    status: string | null;
  }>;
};

export type CronTaskKind = "agent_chat_reply" | "strategy_signal_alert" | string;

export type StrategySignalAlertTaskStatus =
  | "task_not_signal_only"
  | "task_not_cron_driven"
  | "task_not_running_for_cron_signal"
  | "task_lookup_failed"
  | "no_cycle_executed"
  | string;

export type CronTask = {
  kind: CronTaskKind;
  params: Record<string, unknown>;
};

export type CronJob = {
  id: string;
  agent_id: string;
  name: string;
  cron_expression: string;
  timezone: string;
  /** Tagged-union schedule discriminator. ``"cron"`` = recurring 5-field
   * expression (legacy default for back-compat). ``"at"`` = one-shot
   * fire at ``at_iso``, used for "fire in N seconds" intents. */
  schedule_kind: "cron" | "at";
  /** ISO-8601 instant with offset for ``schedule_kind="at"`` rows.
   * ``null`` for ``cron``-kind rows. */
  at_iso: string | null;
  /** If true, the row is hard-deleted after a terminal-state fire.
   * Defaults true for ``at``-kind one-shots, false for recurring
   * cron-kind. */
  delete_after_run: boolean;
  enabled: boolean;
  /** ``null`` on task-pipeline rows; non-null only for legacy rows that
   * stored a Jinja template. */
  input_template: string | null;
  max_concurrency: number;
  timeout_seconds: number;
  /** Optional pre-action executed before the agent dispatch on each fire.
   * Legacy two-stage pipeline only. */
  pre_action: CronPreAction | null;
  /** Task-pipeline kind ('agent_chat_reply' / 'strategy_signal_alert' /
   * ``null`` for legacy rows). The cron manager dispatches by this when
   * present and falls back to ``input_template`` + ``pre_action`` when
   * it is ``null``. */
  task_kind: CronTaskKind | null;
  /** Kind-specific params (see ``cron_executors/*.py``). */
  task_params_json: Record<string, unknown> | null;
  last_run_at: string | null;
  last_run_session_id: string | null;
  last_status:
    | "success"
    | "error"
    | "running"
    | "skipped"
    | "cancelled"
    | "pre_failed"
    | "agent_failed"
    | null;
  last_error: string | null;
  /** Derived (server-side) "what badge should the list page show". Equal
   * to ``last_status`` when a fire has stamped one, otherwise ``waiting``
   * for enabled jobs that haven't fired yet and ``paused`` for disabled
   * jobs. Always non-null — saves every API consumer from re-deriving
   * the same fallback. */
  effective_status:
    | "success"
    | "error"
    | "running"
    | "skipped"
    | "cancelled"
    | "pre_failed"
    | "agent_failed"
    | "waiting"
    | "paused"
    | string;
  created_at: string;
  updated_at: string;
};

export type CronJobListResponse = {
  items: CronJob[];
  total: number;
};

export type CronJobFormValues = {
  name: string;
  cron_expression: string;
  timezone: string;
  /** Legacy. Omit when ``task`` is set. */
  input_template?: string;
  max_concurrency?: number;
  timeout_seconds?: number;
  enabled?: boolean;
  /** Send ``null`` to clear an existing pre_action; omit to leave unchanged. */
  pre_action?: CronPreAction | null;
  /** Task-pipeline payload; preferred over ``input_template``. */
  task?: CronTask | null;
};

export type CronJobRunListResponse = {
  items: CronJobRun[];
};

export type CronJobState = {
  last_run_at: string | null;
  last_status: string | null;
  last_error: string | null;
};

export type AssistantChannel = {
  id: string;
  name: string;
  type: "feishu" | "websocket" | "http" | string;
  enabled: boolean;
  agent_id: string;
  status: string;
  last_error: string;
  last_connected_at: string | null;
  config: {
    app_id?: string;
    domain?: string;
    thinking_card_id?: string;
    tool_call_card_id?: string;
    rich_text_card_id?: string;
    [key: string]: unknown;
  };
  secret_keys: string[];
  created_at: string;
  updated_at: string;
};

export type AssistantChannelListResponse = {
  items: AssistantChannel[];
  total: number;
};

export type CreateAssistantChannelPayload = {
  name: string;
  type: string;
  enabled: boolean;
  agent_id: string;
  config: Record<string, unknown>;
  secrets: Record<string, string>;
};

export type UpdateAssistantChannelPayload = Partial<CreateAssistantChannelPayload>;

export type AssistantChannelSecretCopyResponse = {
  secret_key: string;
  value: string;
};

export type CreateAgentPayload = {
  name: string;
  status?: "active" | "inactive";
  system_prompt: string;
  system_prompt_template_id?: string | null;
  model_route_name?: string;
  tool_configs?: AgentToolConfig[];
  tool_names?: string[];
  skill_names?: string[];
  max_turns?: number;
  context_compaction?: Partial<AgentContextCompaction>;
};

export type AgentContextCompaction = {
  enabled: boolean;
  mode: string;
  trigger_strategy: string;
  auto_threshold_tokens: number;
  warning_threshold_tokens: number;
  preserve_recent_messages: number;
  preserve_recent_tool_pairs: number;
  micro_compaction_enabled: boolean;
  tool_result_max_chars: number;
  full_compaction_enabled: boolean;
  summary_model_route_name: string;
  allow_slash_compact: boolean;
};

// ---------------------------------------------------------------------------
// 打板复盘 / 盘面 (market review) — three whole-market data axes.
// Mirrors the backend operation envelopes in
// doyoutrade/api/operations/{data_market_breadth,data_lhb,data_fund_flow}.py.
// The HTTP endpoints (POST /data/breadth, /data/lhb, /data/fund-flow) return
// the tool's ``data`` payload directly; a non-2xx (e.g. market_breadth_empty)
// is thrown as an ApiError carrying ``errorCode`` / ``status`` / ``message``.
// ---------------------------------------------------------------------------

/**
 * Rule-based single-day sentiment thermometer emitted by /data/breadth.
 * ``label`` ∈ {退潮/低迷, 中性, 分歧加剧, 发酵/活跃, 高潮/亢奋}. ``disclaimer``
 * is a fixed compliance string (single-day snapshot, non-predictive, not
 * investment advice) that MUST always be surfaced. ``inputs`` echoes the raw
 * aggregates the label was derived from.
 */
export type MarketBreadthSentiment = {
  label: string;
  reason: string;
  disclaimer: string;
  inputs: {
    limit_up_count: number;
    limit_down_count: number;
    broken_board_count: number;
    max_streak: number;
    broken_board_rate: number;
  };
};

/** Payload of ``POST /data/breadth`` (status ``ok`` | ``partial``). */
export type MarketBreadthData = {
  status: string;
  /** Compact ``YYYYMMDD`` trade date resolved by the backend. */
  trade_date: string;
  data_source: string;
  limit_up_count: number;
  limit_down_count: number;
  broken_board_count: number;
  /** 炸板率 as a 0..1 ratio (multiply by 100 to display a percent). */
  broken_board_rate: number;
  max_streak: number;
  /** 连板梯队: consecutive-limit height (e.g. ``"1"`` / ``"2"``) → 家数. */
  ladder: Record<string, number>;
  sentiment: MarketBreadthSentiment;
  /** Named pool that failed while others succeeded (present on status=partial). */
  pool_errors?: Record<string, string>;
};

/** One 龙虎榜 preview row from ``POST /data/lhb`` (``latest``). */
export type LhbRow = {
  symbol: string;
  code: string;
  name: string;
  on_date: string;
  reason: string;
  interpretation: string;
  change_pct: number | null;
  close_price: number | null;
  /** 龙虎榜净买额 (signed; positive = net buy). */
  net_buy_amount: number | null;
  buy_amount: number | null;
  sell_amount: number | null;
  turnover_rate: number | null;
  circulating_mv: number | null;
};

/** Payload of ``POST /data/lhb``. */
export type LhbData = {
  status: string;
  start_date: string;
  end_date: string;
  count: number;
  latest: LhbRow[];
};

/** One 资金流排名 preview row from ``POST /data/fund-flow`` (``latest``). */
export type FundFlowRow = {
  name: string;
  symbol: string;
  code: string;
  latest_price: number | null;
  change_pct: number | null;
  /** 主力净流入 (signed money). */
  main_net_amount: number | null;
  /** 主力净占比 as a percent number (e.g. ``2.61`` = +2.61%). */
  main_net_pct: number | null;
  super_large_net_amount: number | null;
  large_net_amount: number | null;
  medium_net_amount: number | null;
  small_net_amount: number | null;
  /** Sector领涨股 (only meaningful for scope=sector). */
  lead_stock: string | null;
};

/** Payload of ``POST /data/fund-flow``. */
export type FundFlowData = {
  status: string;
  scope: "individual" | "sector" | string;
  period: string;
  sector_type?: string;
  count: number;
  top: number;
  latest: FundFlowRow[];
};

export type FundFlowScope = "individual" | "sector";

/** concept = 概念板块; industry = 行业板块. */
export type SectorHeatType = "concept" | "industry";

/** One 题材 / 板块热度 preview row from ``POST /data/sector-heat`` (``latest``). */
export type SectorHeatRow = {
  board_name: string;
  board_code: string;
  sector_type: string;
  /** 板块涨跌幅 as a percent number (e.g. ``2.5`` = +2.5%). */
  change_pct: number | null;
  /** 总市值 in 元 (100亿 = 1e10). */
  total_mv: number | null;
  /** 换手率 as a percent number. */
  turnover_rate: number | null;
  up_count: number | null;
  down_count: number | null;
  /** 领涨股 name (empty when the upstream omits it). */
  leader_stock: string;
  /** 领涨股 涨跌幅 as a percent number. */
  leader_change_pct: number | null;
  provider: string;
};

/** Payload of ``POST /data/sector-heat``. */
export type SectorHeatData = {
  status: string;
  sector_type: "concept" | "industry" | string;
  count: number;
  top: number;
  latest: SectorHeatRow[];
};

// ---------------------------------------------------------------------------
// 系统配置 (static / low-frequency YAML config in ~/.doyoutrade) — contract A/B.
//
// Two config surfaces reachable from the Settings page:
//  - doyoutrade global config  (GET/PUT /config)             → {@link DoyoutradeConfigResponse}
//  - qmt-proxy server config  (GET/PUT /qmt-proxy/config)   → {@link QmtProxyConfigResponse}
//
// Secret values are masked on read as ``"********"`` with a companion
// ``<field>_set: boolean`` flag. On write, sending ``"********"`` (or omitting
// the field) keeps the stored value unchanged. Restart-required leaf paths are
// enumerated by the backend in ``restart_required_fields`` — the UI derives its
// "需重启" badges from that list rather than hard-coding.
// ---------------------------------------------------------------------------

/** ``values.data.tushare`` block of the doyoutrade config. */
export type DoyoutradeConfigTushare = {
  /** Masked (``"********"``) when set; empty string otherwise. */
  token: string;
  token_set: boolean;
  timeout_seconds: number;
};

export type DoyoutradeConfigValues = {
  server: {
    host: string;
    port: number;
    tick_seconds: number;
  };
  data: {
    default_provider: string;
    tushare: DoyoutradeConfigTushare;
  };
  market_data: {
    database_url: string;
    lookback_years: number;
    default_provider: string;
    sync_on_startup: boolean;
    sync_concurrency: number;
    provider_rate_limit_per_second: number;
    sync_full_market: boolean;
  };
  observability: {
    service_name: string;
    log_level: string;
    console_enabled: boolean;
    tracing_enabled: boolean;
  };
  review: {
    symbol_scope_mode: string;
  };
  retention: {
    enabled: boolean;
    observability_ttl_days: number;
    prune_interval_hours: number;
    prune_on_startup: boolean;
  };
  assistant: {
    tool_result_max_chars: number;
    approval_allowlist: {
      rule_keys: string[];
      command_prefixes: string[];
    };
  };
  /** Release-based 自动更新 (all leaves hot-reload, no restart needed). */
  auto_update: {
    enabled: boolean;
    check_interval_hours: number;
    repo: string;
  };
  database: {
    url: string;
    echo: boolean;
    pool_pre_ping: boolean;
  };
  /** Embedded qmt-proxy launcher knobs on the doyoutrade side (distinct from the
   * qmt-proxy server's own config below). */
  qmt_proxy: {
    host: string;
    port: number;
    mode: string;
    grpc_enabled: boolean;
    /** Masked (``"********"``) when set; empty string otherwise. */
    local_token: string;
    local_token_set: boolean;
  };
  feishu: {
    enabled: boolean;
    app_id: string;
    /** Masked when set. */
    app_secret: string;
    app_secret_set: boolean;
    /** Masked when set. */
    encrypt_key: string;
    encrypt_key_set: boolean;
    /** Masked when set. */
    verification_token: string;
    verification_token_set: boolean;
    domain: string;
  };
};

/** Response of ``GET /config``. */
export type DoyoutradeConfigResponse = {
  path: string;
  values: DoyoutradeConfigValues;
  /** Dotted leaf paths (e.g. ``"server.port"``) that need a restart to apply. */
  restart_required_fields: string[];
};

/** Response of ``PUT /config`` and ``PUT /qmt-proxy/config``. */
export type ConfigUpdateResponse = {
  status: string;
  restart_required: boolean;
  restart_fields: string[];
  path: string;
};

// ---------------------------------------------------------------------------
// 自动更新 (release-based self-update; GET /update/status, POST /update/check,
// POST /update/apply). A newer GitHub release only surfaces a prompt — the
// install + restart runs when the user explicitly applies.
// ---------------------------------------------------------------------------

/** Latest known GitHub release, as reported by ``GET /update/status``. */
export type UpdateReleaseInfo = {
  /** Normalized version, e.g. ``"0.2.0"``. */
  version: string;
  /** Raw tag name, e.g. ``"v0.2.0"`` — the git ref that would be installed. */
  tag: string;
  name: string | null;
  published_at: string | null;
  html_url: string | null;
  /** Release notes body (markdown). */
  notes: string | null;
};

/** Response of ``GET /update/status`` / ``POST /update/check`` / ``POST /update/apply``. */
export type UpdateStatus = {
  enabled: boolean;
  check_interval_hours: number;
  repo: string;
  current_version: string;
  /** ``package`` (uv tool install) or ``source`` (dev checkout; apply refused). */
  install_kind: "package" | "source";
  state: "idle" | "checking" | "restarting";
  update_available: boolean;
  latest: UpdateReleaseInfo | null;
  last_checked_at: string | null;
  last_error: {
    error_code: string;
    message: string;
    hint?: string | null;
    at: string;
  } | null;
  /** False when the server cannot restart itself (apply would be refused). */
  restart_supported: boolean;
};

/** One multi-terminal client entry in the qmt-proxy ``xtquant.clients`` list. */
export type QmtClientConfig = {
  client_id: string;
  name?: string | null;
  qmt_userdata_path?: string | null;
  mode?: string | null;
  allow_real_trading?: boolean;
  is_data_source?: boolean;
  [key: string]: unknown;
};

export type QmtProxyConfigValues = {
  xtquant: {
    mode: string;
    data: {
      qmt_userdata_path: string | null;
    };
    trading: {
      allow_real_trading: boolean;
    };
    clients: QmtClientConfig[];
    default_client_id: string | null;
    data_source_client_id: string | null;
  };
  security: {
    /** Masked (``["********"]``) when set. */
    api_keys: string[];
    api_keys_set: boolean;
    api_keys_count: number;
  };
  logging: {
    level: string;
  };
  grpc: {
    enabled: boolean;
    host: string;
    port: number;
  };
  app: {
    host: string;
    port: number;
  };
};

/** Response of ``GET /qmt-proxy/config`` (transparently forwarded from the
 * qmt-proxy ``/api/v1/config`` payload's ``data``). */
export type QmtProxyConfigResponse = {
  path: string;
  app_mode: string;
  values: QmtProxyConfigValues;
  /** ``resolve_clients()`` per-item ``model_dump`` — display-only. */
  resolved_clients: Array<Record<string, unknown>>;
  /** Every writable field needs a proxy restart; hence this = all fields. */
  restart_required_fields: string[];
};
