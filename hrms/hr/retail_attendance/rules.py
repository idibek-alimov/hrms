"""Pure date/state rules shared by the manager screen and attendance commands.

All timestamps are naive datetimes in the Frappe site's time zone, like native
Employee Checkin. A business date is explicit; midnight never changes its state.
"""
from datetime import date, datetime, time, timedelta


def parse_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def parse_time(value):
    if isinstance(value, timedelta):
        if not 0 <= value.total_seconds() < 86400:
            raise ValueError("Invalid time")
        value = (datetime.min + value).time()
    if isinstance(value, time):
        return value.replace(microsecond=0)
    value = str(value)
    if value[1:2] == ":":
        value = "0" + value
    result = time.fromisoformat(value).replace(microsecond=0)
    if result.tzinfo is not None:
        raise ValueError("Use local shop time")
    return result


def span(work_date, start, end):
    start, end = parse_time(start), parse_time(end)
    if start == end:
        raise ValueError("Start and end must differ")
    first = datetime.combine(parse_date(work_date), start)
    last = datetime.combine(parse_date(work_date), end)
    if last < first:
        last += timedelta(days=1)
    return first, last


def weekdays(value):
    result = [int(x) for x in str(value).split(",")]
    if not result or len(result) != len(set(result)) or any(x not in range(7) for x in result):
        raise ValueError("Select working weekdays")
    return ",".join(str(x) for x in sorted(result))


def choose_revision(revisions, work_date):
    """Latest dated exception wins; Usual deliberately restores the regular rule."""
    work_date = parse_date(work_date)
    regular = [r for r in revisions if r["kind"] == "Regular" and parse_date(r["effective_date"]) <= work_date]
    exceptions = [r for r in revisions if r["kind"] == "Exception" and parse_date(r["effective_date"]) == work_date]
    exception = max(exceptions, key=lambda r: int(r["revision"]), default=None)
    normal = max(regular, key=lambda r: (parse_date(r["effective_date"]), int(r["revision"])), default=None)
    return (normal if exception and exception["mode"] == "Usual" else exception or normal), exception


def close_time(value, opened_at, latest_event, now):
    effective = datetime.fromisoformat(str(value))
    if effective.tzinfo is not None or effective > now or effective < opened_at:
        raise ValueError("Use an actual closing time between opening and now")
    if latest_event and effective < latest_event:
        raise ValueError("Closing time precedes a recorded arrival or departure; correct that event first")
    return effective.replace(microsecond=0)


def hours_difference(expected_end, effective_close):
    return round((effective_close - expected_end).total_seconds() / 60)
