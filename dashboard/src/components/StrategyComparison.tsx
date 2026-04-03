import {
  BarChart,
  Bar,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
} from 'recharts'
import { StrategyMetrics } from '../types'
import { formatStrategy } from '../utils/strategies'

interface StrategyComparisonProps {
  data: StrategyMetrics[]
}

const percentFormatter = (value: number) => `${(value * 100).toFixed(1)}%`

const currencyFormatter = new Intl.NumberFormat('en-US', {
  style: 'currency',
  currency: 'USD',
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
})

export function StrategyComparison({ data }: StrategyComparisonProps) {
  if (!data || data.length === 0) {
    return (
      <div className="space-y-4">
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
          {[1, 2].map((i) => (
            <div key={i} className="bg-gray-900 rounded-xl p-6 h-64" />
          ))}
        </div>
      </div>
    )
  }

  const chartData = data.map((metric) => ({
    strategy: formatStrategy(metric.strategy),
    strategyId: metric.strategy,
    winRate: metric.win_rate,
    sharpeRatio: metric.sharpe_ratio,
  }))

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {/* Win Rate Chart */}
        <div className="bg-gray-900 rounded-xl p-6">
          <h3 className="text-lg font-semibold text-gray-100 mb-4">Win Rate by Strategy</h3>
          <ResponsiveContainer width="100%" height={250}>
            <BarChart data={chartData}>
              <CartesianGrid strokeDasharray="3 3" stroke="#374151" />
              <XAxis
                dataKey="strategy"
                stroke="#9ca3af"
                style={{ fontSize: '12px' }}
                angle={-45}
                textAnchor="end"
                height={80}
              />
              <YAxis
                stroke="#9ca3af"
                tickFormatter={percentFormatter}
                style={{ fontSize: '12px' }}
              />
              <Tooltip
                contentStyle={{
                  backgroundColor: '#111827',
                  border: '1px solid #374151',
                  borderRadius: '8px',
                }}
                labelStyle={{ color: '#f3f4f6' }}
                formatter={(value: number) => `${(value * 100).toFixed(1)}%`}
              />
              <Bar dataKey="winRate" fill="#10b981" radius={[8, 8, 0, 0]} />
            </BarChart>
          </ResponsiveContainer>
        </div>

        {/* Sharpe Ratio Chart */}
        <div className="bg-gray-900 rounded-xl p-6">
          <h3 className="text-lg font-semibold text-gray-100 mb-4">Sharpe Ratio by Strategy</h3>
          <ResponsiveContainer width="100%" height={250}>
            <BarChart data={chartData}>
              <CartesianGrid strokeDasharray="3 3" stroke="#374151" />
              <XAxis
                dataKey="strategy"
                stroke="#9ca3af"
                style={{ fontSize: '12px' }}
                angle={-45}
                textAnchor="end"
                height={80}
              />
              <YAxis
                stroke="#9ca3af"
                style={{ fontSize: '12px' }}
              />
              <Tooltip
                contentStyle={{
                  backgroundColor: '#111827',
                  border: '1px solid #374151',
                  borderRadius: '8px',
                }}
                labelStyle={{ color: '#f3f4f6' }}
                formatter={(value: number) => value.toFixed(2)}
              />
              <Bar
                dataKey="sharpeRatio"
                fill="#3b82f6"
                rx={4}
                shape={<CustomBar />}
              />
            </BarChart>
          </ResponsiveContainer>
        </div>
      </div>

      {/* Strategy Metrics Table */}
      <div className="bg-gray-900 rounded-xl p-6">
        <h3 className="text-lg font-semibold text-gray-100 mb-4">Strategy Metrics Summary</h3>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-gray-800">
                <th className="text-left px-4 py-3 text-gray-400 font-medium">Strategy</th>
                <th className="text-right px-4 py-3 text-gray-400 font-medium">Trades</th>
                <th className="text-right px-4 py-3 text-gray-400 font-medium">Win Rate</th>
                <th className="text-right px-4 py-3 text-gray-400 font-medium">Total PnL</th>
                <th className="text-right px-4 py-3 text-gray-400 font-medium">Avg PnL</th>
                <th className="text-right px-4 py-3 text-gray-400 font-medium">Sharpe Ratio</th>
                <th className="text-right px-4 py-3 text-gray-400 font-medium">Edge Capture</th>
                <th className="text-right px-4 py-3 text-gray-400 font-medium">Fill Time (ms)</th>
              </tr>
            </thead>
            <tbody>
              {data.map((metric, idx) => {
                const alternateRow = idx % 2 === 1
                return (
                  <tr
                    key={metric.strategy}
                    className={`border-b border-gray-800 ${alternateRow ? 'bg-gray-800/30' : ''}`}
                  >
                    <td className="px-4 py-3 text-gray-100 font-medium">
                      {formatStrategy(metric.strategy)}
                    </td>
                    <td className="px-4 py-3 text-right text-gray-100">
                      {metric.trade_count}
                    </td>
                    <td className="px-4 py-3 text-right text-gray-100">
                      {(metric.win_rate * 100).toFixed(1)}%
                    </td>
                    <td
                      className={`px-4 py-3 text-right font-semibold ${
                        metric.total_pnl >= 0 ? 'text-green-400' : 'text-red-400'
                      }`}
                    >
                      {metric.total_pnl >= 0 ? '+' : ''}
                      {currencyFormatter.format(metric.total_pnl)}
                    </td>
                    <td
                      className={`px-4 py-3 text-right font-semibold ${
                        metric.avg_pnl >= 0 ? 'text-green-400' : 'text-red-400'
                      }`}
                    >
                      {metric.avg_pnl >= 0 ? '+' : ''}
                      {currencyFormatter.format(metric.avg_pnl)}
                    </td>
                    <td className="px-4 py-3 text-right text-gray-100">
                      {metric.sharpe_ratio.toFixed(2)}
                    </td>
                    <td className="px-4 py-3 text-right text-gray-100">
                      {(metric.avg_edge_capture * 100).toFixed(1)}%
                    </td>
                    <td className="px-4 py-3 text-right text-gray-100">
                      {metric.avg_execution_time_ms.toFixed(0)}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  )
}

// Custom bar shape to color bars based on positive/negative values
function CustomBar(props: any) {
  const { x, y, width, height, payload } = props
  const color = payload.sharpeRatio >= 0 ? '#10b981' : '#ef4444'

  return (
    <rect
      x={x}
      y={y}
      width={width}
      height={height}
      rx={4}
      fill={color}
    />
  )
}
