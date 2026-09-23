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
  }[data.status]

  const issueCount = data.issues?.length ?? 0
  const tooltipText =
    issueCount > 0 ? data.issues.join(' | ') : 'All systems nominal'

  return (
    <div
      className={`inline-flex items-center gap-1.5 px-2 py-1 rounded text-xs ${statusConfig.bg} ${statusConfig.text}`}
      title={tooltipText}
    >
      <div className={`w-1.5 h-1.5 rounded-full ${statusConfig.dot}`} />
      {statusConfig.label}
      {issueCount > 0 && (
        <span className="text-gray-400">({issueCount})</span>
      )}
    </div>
  )
}
