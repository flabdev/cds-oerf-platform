"""Sending windows. Quiet hours defer a rung; they do not skip it."""

from __future__ import annotations

from datetime import datetime, time, timedelta

from pydantic import BaseModel, ConfigDict


class SendingCalendar(BaseModel):
    """Quiet hours in one timezone. An empty window means sending is always allowed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    quiet_start: time | None = None
    quiet_end: time | None = None

    def allows(self, moment: datetime) -> bool:
        if self.quiet_start is None or self.quiet_end is None:
            return True
        current = moment.timetz().replace(tzinfo=None)
        if self.quiet_start <= self.quiet_end:
            return not (self.quiet_start <= current < self.quiet_end)
        return not (current >= self.quiet_start or current < self.quiet_end)

    def next_allowed(self, moment: datetime) -> datetime:
        if self.allows(moment) or self.quiet_end is None:
            return moment
        current = moment.timetz().replace(tzinfo=None)
        opening = moment.replace(
            hour=self.quiet_end.hour,
            minute=self.quiet_end.minute,
            second=0,
            microsecond=0,
        )
        wraps = self.quiet_start is not None and self.quiet_start > self.quiet_end
        if wraps and current >= self.quiet_start:
            opening += timedelta(days=1)
        return opening
