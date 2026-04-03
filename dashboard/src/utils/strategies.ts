/**
 * Strategy naming and formatting utilities
 */

const STRATEGY_LABELS: Record<string, string> = {
  P1_cross_market_arb: 'Cross-Market Arb',
  P2_structured_event: 'Event Modeling',
  P3_calibration_bias: 'Calibration Bias',
  P4_liquidity_timing: 'Liquidity Timing',
  P5_information_latency: 'Info Latency',
}

const STRATEGY_COLORS: Record<string, string> = {
  P1_cross_market_arb: '#10b981', // emerald
  P2_structured_event: '#3b82f6', // blue
  P3_calibration_bias: '#f59e0b', // amber
  P4_liquidity_timing: '#ef4444', // red
  P5_information_latency: '#8b5cf6', // violet
}

/**
 * Format a strategy ID to a human-readable label
 * Falls back to the ID itself if not in the map
 */
export function formatStrategy(id: string): string {
  return STRATEGY_LABELS[id] || id
}

/**
 * Get the color for a strategy
 * Falls back to a default gray if not in the map
 */
export function getStrategyColor(id: string): string {
  return STRATEGY_COLORS[id] || '#6b7280'
}

/**
 * Get all strategy labels as a map
 */
export function getStrategyLabels(): Record<string, string> {
  return { ...STRATEGY_LABELS }
}

/**
 * Get all strategy colors as a map
 */
export function getStrategyColors(): Record<string, string> {
  return { ...STRATEGY_COLORS }
}
