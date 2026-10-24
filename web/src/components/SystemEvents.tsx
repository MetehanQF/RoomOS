import { useCallback, useState } from 'react'
import { api } from '../api'
import { useApi } from '../hooks'
import { useAuthStatus } from '../auth-context'
import { ActivityRow } from '../pages/Activity'
import { ErrorPanel, LoadingPanel } from './Common'

export function SystemEvents() {
  const [offset,setOffset]=useState(0)
  const [filter,setFilter]=useState<'all'|'alerts'|'scenes'>('alerts')
  const allowed=useAuthStatus()?.device?.scopes.includes('activity:read')??false
  const loader=useCallback(()=>allowed?api.activity(filter,20,offset):Promise.resolve(null),[allowed,filter,offset])
  const {data,error,loading}=useApi(loader)
  if(!allowed) return <p>Activity read access is required.</p>
  return <section aria-label="System events"><label>Events <select value={filter} onChange={e=>{setFilter(e.target.value as typeof filter);setOffset(0)}}><option value="alerts">Alerts</option><option value="scenes">Scenes and routines</option><option value="all">All</option></select></label>
    {error&&<ErrorPanel message={error}/>} {loading&&<LoadingPanel label="Reading events…"/>}
    {data&&!loading&&<><div className="activity-timeline">{data.activity.map(item=><ActivityRow key={item.id} item={item}/>)}</div>{!data.activity.length&&<p>No recorded events for this filter.</p>}<div className="monitor-pagination"><button disabled={offset===0} onClick={()=>setOffset(Math.max(0,offset-20))}>Previous</button><span>{offset+1}–{offset+data.activity.length} / {data.total}</span><button disabled={offset+20>=data.total} onClick={()=>setOffset(offset+20)}>Next</button></div></>}
  </section>
}
