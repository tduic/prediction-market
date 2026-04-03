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

  useEffect(() => {
    const fetchData = async () => {
      try {
        setLoading(true)
        setError(null)

        // Build URL with query parameters
        let fetchUrl = url
        if (params) {
          const queryParams = new URLSearchParams()
          Object.entries(params).forEach(([key, value]) => {
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
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Unknown error')
        setData(null)
      } finally {
        setLoading(false)
      }
    }

    fetchData()
    const interval = setInterval(fetchData, refreshInterval)

    return () => clearInterval(interval)
  }, [url, refreshInterval, params])

  return { data, loading, error }
}
