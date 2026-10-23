"""Settled funding evidence, not forecast income or a guaranteed rate floor."""
from opportunity_pool import baseline, number

WINDOWS = {"24h": 86400, "3d": 3*86400, "7d": 7*86400}
CADENCES = (1, 2, 4, 8)


def cadence(seconds):
    return next((h for h in CADENCES if abs(seconds-h*3600) <= 2), None)


def funding_window(events, start, end, current_interval=None):
    # Never use a settlement AFTER the measurement end to classify an outcome.
    clean = sorted({float(t): float(r) for t, r in events
                    if number(t) and number(r) and start-86400 <= t <= end}.items())
    gaps = [None] + [cadence(b[0]-a[0]) for a, b in zip(clean, clean[1:])]
    result = dict(count=0, classified=0, above=0, baseline_count=0, below_positive=0,
                  positive=0, negative=0, zero=0, unknown_count=0, total=None,
                  above_ratio=None, recent_positive_streak=0, longest_positive_streak=0,
                  first_at=None, last_at=None, complete=False, covered_hours=0,
                  start=start, end=end, cadence="相邻结算间隔推断；缺口不当低保")
    recent = []
    streak = 0
    for index, (at, rate) in enumerate(clean):
        if at <= start:
            continue
        hours = gaps[index]
        previous = gaps[index-1] if index >= 2 else None
        following = gaps[index+1] if index+1 < len(gaps) else None
        # A doubled missing period must not silently become an 8H/4H period.
        # Shortening is allowed; lengthening needs corroboration on both sides.
        known = hours is not None and (
            previous == hours or (previous is None and (following == hours or current_interval == hours))
            or (previous is not None and hours < previous))
        if hours and previous and hours > previous:
            known = False  # First longer interval is ambiguous, even if later cadence agrees.
        result["count"] += 1
        result["positive"] += rate > 0
        result["negative"] += rate < 0
        result["zero"] += rate == 0
        if known:
            result["classified"] += 1
            base = baseline(hours)
            result["above"] += rate > base+1e-6
            result["baseline_count"] += abs(rate-base) <= 1e-6
            result["below_positive"] += 0 < rate < base-1e-6
            result["covered_hours"] += (at-max(start, clean[index-1][0]))/3600
        else:
            result["unknown_count"] += 1
        streak = streak+1 if known and rate > 0 else 0
        result["longest_positive_streak"] = max(result["longest_positive_streak"], streak)
        recent.append((at, rate, hours if known else None))
    if recent:
        result.update(total=sum(r for _, r, _ in recent), first_at=recent[0][0],
                      last_at=recent[-1][0], recent_positive_streak=streak)
        if result["classified"]:
            result["above_ratio"] = result["above"]/result["classified"]
        tail_hours = recent[-1][2]
        # Current cadence can reveal that the most recent settlement is overdue.
        if current_interval in CADENCES and tail_hours:
            tail_hours = min(tail_hours, current_interval)
        result["complete"] = bool(clean[0][0] <= start and not result["unknown_count"]
                                  and tail_hours and end-recent[-1][0] <= tail_hours*3600+120)
    return result


def funding_quality(events, now, interval=None):
    return dict(as_of=now, windows={key: funding_window(events, now-seconds, now, interval)
                                 for key, seconds in WINDOWS.items()})
