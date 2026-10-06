// Shapes returned by the backend API. Money/quantities arrive as decimal STRINGS and are
// only converted to numbers for display/charting — the backend does all arithmetic in Decimal.

export type Dec = string | null;

export interface Level {
  price: string;
  size: string;
}

export interface OrderRow {
  id: string;
  window_id: string;
  market_ticker: string;
  side: "UP" | "DOWN";
  kalshi_side: "yes" | "no";
  limit_price: string;
  quantity: string;
  filled_qty: string;
  remaining_qty?: string;
  avg_fill_price?: Dec;
  principal_cost: string;
  fees_paid: string;
  fee_reserved: string;
  reserved_remaining: string;
  status: "pending" | "partially_filled" | "filled" | "expired" | "canceled";
  display_status?: string;
  close_reason: string | null;
  settled: number;
  submitted_at: string;
  first_fill_at: string | null;
  last_fill_at: string | null;
  closed_at: string | null;
  gate?: OrderGate | null;
}

export interface OrderGate {
  state: string; // blocked | stale | no_book | waiting_for_price | no_offers | revalidating | filled | liquidity_used
  text: string;
  best_ask?: string | null;
  best_ask_size?: string | null;
  limit?: string;
  cents_away?: string;
  book_requested_at?: string;
}

export interface DiagnosisCheck {
  key: string;
  status: "ok" | "wait" | "block" | "info";
  title: string;
  detail: string;
}

export interface Diagnosis {
  headline: string;
  status: "blocked" | "waiting" | "waiting_for_price" | "ok";
  detail: string;
  checks: DiagnosisCheck[];
  next_entry_at: string | null;
}

export interface WindowRow {
  id: string;
  window_start: string;
  window_end: string;
  eligible: number;
  status: "skipped" | "active" | "awaiting_settlement" | "settled" | "abandoned";
  skip_reason: string | null;
  market_ticker: string | null;
  event_ticker: string | null;
  market_url: string | null;
  strike_type: string | null;
  floor_strike: Dec;
  up_qty: string;
  up_cost: string;
  down_qty: string;
  down_cost: string;
  fees: string;
  pairs_credited: string;
  pair_credit_amount: string;
  mark_up: Dec;
  mark_down: Dec;
  mark_source: string | null;
  market_status: string | null;
  market_result: string | null;
  settlement_note: string | null;
  payout: Dec;
  net_pnl: Dec;
  outcome_class: string | null;
  settled_at: string | null;
  orders?: OrderRow[];
  matched_pairs?: string;
  unmatched_side?: "UP" | "DOWN" | null;
  unmatched_qty?: string;
  position_value?: string;
}

export interface FillRow {
  id: string;
  order_id: string;
  window_id: string;
  market_ticker: string;
  side: "UP" | "DOWN";
  level_price?: string;
  price: string;
  quantity: string;
  principal: string;
  fee: string;
  liquidity: string;
  filled_at: string;
  observation_id?: number;
  trigger_observation_id?: number | null;
}

export interface Estimate {
  side: string;
  requested_qty: string;
  limit_price: string;
  depth_at_or_below_limit: string;
  fill_qty: string;
  shortfall: string;
  vwap: Dec;
  best_price: Dec;
  worst_price: Dec;
  principal: string;
  est_fee: string;
  levels: { price: string; displayed: string; available: string; take: string }[];
  next_level_above_limit: { price: string; size: string } | null;
}

export interface PriceCheckSide {
  kalshi_side: string;
  best_bid: Level | null;
  executable_ask: (Level & { derived_from: string }) | null;
  estimate: Estimate;
  current_order_estimate: Estimate | null;
  summary: { bid: Dec; ask: Dec; last_trade: Dec; last_trade_note: string | null };
}

export interface SummaryComparison {
  status: "consistent" | "discrepancy" | "rejected";
  reason: string | null;
  skew_seconds: number | null;
  rows: { measure: string; summary: Dec; book: Dec; match: boolean }[];
}

export interface AppState {
  paper: boolean;
  paper_label: string;
  mode: "live" | "preview";
  preview: boolean;
  version: string;
  server_time: string;
  server_time_local: string;
  timezone: string;
  engine: {
    state: "running" | "paused" | "stopped";
    trading_enabled: boolean;
    armed_at: string | null;
    paused_at: string | null;
    started_at: string | null;
    last_tick_at: string | null;
    last_tick_error: string | null;
    entry_status: { state: string; message: string };
    pair_accounting: string;
    execution_delay_seconds: number;
    stale_after_seconds: number;
    entry_grace_seconds: number;
  };
  account: {
    account_id: number;
    created_at: string;
    starting_balance: string;
    cash: string;
    reserved: string;
    available: string;
    position_value: string;
    open_cost: string;
    open_up_qty: string;
    open_down_qty: string;
    unmatched_cost: string;
    equity: string;
    realized_pnl: string;
    unrealized_pnl: string;
    total_pnl: string;
    fees_total: string;
  };
  feed: {
    state: "starting" | "ok" | "error" | "rate_limited";
    last_ok_at: string | null;
    last_ok_age_seconds: number | null;
    last_error: string | null;
    last_error_at: string | null;
    consecutive_failures: number;
    rate_limited_until: string | null;
    retry_at: string | null;
    source: string;
    base_url: string;
    requests: number;
    exchange: { exchange_active: boolean; trading_active: boolean; fetched_at: string } | null;
  };
  schedule: {
    current: {
      start: string;
      end: string;
      eligible: boolean;
      seconds_remaining: number;
      status: string | null;
      skip_reason: string | null;
    };
    next_eligible: { start: string; end: string; seconds_until: number }[];
    hour_slots: {
      start: string;
      end: string;
      label: string;
      eligible: boolean;
      status: string;
      note: string | null;
      net_pnl: Dec;
      is_current: boolean;
    }[];
    eligible_minutes: number[];
  };
  market: null | {
    ticker: string;
    event_ticker: string;
    title: string;
    url: string;
    status: string;
    result: string | null;
    open_time: string | null;
    close_time: string | null;
    strike_type: string | null;
    target_price: Dec;
    yes_sub_title: string;
    rules_primary: string;
    mapping: { UP: string; DOWN: string };
    mapping_basis: string;
    contract_value: Dec;
    tick_size: Dec;
    validation_ok: boolean | null;
    validation_checks: string[];
    validation_warnings: string[];
    validation_error: string | null;
  };
  book: null | {
    ticker: string;
    label: string;
    source: string;
    format: string;
    requested_at: string;
    received_at: string;
    server_date: string | null;
    latency_ms: number;
    age_seconds: number;
    valid: boolean;
    problems: string[];
    yes_bids: Level[];
    no_bids: Level[];
    yes_asks: Level[];
    no_asks: Level[];
    rejected_out_of_order: number;
  };
  summary_quotes: null | {
    label: string;
    fetched_at: string;
    latency_ms: number;
    age_seconds: number;
    yes_bid: Dec;
    yes_bid_size: Dec;
    yes_ask: Dec;
    yes_ask_size: Dec;
    no_bid: Dec;
    no_ask: Dec;
    last_price: Dec;
    status: string;
  };
  summary_comparison: SummaryComparison | null;
  price_check: null | {
    ticker: string;
    limit_price: string;
    quantity: string;
    fee_mode: string;
    fee_estimate_uncertain: boolean;
    observation: {
      source: string;
      requested_at: string;
      received_at: string;
      latency_ms: number;
      age_seconds: number;
      valid: boolean;
      problems: string[];
    };
    sides: { UP: PriceCheckSide; DOWN: PriceCheckSide };
  };
  current_window: WindowRow | null;
  open_windows: WindowRow[];
  recent_fills: FillRow[];
  fee_model: null | {
    series_ticker: string;
    fee_type: string;
    fee_multiplier: string;
    uncertain: boolean;
    changes_error?: string | null;
    taker_rate: string;
    formula: string;
    source: string;
    fetched_at: string;
  };
  settings: Settings;
  events: { ts: string; level: string; kind: string; message: string; window_id: string | null }[];
  diagnosis: Diagnosis;
  clock: { measured_offset_seconds: number | null; applied_offset_seconds: number; source: string };
  last_gap: { from: string; to: string; seconds: number; message: string } | null;
}

export interface Settings {
  starting_balance: string;
  limit_price: string;
  contracts_per_side: string;
  eligible_minutes: number[];
  display_timezone: string;
  fee_mode: string;
}

export interface PriceTick {
  ts: string;
  kind: "book" | "summary";
  yes_bid: Dec;
  yes_ask: Dec;
  no_bid: Dec;
  no_ask: Dec;
  last_price: Dec;
  latency_ms: number | null;
}

export interface Analytics {
  caveat: string;
  windows: {
    recorded: number;
    entered: number;
    settled: number;
    awaiting_settlement: number;
    active: number;
    skipped: number;
    skip_reasons: Record<string, number>;
    partial_fill_windows: number;
  };
  outcomes: Record<string, { windows: number; settled: number; net_pnl: string }>;
  pnl: {
    total_net: string;
    average_per_settled_window: Dec;
    profitable_windows: number;
    losing_windows: number;
    flat_windows: number;
    best_window: Dec;
    worst_window: Dec;
  };
  fills: {
    by_side: Record<string, { fills: number; contracts: string; intended_contracts: string; fill_rate: Dec; avg_price: Dec }>;
    total_fills: number;
    price_improvement_total: string;
  };
  fees: { total: string; average_per_entered_window: Dec };
  per_window: { window_start: string; market_ticker: string; net_pnl: string; outcome_class: string; fees: string; result: string; cumulative_pnl: string }[];
}

export interface EquityPoint {
  ts: string;
  cash: string;
  reserved: string;
  position_value: string;
  equity: string;
  realized_pnl: string;
  unrealized_pnl: string;
  reason: string;
}

export interface Comparison {
  ticker: string;
  side: string;
  quote_type: string;
  observed_at: string;
  verdict: "consistent" | "discrepancy" | "inconclusive";
  measure: string | null;
  api_value: Dec;
  api_observed_at: string | null;
  skew_seconds: number | null;
  visible_price: string;
  notes: string[];
  other_measures: { measure: string; value: Dec; rounds_to_visible: boolean }[];
}

export type LayoutId = "simple" | "terminal" | "analytics";
