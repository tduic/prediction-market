import { RiskMetrics } from '../types'

interface RiskMetricsProps {
  data: RiskMetrics | null
}

const currencyFormatter = new Intl.NumberFormat('en-US', {
  style: 'currency',
  currency: 'USD',
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
})

export function RiskMetricsComponent({ data }: RiskMetricsProps) {
  if (!data) {
    return (
      <div className="bg-gray-900 rounded-xl p-6">
        <h2 className="text-lg font-semibold text-gray-100 mb-4">Risk Metrics</h2>
        <div className="h-40 animate-pulse" />
      </div>
    )
  }

  const sharpeColor = data.sharpe_overall >= 1 ? 'text-green-400' : 'text-red-400'

  return (
    <div className="bg-gray-900 rounded-xl p-6">
      <h2 className="text-lg font-semibold text-gray-100 mb-6">Risk Metrics</h2>
      <div className="space-y-5">
        {/* Max Drawdown */}
        <div className="flex justify-between items-start border-b border-gray-800 pb-4">
          <div>
            <p className="text-gray-400 text-sm">Max Drawdown</p>
            <p className="text-gray-100 text-sm mt-1">
              {currencyFormatter.format(data.max_drawdown)} ({data.max_drawdown_pct.toFixed(2)}%)
            </p>
          </div>
          <div className="text-red-400 text-lg font-semibold">↓</div>
        </div>

        {/* Concentration */}
        <div className="flex justify-between items-start border-b border-gray-800 pb-4">
          <div>
            <p className="text-gray-400 text-sm">Concentration</p>
            <p className="text-gray-100 text-sm mt-1">
              {data.concentration_pct.toFixed(2)}% of capital
            </p>
          </div>
          <div className="text-orange-400 text-lg font-semibold">◐</div>
        </div>

        {/* Daily VaR */}
        <div className="flex justify-between items-start border-b border-gray-800 pb-4">
          <div>
            <p className="text-gray-400 text-sm">Daily VaR (95%)</p>
            <p className="text-gray-100 text-sm mt-1">
              {currencyFormatter.format(data.daily_var)}
            </p>
          </div>
          <div className="text-yellow-400 text-lg font-semibold">⚠</div>
        </div>

        {/* Sharpe Ratio */}
        <div className="flex justify-between items-start">
          <div>
            <p className="text-gray-400 text-sm">Overall Sharpe Ratio</p>
            <p className={`${sharpeColor} text-sm mt-1 font-semibold`}>
              {data.sharpe_overall.toFixed(2)}
            </p>
          </div>
          <div className={`${sharpeColor} text-lg font-semibold`}>
            {data.sharpe_overall >= 1 ? '✓' : '✗'}
          </div>
        </div>
      </div>
    </div>
  )
}
