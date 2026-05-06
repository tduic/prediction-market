import { useState } from 'react'
import { useApi } from './hooks/useApi'
import {
  CircuitBreakerStatus,
  OverviewData,
  StrategyMetrics,
  StrategyPnlPoint,
  EquityCurvePoint,
  Trade,
  FeeBreakdown,
  RiskMetrics,
} from './types'
import { CircuitBreakerStatusComponent } from './components/CircuitBreakerStatus'
import { OverviewCards } from './components/OverviewCards'
import { StrategyPnlChart } from './components/StrategyPnlChart'
import { StrategyComparison } from './components/StrategyComparison'
import { EquityCurve } from './components/EquityCurve'
import { FeeBreakdownComponent } from './components/FeeBreakdown'
import { RiskMetricsComponent } from './components/RiskMetrics'
import { TradeLog } from './components/TradeLog'

const REFRESH_INTERVAL = 30000 // 30 seconds

type TimeRange = '1h' | '6h' | '24h' | '7d' | '30d'

const TIME_RANGE_DAYS: Record<TimeRange, number> = {
  '1h': 0.042,  // 1 hour
  '6h': 0.25,   // 6 hours
  '24h': 1,     // 1 day
  '7d': 7,      // 7 days
  '30d': 30,    // 30 days
}

function App() {
  // Time range state
  const [timeRange, setTimeRange] = useState<TimeRange>('24h')
  const [equityTimeRange, setEquityTimeRange] = useState<TimeRange>('7d')
  const days = TIME_RANGE_DAYS[timeRange]
  const equityDays = TIME_RANGE_DAYS[equityTimeRange]

  // Fetch all data with 30-second refresh
  const overviewResult = useApi<OverviewData>('/api/overview', REFRESH_INTERVAL)
  const metricsResult = useApi<StrategyMetrics[]>('/api/strategies', REFRESH_INTERVAL, { days })
  const pnlResult = useApi<StrategyPnlPoint[]>('/api/strategies/pnl-series', REFRESH_INTERVAL, { days })
  const equityResult = useApi<EquityCurvePoint[]>('/api/equity-curve', REFRESH_INTERVAL, { days: equityDays })
  const tradesResult = useApi<Trade[]>('/api/trades', REFRESH_INTERVAL, { days })
  const feeResult = useApi<FeeBreakdown>('/api/fees', REFRESH_INTERVAL)
  const riskResult = useApi<RiskMetrics>('/api/risk', REFRESH_INTERVAL)
  const circuitBreakerResult = useApi<CircuitBreakerStatus>('/api/circuit-breaker', REFRESH_INTERVAL)

  // Get unique strategies for trade log filter
  const strategies = Array.from(
    new Set(
      [
        ...(metricsResult.data?.map((m) => m.strategy) || []),
        ...(tradesResult.data?.map((t) => t.strategy) || []),
      ].filter(Boolean),
    ),
  ).sort()

  // Check if any endpoint is loading
  const isLoading =
    overviewResult.loading ||
    metricsResult.loading ||
    pnlResult.loading ||
    equityResult.loading ||
    tradesResult.loading ||
    feeResult.loading ||
    riskResult.loading ||
    circuitBreakerResult.loading

  // Check for errors
  const errors = [
    overviewResult.error,
    metricsResult.error,
    pnlResult.error,
    equityResult.error,
    tradesResult.error,
    feeResult.error,
    riskResult.error,
    circuitBreakerResult.error,
  ].filter(Boolean)

  // Most recent successful fetch time across all endpoints
  const lastUpdated = [
    overviewResult.lastUpdated,
    metricsResult.lastUpdated,
    pnlResult.lastUpdated,
    equityResult.lastUpdated,
    tradesResult.lastUpdated,
    feeResult.lastUpdated,
    riskResult.lastUpdated,
  ].reduce<Date | null>((latest, d) => {
    if (!d) return latest
    if (!latest) return d
    return d > latest ? d : latest
  }, null)

  return (
    <div className="min-h-screen bg-gray-950 text-gray-100">
      {/* Header */}
      <header className="border-b border-gray-800 bg-gray-900 sticky top-0 z-50">
        <div className="max-w-7xl mx-auto px-4 py-4 sm:px-6 lg:px-8">
          <div className="flex flex-col sm:flex-row justify-between items-start sm:items-center gap-4">
            <div className="flex items-center gap-3">
              <h1 className="text-2xl font-bold">Prediction Market Dashboard</h1>
              <div className="flex items-center gap-2">
                <div
                  className={`w-2.5 h-2.5 rounded-full ${
                    isLoading ? 'bg-yellow-400 animate-pulse' : 'bg-green-400 animate-pulse'
                  }`}
                />
                <span className="text-xs text-gray-400">
                  {isLoading ? 'Updating...' : 'Live'}
                </span>
              </div>
              <CircuitBreakerStatusComponent data={circuitBreakerResult.data} />
            </div>
            <div className="flex flex-col sm:flex-row items-start sm:items-center gap-4 w-full sm:w-auto">
              <div className="flex gap-2">
                <div>
                  <label className="text-xs text-gray-400 block mb-1">Charts Time Range</label>
                  <select
                    value={timeRange}
                    onChange={(e) => setTimeRange(e.target.value as TimeRange)}
                    className="bg-gray-800 text-gray-100 border border-gray-700 rounded-lg px-3 py-1.5 text-xs focus:outline-none focus:border-blue-500"
                  >
                    <option value="1h">Last 1h</option>
                    <option value="6h">Last 6h</option>
                    <option value="24h">Last 24h</option>
                    <option value="7d">Last 7d</option>
                    <option value="30d">Last 30d</option>
                  </select>
                </div>
                <div>
                  <label className="text-xs text-gray-400 block mb-1">Equity Curve</label>
                  <select
                    value={equityTimeRange}
                    onChange={(e) => setEquityTimeRange(e.target.value as TimeRange)}
                    className="bg-gray-800 text-gray-100 border border-gray-700 rounded-lg px-3 py-1.5 text-xs focus:outline-none focus:border-blue-500"
                  >
                    <option value="1h">Last 1h</option>
                    <option value="6h">Last 6h</option>
                    <option value="24h">Last 24h</option>
                    <option value="7d">Last 7d</option>
                    <option value="30d">Last 30d</option>
                  </select>
                </div>
              </div>
              <div className="text-xs text-gray-500 whitespace-nowrap">
                Last refresh: {lastUpdated ? lastUpdated.toLocaleTimeString() : '—'}
              </div>
            </div>
          </div>
        </div>
      </header>

      {/* Error messages */}
      {errors.length > 0 && (
        <div className="max-w-7xl mx-auto px-4 py-4 sm:px-6 lg:px-8">
          <div className="bg-red-900/20 border border-red-800 rounded-lg p-4">
            <p className="text-red-400 text-sm font-medium">
              {errors.length === 1
                ? `Error: ${errors[0]}`
                : `${errors.length} data sources unavailable`}
            </p>
          </div>
        </div>
      )}

      {/* Main content */}
      <main className="max-w-7xl mx-auto px-4 py-8 sm:px-6 lg:px-8 space-y-6">
        {/* Overview Cards */}
        <section>
          <OverviewCards data={overviewResult.data} />
        </section>

        {/* Strategy PnL Over Time */}
        <section>
          <StrategyPnlChart data={pnlResult.data || []} />
        </section>

        {/* Strategy Comparison */}
        <section>
          <StrategyComparison data={metricsResult.data || []} />
        </section>

        {/* Equity Curve */}
        <section>
          <EquityCurve data={equityResult.data || []} />
        </section>

        {/* Fee Breakdown and Risk Metrics */}
        <section className="grid grid-cols-1 lg:grid-cols-3 gap-4">
          <div className="lg:col-span-2">
            <FeeBreakdownComponent data={feeResult.data} />
          </div>
          <div>
            <RiskMetricsComponent data={riskResult.data} />
          </div>
        </section>

        {/* Trade Log */}
        <section>
          <TradeLog data={tradesResult.data || []} strategies={strategies} />
        </section>
      </main>

      {/* Footer */}
      <footer className="border-t border-gray-800 bg-gray-900 mt-12">
        <div className="max-w-7xl mx-auto px-4 py-6 sm:px-6 lg:px-8">
          <p className="text-xs text-gray-500 text-center">
            Data updates every 30 seconds. All times in local timezone.
          </p>
        </div>
      </footer>
    </div>
  )
}

export default App
