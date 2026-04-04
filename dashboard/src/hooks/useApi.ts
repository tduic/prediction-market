import { useState, useEffect } from 'react'

interface UseApiResult<T> {
  data: T | null
  loading: boolean
  error: string | null
}

interface UseApiParams {
  [key: string]: string | number | boolean | undefined
}

export function useApi<T>(
  url: string,
  refreshInterval: number = 30000,
  params?: UseApiParams,
): UseApiResult<T> {
  const [data, setData] = useState<T | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  // Serialize params so a new object with the same values doesn't re-trigger
  // the effect on every render. Without this, inline objects like `{ days }`
  // get a new reference each render, causing an infinite fetch loop.
  const paramsKey = JSON.stringify(params ?? null)

  useEffect(() => {
    const fetchData = async () => {
      try {
        setLoading(true)
        setError(null)

        // Build URL with query parameters
        let fetchUrl = url
        const currentParams: UseApiParams = JSON.parse(paramsKey)
        if (currentParams) {
          const queryParams = new URLSearchParams()
          Object.entries(currentParams).forEach(([key, value]) => {
            if (value !== undefined) {
              queryParams.append(key, String(value))
            }
          })
          const queryString = queryParams.toString()
          if (queryString) {
            fetchUrl = `${url}?${queryString}`
          }
        }

        const response = await fetch(fetchUrl)
        if (!response.ok) {
          throw new Error(`HTTP ${response.status}: ${response.statusText}`)
        }
        const json = await response.json()
        setData(json)
        setError(null)
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Unknown error')
        // Do NOT clear data on error — keep showing stale data so the
        // dashboard stays usable if a single refresh fails.
      } finally {
        setLoading(false)
      }
    }

    fetchData()
    const interval = setInterval(fetchData, refreshInterval)

    return () => clearInterval(interval)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [url, refreshInterval, paramsKey])

  return { data, loading, error }
}
