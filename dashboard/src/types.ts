export interface OverviewData {
  total_capital: number
  cash: number
  deployed: number
  open_positions: number
  unrealized_pnl: number
  realized_pnl_total: number
  fees_total: number
  net_return_pct: number
  total_fees?: number
  total_trades?: number
}

export interface StrategyMetrics {
  strategy: string
  trade_count: number
  win_count: number
  win_rate: number
  avg_pnl: number
  total_pnl: number
  total_fees: number
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
  market_id_a: string
  market_id_b: string
  predicted_edge: number
  predicted_pnl: number
  actual_pnl: number
  fees_total: number
  edge_captured_pct: number
  signal_to_fill_ms: number
  resolved_at: string
}

export interface FeeBreakdownByPlatform {
  platform: string
  total_fees: number
  order_count: number
}

export interface FeeBreakdownByStrategy {
  strategy: string
  total_fees: number
  order_count: number
}

export interface FeeBreakdown {
  by_platform: FeeBreakdownByPlatform[]
  by_strategy: FeeBreakdownByStrategy[]
}

export interface RiskMetrics {
  max_drawdown: number
  max_drawdown_pct: number
  concentration_pct: number
  daily_var: number
  sharpe_overall: number
}
