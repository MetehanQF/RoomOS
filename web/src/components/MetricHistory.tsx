import { useCallback, useState } from 'react'
import { api } from '../api'
import { useApi } from '../hooks'
import { ErrorPanel, LoadingPanel } from './Common'

export type MetricHistoryData = { device_id: string; metric: string; range: string; status: 'ok'|'insufficient'|'unavailable'; unit: string|null; step_seconds: number; generated_at: string; points: {ts:string;avg:number|null;min:number|null;max:number|null;samples:number;source:string|null;provenance:string|null}[] }
export type MetricRange = '1h'|'24h'|'7d'|'30d'

export function MetricHistory({deviceId, metric, label}: {deviceId:string;metric:string;label:string}) {
  const [range,setRange] = useState<MetricRange>('24h')
  const loader = useCallback(()=>api.metricHistory(deviceId,metric,range),[deviceId,metric,range])
  const {data,loading,error,refresh} = useApi(loader)
  const values = data?.points.flatMap(p=>p.avg===null?[]:[p.avg]) ?? []
  const low = Math.min(...values), high = Math.max(...values)
  const segments: string[][] = [[]]
  data?.points.forEach((p,i)=>{
    if(p.avg===null) { if(segments.at(-1)!.length) segments.push([]); return }
    segments.at(-1)!.push(`${10+i*580/Math.max(1,data.points.length-1)},${110-(p.avg-low)*90/Math.max(1,high-low)}`)
  })
  return <article className="metric-history surface">
    <header><h3>{label}</h3><label>History range <select value={range} onChange={e=>setRange(e.target.value as MetricRange)}>{(['1h','24h','7d','30d'] as const).map(r=><option key={r}>{r}</option>)}</select></label></header>
    {loading ? <LoadingPanel label="Loading history…"/> : error ? <><ErrorPanel message={error}/><button onClick={()=>void refresh()}>Retry</button></> : !values.length ? <p>No valid history collected for this window yet.</p> : <>
      <svg viewBox="0 0 600 130" role="img" aria-label={`${label}: ${low.toFixed(1)}–${high.toFixed(1)} ${data?.unit ?? ''}. Gaps indicate missing valid observations.`}>{segments.filter(s=>s.length).map((s,i)=>s.length===1 ? <circle key={i} cx={s[0].split(',')[0]} cy={s[0].split(',')[1]} r="3" fill="#bd9aff"/> : <polyline key={i} points={s.join(' ')} fill="none" stroke="#bd9aff" strokeWidth="2" />)}</svg>
      <p>Min {low.toFixed(1)} · Max {high.toFixed(1)} {data?.unit} · {values.length}/{data?.points.length} buckets</p>
      <small>UTC storage · Europe/Istanbul display. Missing observations are gaps, not zeroes. Receipt observations are not source measurement times.</small>
      <details><summary>Accessible data table</summary><div className="history-table"><table><thead><tr><th>İstanbul</th><th>Average</th><th>Range</th></tr></thead><tbody>{data?.points.map(p=><tr key={p.ts}><td>{new Intl.DateTimeFormat('tr-TR',{timeZone:'Europe/Istanbul',dateStyle:'short',timeStyle:'short'}).format(new Date(p.ts))}</td><td>{p.avg?.toFixed(1)??'Gap'}</td><td>{p.min===null?'—':`${p.min?.toFixed(1)}–${p.max?.toFixed(1)}`}</td></tr>)}</tbody></table></div></details>
    </>}
  </article>
}
