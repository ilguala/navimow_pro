"""Mower event detection, kept free of Home Assistant so it can be tested alone.

The event entity (event.py) fires on the moments an automation usually
wants: a mow starting, the mower getting back to its dock, a job finishing, and
a fault. Reacting to these saves an automation from reading state transitions
itself -- and from the traps in them, below.

Worked out from a live i220's history, where the obvious signals mislead:

- "Docked (finished)" (0102) and 0103 are reported after EVERY return, at 52 %
  as much as at 100 %. A job is only finished when the mower reports 100 %.
- A mower in its dock can flit between Charging and unmapped 02xx "Working"
  codes for minutes without going anywhere. Only the codes that mean it is out
  on the lawn (mowing, returning, paused) count as a trip, so a return is
  reported once per trip and a start once per departure.
- A fault can be raised while it sits in the dock; that is a fault, not a trip.

The first snapshot after setup only primes the detector: whatever happened
while Home Assistant was not watching is not replayed as if it just happened.
"""
from __future__ import annotations

from typing import Any

from .const import STATE_MOWING, STATE_PAUSED, STATE_PAUSED_RETURNING, STATE_RETURNING

EVENT_MOWING_STARTED = "mowing_started"
EVENT_RETURNED_TO_DOCK = "returned_to_dock"
EVENT_MOWING_FINISHED = "mowing_finished"
EVENT_ERROR = "error"
EVENT_TYPES = [EVENT_MOWING_STARTED, EVENT_RETURNED_TO_DOCK, EVENT_MOWING_FINISHED, EVENT_ERROR]

# States that mean the mower is out on the lawn. Unmapped 02xx codes are left
# out on purpose: they were seen flickering with Charging while it stayed put.
_OUT_STATES = frozenset({STATE_MOWING, STATE_RETURNING, STATE_PAUSED, STATE_PAUSED_RETURNING})


class MowerEventDetector:
    """Turns successive snapshots into events. Pure, so it can be replayed."""

    def __init__(self) -> None:
        self._primed = False
        self._error = False
        # Seen docked since the last start, so the next mowing state is a start.
        self._at_dock = False
        # Out on the lawn since the last return, so the next dock is a return.
        self._on_trip = False
        # Back from a trip and not yet at 100 %: the job may still be reported
        # finished while it sits in the dock.
        self._finish_pending = False

    def update(self, data: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        """Feed one snapshot; return the (event_type, attributes) it causes."""
        if not data or not data.get("state_code"):
            return []
        code = str(data["state_code"])
        docked = bool(data.get("docked"))
        error = bool(data.get("error"))
        progress = data.get("mowing_progress")
        finished = isinstance(progress, int) and progress >= 100
        attrs = {
            "state": data.get("state"),
            "state_code": code,
            "mowing_progress": progress,
            "current_zone": data.get("current_zone"),
            "battery": data.get("battery"),
        }

        if not self._primed:
            self._primed = True
            self._error = error
            self._at_dock = docked
            self._on_trip = code in _OUT_STATES
            return []

        events: list[tuple[str, dict[str, Any]]] = []
        if error and not self._error:
            events.append(
                (
                    EVENT_ERROR,
                    {
                        **attrs,
                        "error_text": data.get("error_text"),
                        "error_codes": data.get("error_codes") or [],
                    },
                )
            )

        if docked:
            if self._on_trip:
                self._on_trip = False
                self._finish_pending = True
                events.append((EVENT_RETURNED_TO_DOCK, attrs))
            if self._finish_pending and finished:
                self._finish_pending = False
                events.append((EVENT_MOWING_FINISHED, attrs))
            self._at_dock = True
        elif code in _OUT_STATES:
            if code == STATE_MOWING and self._at_dock:
                self._at_dock = False
                self._finish_pending = False
                events.append((EVENT_MOWING_STARTED, attrs))
            self._on_trip = True

        self._error = error
        return events

