"""Bound checkpoint writes without slowing observation or losing transitions."""

PULSE_CHECKPOINT_SECONDS = 15
FUNDING_CHECKPOINT_SECONDS = 60


def pulse_checkpoint(pulse, at):
    # Small committed watermark, not a second copy of the evidence JSON.
    return {"at": at, **{key: pulse.get(key) for key in
            ("state", "notice_version", "ended_at", "peak")}}


def pulse_write_due(previous, pulse, now):
    if previous is None or now - previous["at"] >= PULSE_CHECKPOINT_SECONDS:
        return True
    # New pulse, retrace, restart, gap/isolation/end and notification transitions
    # bypass batching. In-memory detector still sees EVERY valid quote.
    if any(previous.get(key) != pulse.get(key) for key in
           ("state", "notice_version", "ended_at")):
        return True
    old, new = previous.get("peak"), pulse.get("peak")
    if isinstance(old, (float, int)) and isinstance(new, (float, int)):
        return abs(new - old) >= .20 - 1e-9
    return False


def funding_write_due(persisted, current, events):
    if not persisted or events:
        return True
    if current["observed_at"] - persisted["observed_at"] >= FUNDING_CHECKPOINT_SECONDS:
        return True
    # Quotes/freshness live in the bounded read cache. All durable state-machine
    # changes (including a pending settlement or leaving the cap) commit at once.
    volatile = {"rate", "rate_at", "info_at", "observed_at"}
    return ({k: v for k, v in persisted.items() if k not in volatile} !=
            {k: v for k, v in current.items() if k not in volatile})
