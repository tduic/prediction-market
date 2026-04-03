import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  Legend,
  ResponsiveContainer,
} from 'recharts'
import { StrategyPnlPoint } from '../types'
import { formatStrategy } from '../utils/strategies'

interface StrategyPnlChartProps {
  data: StrategyPnlPoint[]
}

const colors = [
  '#10b981',
  '#3b82f6',
  '#f59e0b',
  '#ef4444',
  '#8b5cf6',
  '#ec4899',
  '#14b8a6',
  '#6366f1',
]

const currencyFormatter = (value: number) => {
  if (Math.abs(value) >= 1000) {
    return `$${(value / 1000).toFixed(1)}k`
  }
  return `$${value.toFixed(2)}`
}

const timeFormatter = (dateString: string) => {
  const date = new Date(dateString)
  const month = String(date.getMonth() + 1).padStart(2, '0')
  const day = String(date.getDate()).padStart(2, '0')
  const hours = String(date.getHours()).padStart(2, '0')
  const minutes = String(date.getMinutes()).padStart(2, '0')
  return `${month}/${day} ${hours}:${minutes}`
}

export function StrategyPnlChart({ data }: StrategyPnlChartProps) {
  if (!data || data.length === 0) {
    return (
      <div className="bg-gray-900 rounded-xl p-6">
        <h2 className="text-lg font-semibold text-gray-100 mb-4">Strategy PnL Over Time</h2>
        <div className="h-64 flex items-center justify-center">
          <div className="text-center">
            <p className="text-gray-400 mb-2">No data available</p>
            <p className="text-gray-500 text-sm">Snapshots populate every 30 seconds after trading begins</p>
          </div>
        </div>
      </div>
    )
  }

  // Group by timestamp and pivot strategies into columns
  const groupedData: Record<string, Record<string, string | number>> = {}

  data.forEach((point) => {
    if (!groupedData[point.snapshotted_at]) {
      groupedData[point.snapshotted_at] = {
        time: timeFormatter(point.snapshotted_at),
        _sortKey: point.snapshotted_at,
      }
    }
    groupedData[point.snapshotted_at][formatStrategy(point.strategy)] = point.realized_pnl
  })

  const chartData = Object.values(groupedData).sort(
    (a, b) => String(a._sortKey).localeCompare(String(b._sortKey)),
  )

  // Get unique strategies (formatted)
  const strategies = Array.from(
    new Set(data.map((d) => formatStrategy(d.strategy))),
  )

  return (
    <div className="bg-gray-900 rounded-xl p-6">
      <h2 className="text-lg font-semibold text-gray-100 mb-4">Strategy PnL Over Time</h2>
      <ResponsiveContainer width="100%" height={300}>
        <LineChart data={chartData}>
          <CartesianGrid strokeDasharray="3 3" stroke="#374151" />
          <XAxis
            dataKey="time"
            stroke="#9ca3af"
            style={{ fontSize: '12px' }}
          />
          <YAxis
            stroke="#9ca3af"
            tickFormatter={currencyFormatter}
            style={{ fontSize: '12px' }}
          />
          <Tooltip
            contentStyle={{
              backgroundColor: '#111827',
              border: '1px solid #374151',
              borderRadius: '8px',
            }}
            labelStyle={{ color: '#f3f4f6' }}
            formatter={(value: number) => `$${value.toFixed(2)}`}
          />
          <Legend
            wrapperStyle={{ paddingTop: '20px' }}
            iconType="line"
          />
          {strategies.map((strategy, idx) => (
            <Line
              key={strategy}
              type="monotone"
              dataKey={strategy}
              stroke={colors[idx % colors.length]}
              dot={false}
              strokeWidth={2}
              isAnimationActive={false}
            />
          ))}
        </LineChart>
      </ResponsiveContainer>
    </div>
  )
}
