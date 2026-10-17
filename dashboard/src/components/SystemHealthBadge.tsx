import { SystemHealth } from '../types'

interface Props {
  data: SystemHealth | null
}

export function SystemHealthBadge({ data }: Props) {
  if (!data) {
    return (
      <div className="inline-flex items-center gap-1.5 px-2 py-1 rounded text-xs bg-gray-800 text-gray-400">
        <div className="w-1.5 h-1.5 rounded-full bg-gray-500" />
        Health...
      </div>
    )
  }

  // A persistent execution halt blocks every new order until an operator
  // runs scripts/clear_halt.py — always show it as critical, by name.
  const halt =
    data.execution_halt && !('error' in data.execution_halt)
      ? data.execution_halt
      : null

  const statusConfig = {
    ok: {
      bg: 'bg-green-900/20 border border-green-800',
      text: 'text-green-400',
      dot: 'bg-green-400',
      label: 'SYS OK',
    },
    warn: {
      bg: 'bg-yellow-900/30 border border-yellow-700',
      text: 'text-yellow-300',
      dot: 'bg-yellow-400',
      label: 'SYS WARN',
    },
    critical: {
      bg: 'bg-red-900/40 border border-red-700',
      text: 'text-red-300',
      dot: 'bg-red-400 animate-pulse',
      label: 'SYS CRITICAL',
    },
  }[halt ? 'critical' : data.status]
  const label = halt ? 'TRADING HALTED' : statusConfig.label

  const issueCount = data.issues?.length ?? 0
  const haltText = halt
    ? `Execution halted${halt.halted_at ? ` since ${halt.halted_at}` : ''}: ${halt.reason ?? 'unknown reason'} (clear with scripts/clear_halt.py)`
    : null
  const issuesText =
    issueCount > 0 ? data.issues.join(' | ') : 'All systems nominal'
  const tooltipText = haltText ? `${haltText} | ${issuesText}` : issuesText

  return (
    <div
      className={`inline-flex items-center gap-1.5 px-2 py-1 rounded text-xs ${statusConfig.bg} ${statusConfig.text}`}
      title={tooltipText}
    >
      <div className={`w-1.5 h-1.5 rounded-full ${statusConfig.dot}`} />
      {label}
      {issueCount > 0 && (
        <span className="text-gray-400">({issueCount})</span>
      )}
    </div>
  )
}
