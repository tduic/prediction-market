import { CircuitBreakerStatus } from '../types'

interface Props {
  data: CircuitBreakerStatus | null
}

export function CircuitBreakerStatusComponent({ data }: Props) {
  if (!data) {
    return (
      <div className="inline-flex items-center gap-1.5 px-2 py-1 rounded text-xs bg-gray-800 text-gray-400">
        <div className="w-1.5 h-1.5 rounded-full bg-gray-500" />
        CB loading...
      </div>
    )
  }

  if (data.tripped) {
    return (
      <div className="inline-flex items-center gap-1.5 px-2 py-1 rounded text-xs bg-red-900/40 border border-red-700 text-red-300">
        <div className="w-1.5 h-1.5 rounded-full bg-red-400 animate-pulse" />
        <span className="font-semibold">HALTED</span>
        {data.reason && (
          <span className="text-red-400 truncate max-w-xs" title={data.reason}>
            — {data.reason}
          </span>
        )}
      </div>
    )
  }

  const lossUsedPct =
    data.daily_loss_limit > 0 ? (data.daily_loss / data.daily_loss_limit) * 100 : 0
  const isWarning = lossUsedPct >= 75

  return (
    <div
      className={`inline-flex items-center gap-1.5 px-2 py-1 rounded text-xs ${
        isWarning
          ? 'bg-yellow-900/30 border border-yellow-700 text-yellow-300'
          : 'bg-green-900/20 border border-green-800 text-green-400'
      }`}
    >
      <div
        className={`w-1.5 h-1.5 rounded-full ${isWarning ? 'bg-yellow-400' : 'bg-green-400'}`}
      />
      {isWarning ? 'CB WARNING' : 'CB OK'}
      <span className="text-gray-400">
        ${data.daily_loss.toFixed(2)} / ${data.daily_loss_limit.toFixed(2)}
      </span>
    </div>
  )
}
