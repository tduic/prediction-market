export interface OverviewData {
  total_capital: number
  cash: number
  deployed: number
  open_positions: number
  unrealized_pnl: number
  realized_pnl_total: number
  net_return_pct: number
  total_fees: number
  snapshotted_at: string | null
  signals_24h: number
  violations_24h: number
  daily_loss_pct_used: number
}

export interface StrategyMetrics {
  strategy: string
  trade_count: number
  win_count: number
  win_rate: number
  avg_pnl: number
  total_pnl: number
  total_fees: number
  net_pnl?: number
  sharpe_ratio: number
  avg_edge_capture: number
  avg_execution_time_ms: number
}

export interface StrategyPnlPoint {
  snapshotted_at: string
  strategy: string
  realized_pnl: number
  unrealized_pnl: number
  fees: number
  trade_count: number
  win_count: number
}

export interface EquityCurvePoint {
  snapshotted_at: string
  total_capital: number
  unrealized_pnl: number
  realized_pnl_total: number
  fees_total: number
}

export interface Trade {
  id: string
  signal_id: string
  strategy: string
  market_id_a: string | null
  market_id_b: string | null
  predicted_edge: number
  predicted_pnl: number
  actual_pnl: number
  fees_total: number
  edge_captured_pct: number
  signal_to_fill_ms: number | null
  resolved_at: string
}

export interface FeeBreakdownByPlatform {
  platform: string
  total_fees: number
}

export interface FeeBreakdownByStrategy {
  strategy: string
  total_fees: number
}

export interface FeeBreakdown {
  total_fees: number
  by_platform: FeeBreakdownByPlatform[]
  by_strategy: FeeBreakdownByStrategy[]
}

export interface RiskMetrics {
  max_drawdown: number
  max_drawdown_pct: number
  concentration_pct: number
  daily_var: number
  daily_var_sample_size: number
  sharpe_overall: number
  sharpe_sample_size: number
}

export interface CircuitBreakerStatus {
  tripped: boolean
  reason: string | null
  tripped_at: string | null
  daily_loss: number
  daily_loss_limit: number
  daily_loss_limit_pct: number
  daily_loss_available: boolean
  daily_loss_pct_used: number
}

export interface SystemHealth {
  status: 'ok' | 'warn' | 'critical'
  issues: string[]
  circuit_breaker: { tripped: boolean; reason: string | null } | { error: string }
  reconciliation_discrepancies_24h: number | null
  last_reconciliation_age_s: number | null
  invariant_violations_24h: number | null
  last_snapshot_age_s: number | null
  last_signal_age_s: number | null
  signals_24h: number | null
  signals_24h_by_strategy: Record<string, number> | null
  daily_loss_pct_used: number | null
  /** Persistent execution halt (core/engine/execution_control.get_halt):
   * null when trading is allowed, the halt details when halted, or
   * {error} when the halt state could not be queried. */
  execution_halt: ExecutionHalt | { error: string } | null
  /** Snapshot from the embedded trading session; null for a standalone dashboard. */
  runtime: RuntimeHealth | null
  runtime_age_s?: number | null
}

export interface ExecutionHalt {
  reason: string | null
  component: string | null
  halted_at: string | null
}

export interface RuntimeHealth {
  updated_at: string
  state: 'starting' | 'running' | string
  execution_mode: string
  process_rss_bytes?: number | null
  database_bytes?: number
  pairs_monitored?: number
  pairs_eligible_now?: number
  prices_tracked?: number
  stale_prices_count?: number
  skipped_stale?: number
  skipped_fee_unknown?: number
  skipped_unprofitable?: number
  skipped_depth_unknown?: number
  skipped_thin_book?: number
  last_arb_fired_at?: number | null
  arb_trade_count?: number
  scheduled_trades?: number
  ws_last_tick_age_ms_by_platform?: Record<string, number>
}

export interface DailyPnlPoint {
  date: string
  trade_count: number
  gross_pnl: number
  total_fees: number
  net_pnl: number
  win_count: number
  win_rate: number
}
