import {
  BarChart,
  Bar,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
} from 'recharts'
import { FeeBreakdown } from '../types'
import { formatStrategy } from '../utils/strategies'

interface FeeBreakdownProps {
  data: FeeBreakdown | null
}

const currencyFormatter = (value: number) => {
  if (Math.abs(value) >= 1000) {
    return `$${(value / 1000).toFixed(1)}k`
  }
  return `$${value.toFixed(2)}`
}

export function FeeBreakdownComponent({ data }: FeeBreakdownProps) {
  if (!data) {
    return (
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {[1, 2].map((i) => (
          <div key={i} className="bg-gray-900 rounded-xl p-6 h-64" />
        ))}
      </div>
    )
  }

  // Format strategy names for display
  const strategiesData = data.by_strategy.map((item) => ({
    ...item,
    displayName: formatStrategy(item.strategy),
  }))

  return (
    <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
      {/* Fees by Platform */}
      <div className="bg-gray-900 rounded-xl p-6">
        <h3 className="text-lg font-semibold text-gray-100 mb-4">Fees by Platform</h3>
        <ResponsiveContainer width="100%" height={250}>
          <BarChart
            data={data.by_platform}
            layout="vertical"
            margin={{ top: 5, right: 30, left: 100, bottom: 5 }}
          >
            <CartesianGrid strokeDasharray="3 3" stroke="#374151" />
            <XAxis type="number" stroke="#9ca3af" style={{ fontSize: '12px' }} />
            <YAxis
              type="category"
              dataKey="platform"
              stroke="#9ca3af"
              style={{ fontSize: '12px' }}
              width={95}
            />
            <Tooltip
              contentStyle={{
                backgroundColor: '#111827',
                border: '1px solid #374151',
                borderRadius: '8px',
              }}
              labelStyle={{ color: '#f3f4f6' }}
              formatter={(value: number) => currencyFormatter(value)}
            />
            <Bar dataKey="total_fees" fill="#f59e0b" radius={[0, 8, 8, 0]} />
          </BarChart>
        </ResponsiveContainer>
      </div>

      {/* Fees by Strategy */}
      <div className="bg-gray-900 rounded-xl p-6">
        <h3 className="text-lg font-semibold text-gray-100 mb-4">Fees by Strategy</h3>
        <ResponsiveContainer width="100%" height={250}>
          <BarChart
            data={strategiesData}
            layout="vertical"
            margin={{ top: 5, right: 30, left: 120, bottom: 5 }}
          >
            <CartesianGrid strokeDasharray="3 3" stroke="#374151" />
            <XAxis type="number" stroke="#9ca3af" style={{ fontSize: '12px' }} />
            <YAxis
              type="category"
              dataKey="displayName"
              stroke="#9ca3af"
              style={{ fontSize: '12px' }}
              width={115}
            />
            <Tooltip
              contentStyle={{
                backgroundColor: '#111827',
                border: '1px solid #374151',
                borderRadius: '8px',
              }}
              labelStyle={{ color: '#f3f4f6' }}
              formatter={(value: number) => currencyFormatter(value)}
            />
            <Bar dataKey="total_fees" fill="#8b5cf6" radius={[0, 8, 8, 0]} />
          </BarChart>
        </ResponsiveContainer>
      </div>
    </div>
  )
}
