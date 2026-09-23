import {
  BarChart,
  Bar,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
  Cell,
} from 'recharts'
import { DailyPnlPoint } from '../types'

interface DailyPnlChartProps {
  data: DailyPnlPoint[]
}

const currencyFormatter = (value: number) => {
  if (Math.abs(value) >= 1000) {
    return `$${(value / 1000).toFixed(1)}k`
  }
  return `$${value.toFixed(2)}`
}

export function DailyPnlChart({ data }: DailyPnlChartProps) {
  if (!data || data.length === 0) {
    return (
      <div className="bg-gray-900 rounded-xl p-6">
        <h2 className="text-lg font-semibold text-gray-100 mb-4">Daily P&amp;L</h2>
        <div className="h-48 flex items-center justify-center">
          <p className="text-gray-400">No daily P&amp;L data available</p>
        </div>
      </div>
    )
  }

  // Pad sparse series so every calendar day in the window appears.
  // The API omits days with no trades; we fill them with zero-bars so the
  // x-axis stays proportional and quiet periods are visually distinct.
  const padded: (DailyPnlPoint & { padded?: boolean })[] = []
  const sorted = [...data].sort((a, b) => a.date.localeCompare(b.date))
  if (sorted.length > 0) {
    const startDate = new Date(sorted[0].date + 'T00:00:00Z')
    const endDate = new Date(sorted[sorted.length - 1].date + 'T00:00:00Z')
    const byDate = new Map(sorted.map((d) => [d.date, d]))
    for (let d = new Date(startDate); d <= endDate; d.setUTCDate(d.getUTCDate() + 1)) {
      const key = d.toISOString().slice(0, 10)
      padded.push(
        byDate.get(key) ?? {
          date: key,
          trade_count: 0,
          gross_pnl: 0,
          total_fees: 0,
          net_pnl: 0,
          win_count: 0,
          win_rate: 0,
          padded: true,
        },
      )
    }
  }

  const totalNetPnl = data.reduce((sum, d) => sum + d.net_pnl, 0)
  const tradingDays = data.length
  const positiveDays = data.filter((d) => d.net_pnl > 0).length

  return (
    <div className="bg-gray-900 rounded-xl p-6">
      <div className="flex items-center justify-between mb-4">
        <h2 className="text-lg font-semibold text-gray-100">Daily P&amp;L</h2>
        <div className="flex gap-4 text-xs text-gray-400">
          <span>
            Net:{' '}
            <span className={totalNetPnl >= 0 ? 'text-green-400' : 'text-red-400'}>
              {currencyFormatter(totalNetPnl)}
            </span>
          </span>
          <span>
            Days: {positiveDays}/{tradingDays} profitable
          </span>
        </div>
      </div>
      <ResponsiveContainer width="100%" height={220}>
        <BarChart data={padded} margin={{ top: 4, right: 8, left: 0, bottom: 0 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="#374151" />
          <XAxis
            dataKey="date"
            stroke="#9ca3af"
            style={{ fontSize: '11px' }}
            tickFormatter={(v: string) => v.slice(5)}
          />
          <YAxis
            stroke="#9ca3af"
            tickFormatter={currencyFormatter}
            style={{ fontSize: '11px' }}
          />
          <Tooltip
            contentStyle={{
              backgroundColor: '#111827',
              border: '1px solid #374151',
              borderRadius: '8px',
            }}
            labelStyle={{ color: '#f3f4f6' }}
            formatter={(value: number, name: string) => [
              currencyFormatter(value),
              name,
            ]}
            content={({ active, payload, label }) => {
              if (!active || !payload || payload.length === 0) return null
              const d = padded.find((p) => p.date === label)
              if (!d) return null
              return (
                <div
                  style={{
                    backgroundColor: '#111827',
                    border: '1px solid #374151',
                    borderRadius: '8px',
                    padding: '8px 12px',
                    fontSize: '12px',
                  }}
                >
                  <p style={{ color: '#f3f4f6', marginBottom: 4 }}>{label}</p>
                  <p style={{ color: d.net_pnl >= 0 ? '#10b981' : '#ef4444' }}>
                    Net PnL: {currencyFormatter(d.net_pnl)}
                  </p>
                  <p style={{ color: '#9ca3af' }}>
                    Gross: {currencyFormatter(d.gross_pnl)} | Fees: {currencyFormatter(d.total_fees)}
                  </p>
                  <p style={{ color: '#9ca3af' }}>
                    Trades: {d.trade_count} | Win rate: {(d.win_rate * 100).toFixed(0)}%
                  </p>
                </div>
              )
            }}
          />
          <Bar dataKey="net_pnl" name="Net PnL" isAnimationActive={false} radius={[2, 2, 0, 0]}>
            {padded.map((entry, index) => (
              <Cell
                key={`cell-${index}`}
                fill={entry.net_pnl >= 0 ? '#10b981' : '#ef4444'}
                fillOpacity={(entry as { padded?: boolean }).padded ? 0.2 : 0.85}
              />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </div>
  )
}
