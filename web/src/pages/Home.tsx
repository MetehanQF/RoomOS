import { useCallback, useState } from 'react'
import { api } from '../api'
import { useApi, useRoomOSEvents } from '../hooks'
import { CommandNotice, ErrorPanel, LoadingPanel, PageHeader, RealtimePill, SectionHeading, State, StatusPill } from '../components/Common'
import { Icon } from '../components/Icons'
import { applyLightEvent, describeGroupResult, groupControlEnabled, lightNameMap, serviceName } from '../ui'
import type { CommandFeedback, RoomOSEvent } from '../types'

export function HomePage() {
  const load = useCallback(async () => {
    const [health, lights, system] = await Promise.all([api.health(), api.lights(), api.system()])
    return { health, lights, system }
  }, [])
  const { data, error, loading, refresh, setData } = useApi(load)
  const [feedback, setFeedback] = useState<CommandFeedback | null>(null)
  const onEvent = useCallback((event: RoomOSEvent) => {
    if (event.type !== 'light.state_changed') return
    setData((current) => current ? { ...current, lights: applyLightEvent(current.lights, event) ?? current.lights } : current)
  }, [setData])
  const realtime = useRoomOSEvents(onEvent, refresh)
  const active = data?.lights.filter((light) => light.on).length ?? 0
  const available = data?.lights.filter((light) => light.available).length ?? 0
  const groupEnabled = groupControlEnabled(data?.health ?? null)
  const metric = (node: string, name: string) => data?.system.nodes[node]?.metrics[`${node}_${name}`]

  const runGroup = async (on: boolean) => {
    setFeedback({ tone: 'pending', title: on ? 'Turning every light on…' : 'Turning every light off…' })
    try {
      const result = await api.allPower(on)
      setFeedback(describeGroupResult(result, lightNameMap(data?.lights)))
      await refresh()
    } catch (cause) {
      setFeedback({ tone: 'error', title: 'Lighting command failed', detail: cause instanceof Error ? cause.message : 'Please try again.' })
    }
  }

  return <section className="page home-page">
    <PageHeader eyebrow="Room overview" title="Good evening" subtitle="Your space, live and ready.">
      <div className="header-status"><RealtimePill status={realtime} /><StatusPill status={data?.health.home_assistant === 'connected' ? 'online' : data?.health.home_assistant === 'disconnected' ? 'offline' : 'unknown'} label="Home Assistant" /></div>
    </PageHeader>
    {error && <ErrorPanel message={error} />}
    {loading && <LoadingPanel />}
    {data && <>
      <div className="overview-grid">
        <article className="room-hero surface">
          <div className="room-hero-copy"><p className="eyebrow">At a glance</p><div className="light-count"><strong>{active}</strong><span>of {data.lights.length}<small>lights on</small></span></div><p>{active === 0 ? 'The room is resting.' : active === data.lights.length ? 'The whole room is illuminated.' : `${active} light${active === 1 ? '' : 's'} shaping the room.`}</p></div>
          <div className={`room-orb ${active > 0 ? 'is-awake' : ''}`}><span /><span /><span /></div>
          <div className="hero-foot"><span><Icon name="wifi" />{available}/{data.lights.length} devices available</span><span><Icon name="pulse" />Realtime sync</span></div>
        </article>
        <article className="quick-lighting surface">
          <SectionHeading icon="lights" title="Quick lighting" detail="Every room light, one touch" />
          <div className="quick-actions"><button className="power-action on" disabled={!groupEnabled || feedback?.tone === 'pending'} onClick={() => void runGroup(true)}><Icon name="power" /><span>All lights on</span></button><button className="power-action off" disabled={!groupEnabled || feedback?.tone === 'pending'} onClick={() => void runGroup(false)}><Icon name="power" /><span>All lights off</span></button></div>
          {!groupEnabled && <p className="control-hint"><Icon name="system" />Controls are safely locked at the Core.</p>}
          <CommandNotice feedback={feedback} onDismiss={() => setFeedback(null)} />
        </article>
      </div>

      <SectionHeading icon="server" title="Devices" />
      <div className="device-summary-grid">{Object.keys(data.system.nodes).map((nodeId) => {
        const node = data.system.nodes[nodeId]
        const cpu = metric(nodeId, 'cpu'); const ram = metric(nodeId, 'ram'); const disk = metric(nodeId, 'disk'); const temp = metric(nodeId, 'temperature')
        return <article className="device-summary compact surface" key={nodeId}><div className="device-title"><span className="device-icon"><Icon name="server" /></span><div><p>Node</p><h3>{node.name}</h3></div></div><div className="compact-stats"><span><b>CPU</b>{cpu?.value ?? '—'}{cpu?.unit}</span><span><b>RAM</b>{ram?.value ?? '—'}{ram?.unit}</span><span><b>Temp</b>{temp?.value ?? '—'}{temp?.unit}</span><span className="optional-disk"><b>Disk</b>{disk?.value ?? '—'}{disk?.unit}</span></div><State value={node.online} /></article>
      })}</div>

      <SectionHeading icon="pulse" title="Services" />
      <div className="service-strip">{data.system.services.map((service) => <article className="service-item" key={service.id}><span className="service-icon"><Icon name={service.id === 'home_assistant' ? 'home' : 'server'} /></span><div><strong>{serviceName(service.id)}</strong><small>{service.status === 'unknown' ? 'Unknown' : service.source}</small></div><StatusPill status={service.status} /></article>)}</div>
    </>}
  </section>
}
