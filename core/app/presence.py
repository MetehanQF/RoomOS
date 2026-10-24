"""Reporting evidence only. HA does not expose the original GPS observation time."""
import time
from datetime import UTC, datetime
from pydantic import BaseModel
from typing import Literal

from .catalog import PRESENCE_TRACKER_ENTITY_ID

TRACKER = PRESENCE_TRACKER_ENTITY_ID  # Server-only observation allowlist; never a trigger.


class PresenceEvidence(BaseModel):
    presence_freshness: Literal['fresh', 'stale', 'unknown'] = 'unknown'
    reporting: Literal['fresh', 'stale', 'unknown'] = 'unknown'
    basis: Literal['live_tracker_event'] | None = None
    observed_at: datetime | None = None
    threshold_seconds: int
    gps_observation_available: bool = False


class PresenceObserver:
    def __init__(self, threshold=21600, *, clock=None, monotonic=None):
        self.threshold = threshold
        self.clock = clock or (lambda: datetime.now(UTC))
        self.monotonic = monotonic or time.monotonic
        self.reset()

    def reset(self):
        self.connected = False
        self.source_known = False
        self.epoch = self.clock()
        self.last_stamp = None
        self.observed_at = None
        self.observed_mono = None

    def bootstrap(self, person):
        self.reset()
        self.connected = True
        self.person(person)

    def person(self, state):
        state = state if isinstance(state, dict) else {}
        attrs = state.get('attributes') or {}
        attrs = attrs if isinstance(attrs, dict) else {}
        known = attrs.get('source') == TRACKER and (state or {}).get('state') not in {None, 'unknown', 'unavailable'}
        if not known or not self.source_known:
            self.observed_at = self.observed_mono = None
        self.source_known = known

    def observe(self, state, old_state):
        # Initial/restored entities, missing metadata and cached/pre-epoch events prove nothing.
        state = state if isinstance(state, dict) else {}
        attrs = state.get('attributes') or {}
        attrs = attrs if isinstance(attrs, dict) else {}
        if not self.connected or not self.source_known:
            return
        try:
            stamp = datetime.fromisoformat(state['last_updated'].replace('Z', '+00:00'))
            now = self.clock()
            valid = (old_state is not None and stamp.tzinfo is not None
                     and attrs.get('source_type') == 'gps' and not attrs.get('restored')
                     and state.get('state') not in {None, 'unknown', 'unavailable'}
                     and self.epoch < stamp <= now and (now - stamp).total_seconds() <= 60)
            if not valid:
                self.observed_at = self.observed_mono = None
                return
            if self.last_stamp is not None and stamp <= self.last_stamp:
                return  # replay / out of order cannot refresh reporting evidence
            self.last_stamp = stamp
            self.observed_at = now
            self.observed_mono = self.monotonic()
        except (KeyError, TypeError, ValueError):
            self.observed_at = self.observed_mono = None

    def snapshot(self):
        result = PresenceEvidence(threshold_seconds=self.threshold)
        if not self.connected or not self.source_known or self.observed_at is None:
            return result
        elapsed = self.monotonic() - self.observed_mono
        wall = (self.clock() - self.observed_at).total_seconds()
        if elapsed < 0 or abs(wall - elapsed) > 60:
            return result
        # Deliberately never claims GPS freshness, even when recent HA reporting is observed.
        return result.model_copy(update={'reporting': 'fresh' if elapsed < self.threshold else 'stale',
                                         'basis': 'live_tracker_event', 'observed_at': self.observed_at})
