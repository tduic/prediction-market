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

/**
 * Format a strategy ID to a human-readable label
 * Falls back to the ID itself if not in the map
 */
export function formatStrategy(id: string): string {
  return STRATEGY_LABELS[id] || id
}
