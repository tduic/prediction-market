import { OverviewData } from '../types'

interface OverviewCardsProps {
  data: OverviewData | null
}

const currencyFormatter = new Intl.NumberFormat('en-US', {
  style: 'currency',
  currency: 'USD',
  minimumFractionDigits: 0,
  maximumFractionDigits: 0,
})

const percentFormatter = new Intl.NumberFormat('en-US', {
  style: 'percent',
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
})

export function OverviewCards({ data }: OverviewCardsProps) {
  if (!data) {
    return (
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-6 gap-4">
        {[1, 2, 3, 4, 5, 6].map((i) => (
          <div key={i} className="bg-gray-900 rounded-xl p-6 h-24 animate-pulse" />
        ))}
      </div>
    )
  }

  const realizedPnlIsPositive = data.realized_pnl_total >= 0
  const netReturnIsPositive = data.net_return_pct >= 0

  return (
    <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-6 gap-4">
      {/* Total Capital */}
      <div className="bg-gray-900 rounded-xl p-6">
        <p className="text-gray-400 text-sm font-medium mb-2">Total Capital</p>
        <p className="text-2xl font-semibold text-gray-100">
          {currencyFormatter.format(data.total_capital)}
        </p>
      </div>

      {/* Realized PnL */}
      <div className="bg-gray-900 rounded-xl p-6">
        <p className="text-gray-400 text-sm font-medium mb-2">Realized PnL</p>
        <p
          className={`text-2xl font-semibold ${
            realizedPnlIsPositive ? 'text-green-400' : 'text-red-400'
          }`}
        >
          {realizedPnlIsPositive ? '+' : ''}
          {currencyFormatter.format(data.realized_pnl_total)}
        </p>
      </div>

      {/* Open Positions */}
      <div className="bg-gray-900 rounded-xl p-6">
        <p className="text-gray-400 text-sm font-medium mb-2">Open Positions</p>
        <p className="text-2xl font-semibold text-gray-100">
          {data.open_positions}
        </p>
      </div>

      {/* Net Return % */}
      <div className="bg-gray-900 rounded-xl p-6">
        <p className="text-gray-400 text-sm font-medium mb-2">Net Return %</p>
        <p
          className={`text-2xl font-semibold ${
            netReturnIsPositive ? 'text-green-400' : 'text-red-400'
          }`}
        >
          {netReturnIsPositive ? '+' : ''}
          {percentFormatter.format(data.net_return_pct / 100)}
        </p>
      </div>

      {/* Total Fees */}
      <div className="bg-gray-900 rounded-xl p-6">
        <p className="text-gray-400 text-sm font-medium mb-2">Total Fees</p>
        <p className="text-2xl font-semibold text-gray-100">
          {currencyFormatter.format(data.total_fees || data.fees_total || 0)}
        </p>
      </div>

      {/* Total Trades */}
      <div className="bg-gray-900 rounded-xl p-6">
        <p className="text-gray-400 text-sm font-medium mb-2">Total Trades</p>
        <p className="text-2xl font-semibold text-gray-100">
          {data.total_trades || 0}
        </p>
      </div>
    </div>
  )
}
