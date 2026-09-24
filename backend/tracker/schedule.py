"""Pure refresh policy shared by batch collectors and per-subject monitors."""
from dataclasses import dataclass, replace
from datetime import datetime


@dataclass(frozen=True)
class Schedule:
    interval_seconds: int
    retry_seconds: int = 300
    max_retry_seconds: int = 3600

    def __post_init__(self):
        for value in (self.interval_seconds, self.retry_seconds, self.max_retry_seconds):
            if type(value) is not int or not 1 <= value <= 604800:
                raise ValueError('refresh delays must be integers in 1..604800')
        if self.retry_seconds > self.max_retry_seconds:
            raise ValueError('refresh retry must not exceed its maximum')

    def override(self, values):
        if not isinstance(values, dict) or set(values) - {
            'interval_seconds', 'retry_seconds', 'max_retry_seconds'
        }:
            raise ValueError('unknown refresh setting')
        return replace(self, **values)

    def delay(self, failures=0):
        if failures <= 0:
            return self.interval_seconds
        return min(self.retry_seconds * 2 ** min(failures - 1, 20), self.max_retry_seconds)

    def due(self, previous, fingerprint, now):
        if previous.get('fingerprint') != fingerprint:
            return True
        stamp = previous.get('attempted_at') or previous.get('checked_at')
        failures = (max(1, previous.get('failures', 0))
                    if previous.get('status') in ('error', 'partial') else 0)
        try:
            age = (now - datetime.fromisoformat(stamp)).total_seconds()
            return age < -300 or age >= self.delay(failures)
        except (ValueError, TypeError):
            return True
