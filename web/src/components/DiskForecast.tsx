import { useCallback } from 'react'
import { api } from '../api'
import { useApi } from '../hooks'
import { forecastText, VERDICT } from '../disk-forecast'

export function DiskForecast({deviceId}: {deviceId: string}) {
  const {data, error} = useApi(useCallback(() => api.diskForecast(deviceId), [deviceId]))
  return <article className="surface monitor-notice" data-forecast={data?.status ?? (error ? 'unavailable' : 'loading')}>
    <h3>Disk forecast · {data ? VERDICT[data.status] : error ? VERDICT.unavailable : 'Reading…'}</h3>
    {data && <p>{forecastText(data)}</p>}
    {data && data.observations > 0 && <small>{data.observations} hourly observations. Linear estimate, not a health verdict; deletions and capacity changes make it unreliable.</small>}
    {error && <p>Forecast unavailable; live storage values are shown separately.</p>}
  </article>
}
