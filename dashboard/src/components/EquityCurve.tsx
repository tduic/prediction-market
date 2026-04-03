import {
  AreaChart,
  Area,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  Legend,
  ResponsiveContainer,
  ComposedChart,
} from 'recharts'
import { EquityCurvePoint } from '../types'

interface EquityCurveProps {
  data: EquityCurvePoint[]
}

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

export function EquityCurve({ data }: EquityCurveProps) {
  if (!data || data.length === 0) {
    return (
      <div className="bg-gray-900 rounded-xl p-6">
        <h2 className="text-lg font-semibold text-gray-100 mb-4">Equity Curve</h2>
        <div className="h-64 flex items-center justify-center">
          <div className="text-center">
            <p className="text-gray-400 mb-2">No data available</p>
            <p className="text-gray-500 text-sm">Snapshots populate every 30 seconds after trading begins</p>
          </div>
        </div>
      </div>
    )
  }

  const chartData = data.map((point) => ({
    time: timeFormatter(point.snapshotted_at),
    total_capital: point.total_capital,
    realized_pnl_total: point.realized_pnl_total,
  }))

  return (
    <div className="bg-gray-900 rounded-xl p-6">
      <h2 className="text-lg font-semibold text-gray-100 mb-4">Equity Curve</h2>
      <ResponsiveContainer width="100%" height={300}>
        <ComposedChart data={chartData}>
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
          <Legend wrapperStyle={{ paddingTop: '20px' }} />
          <Area
            type="monotone"
            dataKey="total_capital"
            fill="#3b82f6"
            fillOpacity={0.2}
            stroke="none"
            name="Total Capital"
            isAnimationActive={false}
          />
          <Line
            type="monotone"
            dataKey="realized_pnl_total"
            stroke="#10b981"
            strokeWidth={2}
            dot={false}
            name="Realized PnL"
            isAnimationActive={false}
          />
        </ComposedChart>
      </ResponsiveContainer>
    </div>
  )
}
