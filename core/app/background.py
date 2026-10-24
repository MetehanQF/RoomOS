"""Supervision of Core's long-running background tasks.

Every task is started through one registry so the operational Health view can see all
of them, and a single done-callback logs any task that ends while RoomOS is still
running. The registry observes only: it never restarts a task, so a recurring bug cannot
turn into a fast restart loop. Tasks that must survive errors (the Home Assistant bridge)
recover inside their own loop with backoff.

Logs carry the task name and exception class only. Exception text can contain URLs,
addresses or credentials and is never written.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any, Literal

logger = logging.getLogger(__name__)

TaskState = Literal["running", "stopped", "failed", "disabled"]

TASK_NAMES: tuple[str, ...] = (
    "scheduler",
    "ha_bridge",
    "notification_dispatcher",
    "devices",
    "system_monitor",
    "camera",
    "dns_health",
    "routines.diagnostics",
    "smart_automations",
)

TASK_LABELS: dict[str, str] = {
    "scheduler": "Routine scheduler",
    "ha_bridge": "Home Assistant bridge",
    "notification_dispatcher": "Notification dispatcher",
    "devices": "Device registry",
    "system_monitor": "System monitor",
    "camera": "Camera monitor",
    "dns_health": "DNS health monitor",
    "routines.diagnostics": "Routine diagnostics",
    "smart_automations": "Smart automation engine",
}


class BackgroundTasks:
    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task | None] = {}
        # Name -> exception class name, or "returned" for an unexpected clean exit.
        self.failures: dict[str, str] = {}
        self._shutting_down = False
        self.listeners: list[Callable[[str, TaskState], Awaitable[None]]] = []

    def start(self, name: str, coro: Coroutine[Any, Any, None]) -> asyncio.Task:
        if name not in TASK_LABELS:
            coro.close()
            raise ValueError(f"unknown background task {name}")
        task = asyncio.create_task(coro, name=f"roomos:{name}")
        self._tasks[name] = task
        task.add_done_callback(lambda finished: self._finished(name, finished))
        return task

    def disabled(self, name: str) -> None:
        """Record a task that is intentionally not started by configuration."""
        self._tasks[name] = None

    def begin_shutdown(self) -> None:
        self._shutting_down = True

    def get(self, name: str) -> asyncio.Task | None:
        return self._tasks.get(name)

    def __getitem__(self, name: str) -> asyncio.Task | None:
        return self._tasks[name]

    def alive(self, name: str) -> bool:
        task = self._tasks.get(name)
        return task is not None and not task.done()

    def snapshot(self) -> tuple[tuple[str, TaskState], ...]:
        states: list[tuple[str, TaskState]] = []
        for name in TASK_NAMES:
            if name not in self._tasks:
                continue
            task = self._tasks[name]
            if task is None:
                states.append((name, "disabled"))
            elif not task.done():
                states.append((name, "running"))
            elif name in self.failures:
                states.append((name, "failed"))
            else:
                states.append((name, "stopped"))
        return tuple(states)

    async def wait(self) -> None:
        await asyncio.gather(*(task for task in self._tasks.values() if task is not None), return_exceptions=True)

    def _finished(self, name: str, task: asyncio.Task) -> None:
        if task.cancelled():
            # Cancellation is how lifespan shutdown stops tasks; never a crash.
            return
        error = task.exception()
        if self._shutting_down and error is None:
            return
        reason = type(error).__name__ if error is not None else "returned"
        self.failures[name] = reason
        logger.error("Background task stopped unexpectedly task=%s error=%s", name, reason)
        state: TaskState = "failed"
        if self.listeners:
            asyncio.create_task(
                self._notify_listeners(name, state),
                name=f"roomos:alert:{name}",
            )

    async def _notify_listeners(self, name: str, state: TaskState) -> None:
        for listener in self.listeners:
            try:
                await listener(name, state)
            except Exception as exc:
                logger.warning(
                    "Background task observer failed task=%s error=%s",
                    name,
                    type(exc).__name__,
                )
