"""Acceptance-only wrapper; no production source mutation or test control endpoints."""
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC,datetime,timedelta
from app.main import app
from app.devices import CATALOG,BINDINGS,DeviceRegistry,SourceSpec,Reading
original=app.router.lifespan_context
@asynccontextmanager
async def fixture_lifespan(app):
    async with original(app):
        registry=app.state.devices
        # Unconfigured physical device adapters remain absent in the shipping source.
        for name in ('workstation','phone'):
            registry.catalog[name]=replace(registry.catalog[name],sources=(SourceSpec('home_assistant',True),))
            registry.bindings[('home_assistant','fixture-'+name)]=name
        now=datetime.now(UTC)
        registry.ingest('home_assistant','fixture-workstation',{'cpu_usage':Reading(30,measured_at=now-timedelta(minutes=20))},connection='offline')
        registry.ingest('home_assistant','fixture-phone',{'battery_percent':Reading(80,measured_at=now-timedelta(minutes=20)),'wifi_connected':Reading(True)},connection='unknown')
        import asyncio
        async def pulse():
            # Synthetic devices:read traffic so revocation is tested against live delivery.
            n=0
            while True:
                await asyncio.sleep(2); n+=1
                registry.ingest('home_assistant','fixture-phone',{'wifi_connected':Reading(n%2==0)},connection='online')
                await registry.publish_changes()
        task=asyncio.create_task(pulse())
        try:
            yield
        finally:
            task.cancel()
app.router.lifespan_context=fixture_lifespan
