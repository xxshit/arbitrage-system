"""Request-local scalar summaries; never retain a second list of history rows."""
import math


class AlertSummary:
    def __init__(self, *, count_alerts=True):
        self.count = 0
        self.latest = self.first = self.best = None
        self.best_priority = None
        self.max_spread = self.max_basis = None
        self.reminders = 0
        self.legacy_rounds = set() if count_alerts else None
        # Observations precede pulse bounds in the original range calculation.
        # Keep the two sources separate to preserve ties, including signed zero.
        self.observed = [[None, None], [None, None]]
        self.pulse_bounds = [[None, None], [None, None]]

    @staticmethod
    def _include(bounds, value):
        if value is None or not math.isfinite(value):
            return
        if bounds[0] is None or value < bounds[0]:
            bounds[0] = value
        if bounds[1] is None or value > bounds[1]:
            bounds[1] = value

    def add(self, item, pulse, round_id, now, priority, basis, has_basis):
        if self.latest is None:
            self.latest = item
        self.first = item
        self.count += 1
        if self.best is None or priority > self.best_priority:
            self.best, self.best_priority = item, priority
        if item.open_spread is not None and (self.max_spread is None or
                abs(item.open_spread or 0) > abs(self.max_spread.open_spread or 0)):
            self.max_spread = item
        if item.basis is not None and (self.max_basis is None or
                abs(item.basis or 0) > abs(self.max_basis.basis or 0)):
            self.max_basis = item
        if self.legacy_rounds is not None:
            if pulse is not None and pulse.has_reminder:
                self.reminders += pulse.count(now)
            else:
                self.legacy_rounds.add(round_id)
        self._include(self.observed[0], item.open_spread)
        self._include(self.observed[1], basis)
        if pulse is not None:
            for index, value in enumerate(pulse.bounds):
                if index >= 2 and not has_basis:
                    continue
                if isinstance(value, (int, float)):
                    self._include(self.pulse_bounds[index // 2], value)

    @property
    def alert_count(self):
        return self.reminders + len(self.legacy_rounds or ())

    def value_ranges(self):
        result = {}
        for index, name in enumerate(("open_spread", "basis")):
            bounds = list(self.observed[index])
            for value in self.pulse_bounds[index]:
                self._include(bounds, value)
            result[name] = {"max": bounds[1], "min": bounds[0]}
        return result
