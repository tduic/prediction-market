import { useState } from 'react'
import { Trade } from '../types'
import { formatStrategy } from '../utils/strategies'

interface TradeLogProps {
  data: Trade[]
  strategies: string[]
}

interface ExpandedTradeId {
  [key: string]: boolean
}

const currencyFormatter = new Intl.NumberFormat('en-US', {
  style: 'currency',
  currency: 'USD',
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
})

const timeFormatter = (dateString: string) => {
  const date = new Date(dateString)
  const month = String(date.getMonth() + 1).padStart(2, '0')
  const day = String(date.getDate()).padStart(2, '0')
  const hours = String(date.getHours()).padStart(2, '0')
  const minutes = String(date.getMinutes()).padStart(2, '0')
  return `${month}/${day} ${hours}:${minutes}`
}

export function TradeLog({ data, strategies }: TradeLogProps) {
  const [selectedStrategy, setSelectedStrategy] = useState<string>('all')
  const [expandedRows, setExpandedRows] = useState<ExpandedTradeId>({})

  // Filter trades
  const filteredTrades = selectedStrategy === 'all'
    ? data
    : data.filter((trade) => trade.strategy === selectedStrategy)

  // Sort by time descending
  const sortedTrades = [...filteredTrades].sort(
    (a, b) => new Date(b.resolved_at).getTime() - new Date(a.resolved_at).getTime(),
  )

  const toggleRowExpand = (tradeId: string) => {
    setExpandedRows((prev) => ({
      ...prev,
      [tradeId]: !prev[tradeId],
    }))
  }

  return (
    <div className="bg-gray-900 rounded-xl p-6">
      <div className="flex justify-between items-center mb-6">
        <h2 className="text-lg font-semibold text-gray-100">Recent Trades</h2>
        <select
          value={selectedStrategy}
          onChange={(e) => setSelectedStrategy(e.target.value)}
          className="bg-gray-800 text-gray-100 border border-gray-700 rounded-lg px-4 py-2 text-sm focus:outline-none focus:border-blue-500"
        >
          <option value="all">All Strategies</option>
          {strategies.map((strategy) => (
            <option key={strategy} value={strategy}>
              {formatStrategy(strategy)}
            </option>
          ))}
        </select>
      </div>

      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-gray-800">
              <th className="text-center px-4 py-3 text-gray-400 font-medium w-8"></th>
              <th className="text-left px-4 py-3 text-gray-400 font-medium">Time</th>
              <th className="text-left px-4 py-3 text-gray-400 font-medium">Strategy</th>
              <th className="text-left px-4 py-3 text-gray-400 font-medium">Markets</th>
              <th className="text-right px-4 py-3 text-gray-400 font-medium">Edge</th>
              <th className="text-right px-4 py-3 text-gray-400 font-medium">PnL</th>
              <th className="text-right px-4 py-3 text-gray-400 font-medium">Fees</th>
              <th className="text-right px-4 py-3 text-gray-400 font-medium">Edge Capture %</th>
              <th className="text-right px-4 py-3 text-gray-400 font-medium">Fill Time (ms)</th>
            </tr>
          </thead>
          <tbody>
            {sortedTrades.length === 0 ? (
              <tr>
                <td colSpan={9} className="text-center py-8 text-gray-400">
                  No trades found
                </td>
              </tr>
            ) : (
              sortedTrades.map((trade, idx) => {
                const isPositivePnl = trade.actual_pnl >= 0
                const alternateRow = idx % 2 === 1
                const isExpanded = expandedRows[trade.id]

                return (
                  <tr
                    key={trade.id}
                    className={`border-b border-gray-800 ${
                      alternateRow ? 'bg-gray-800/30' : ''
                    } hover:bg-gray-800/50 transition-colors`}
                  >
                    <td className="text-center px-4 py-3">
                      <button
                        onClick={() => toggleRowExpand(trade.id)}
                        className="text-gray-400 hover:text-gray-200 transition-colors text-lg"
                        title="Click to expand market IDs"
                      >
                        {isExpanded ? '▼' : '▶'}
                      </button>
                    </td>
                    <td className="px-4 py-3 text-gray-100">
                      {timeFormatter(trade.resolved_at)}
                    </td>
                    <td className="px-4 py-3 text-gray-100">
                      {formatStrategy(trade.strategy)}
                    </td>
                    <td className="px-4 py-3 text-gray-100 font-mono text-xs">
                      {isExpanded ? (
                        <div className="space-y-1">
                          <div>
                            <span className="text-gray-400">A: </span>
                            {trade.market_id_a}
                          </div>
                          <div>
                            <span className="text-gray-400">B: </span>
                            {trade.market_id_b}
                          </div>
                        </div>
                      ) : (
                        <span className="text-gray-500">
                          {trade.market_id_a.substring(0, 8)}... / {trade.market_id_b.substring(0, 8)}...
                        </span>
                      )}
                    </td>
                    <td className="px-4 py-3 text-right text-gray-100">
                      {(trade.predicted_edge * 100).toFixed(2)}%
                    </td>
                    <td
                      className={`px-4 py-3 text-right font-semibold ${
                        isPositivePnl ? 'text-green-400' : 'text-red-400'
                      }`}
                    >
                      {isPositivePnl ? '+' : ''}
                      {currencyFormatter.format(trade.actual_pnl)}
                    </td>
                    <td className="px-4 py-3 text-right text-gray-100">
                      {currencyFormatter.format(trade.fees_total)}
                    </td>
                    <td className="px-4 py-3 text-right text-gray-100">
                      {trade.edge_captured_pct.toFixed(1)}%
                    </td>
                    <td className="px-4 py-3 text-right text-gray-100">
                      {trade.signal_to_fill_ms}
                    </td>
                  </tr>
                )
              })
            )}
          </tbody>
        </table>
      </div>
    </div>
  )
}
