"""Расчёт сквозной статистики. Общий для руководителя и технического раздела."""
from datetime import date, datetime, timedelta

PERIODS = [
    ("today", "Сегодня"),
    ("yesterday", "Вчера"),
    ("day_before", "Позавчера"),
    ("week", "7 дней"),
    ("month", "Месяц"),
    ("all", "Всё время"),
]
PERIOD_CAPTIONS = {
    "today": "сегодня",
    "yesterday": "вчера",
    "day_before": "позавчера",
    "week": "за 7 дней",
    "month": "в этом месяце",
    "all": "за всё время",
}
WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


def fmt_hours(hours):
    """Часы → «5.3 ч» или «2.1 дн»; нет данных → «—»."""
    if hours is None:
        return "—"
    if hours < 48:
        return f"{hours:.1f} ч"
    return f"{hours / 24:.1f} дн"


def _parse_day(text):
    try:
        return datetime.strptime(text or "", "%Y-%m-%d").date()
    except ValueError:
        return None


def resolve_period(p, d, today):
    """Вернуть (ключ, подпись, начало, конец). Начало/конец — date или None."""
    if p == "day":
        day = _parse_day(d)
        if day is not None and day <= today:
            return "day", day.strftime("%d.%m.%Y"), day, day
        p = "today"
    if p not in PERIOD_CAPTIONS:
        p = "today"
    if p == "today":
        return p, PERIOD_CAPTIONS[p], today, today
    if p == "yesterday":
        y = today - timedelta(days=1)
        return p, PERIOD_CAPTIONS[p], y, y
    if p == "day_before":
        y = today - timedelta(days=2)
        return p, PERIOD_CAPTIONS[p], y, y
    if p == "week":
        return p, PERIOD_CAPTIONS[p], today - timedelta(days=6), today
    if p == "month":
        return p, PERIOD_CAPTIONS[p], today.replace(day=1), today
    return "all", PERIOD_CAPTIONS["all"], None, None


def _in_range(iso, start, end):
    if not iso:
        return False
    if start is None:
        return True
    return start.isoformat() <= iso[:10] <= end.isoformat()


def _hours_between(a, b):
    try:
        da = datetime.fromisoformat(a)
        db = datetime.fromisoformat(b)
    except (TypeError, ValueError):
        return None
    return max((db - da).total_seconds(), 0) / 3600


def _avg(values):
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else None


def compute(db, today, p, d, brigade_ids, office_ids, payments, request_types, equipment):
    key, caption, start, end = resolve_period(p, d, today)
    reqs = [dict(r) for r in db.execute("SELECT * FROM requests")]
    events = [dict(e) for e in db.execute("SELECT * FROM events")]

    def price(r):
        return r["report_price"] or 0

    created = [r for r in reqs if _in_range(r["created_at"], start, end)]
    done = [r for r in reqs if r["status"] == "done" and _in_range(r["done_at"], start, end)]
    cancelled = [r for r in reqs if r["status"] == "cancelled" and _in_range(r["cancelled_at"], start, end)]
    ev_in = [e for e in events if _in_range(e["at"], start, end)]
    taken_events = [e for e in ev_in if e["action"] == "taken"]

    # ожидание до первого «взято»
    first_taken = {}
    for e in sorted(events, key=lambda x: (x["at"], x["id"])):
        if e["action"] == "taken" and e["request_id"] not in first_taken:
            first_taken[e["request_id"]] = e["at"]
    by_id = {r["id"]: r for r in reqs}
    waits = []
    for rid, at in first_taken.items():
        if _in_range(at, start, end) and rid in by_id:
            waits.append(_hours_between(by_id[rid]["created_at"], at))

    tiles = {
        "created": len(created),
        "taken": len(taken_events),
        "done": len(done),
        "cancelled": len(cancelled),
        "sum": sum(price(r) for r in done),
        "wait": fmt_hours(_avg(waits)),
        "now_new": sum(1 for r in reqs if r["status"] == "new"),
        "now_in_work": sum(1 for r in reqs if r["status"] == "in_work"),
    }

    brigades = []
    for uid in brigade_ids:
        my_done = [r for r in done if r["taken_by"] == uid]
        spans = [_hours_between(r["taken_at"], r["done_at"]) for r in my_done]
        brigades.append({
            "id": uid,
            "taken": sum(1 for e in taken_events if e["user_id"] == uid),
            "done": len(my_done),
            "sum": sum(price(r) for r in my_done),
            "span": fmt_hours(_avg(spans)),
            "returned": sum(1 for e in ev_in if e["action"] == "returned" and e["user_id"] == uid),
            "in_work": sum(1 for r in reqs if r["status"] == "in_work" and r["taken_by"] == uid),
        })

    office = []
    for uid in office_ids:
        office.append({
            "id": uid,
            "created": sum(1 for e in ev_in if e["action"] == "created" and e["user_id"] == uid),
            "edited": sum(1 for e in ev_in if e["action"] == "edited" and e["user_id"] == uid),
            "cancelled": sum(1 for e in ev_in if e["action"] == "cancelled" and e["user_id"] == uid),
        })

    pay_sums = {}
    for r in done:
        label = payments.get(r["report_payment"] or "", "не указана")
        pay_sums[label] = pay_sums.get(label, 0) + price(r)
    pay_rows = sorted(pay_sums.items(), key=lambda kv: -kv[1])

    # тип заявки · оборудование: каждая заявка один раз
    combos = {}

    def combo_label(r):
        t = request_types.get(r["request_type"] or "")
        e = equipment.get(r["equipment"] or "")
        if not t and not e:
            return "не указано (старые заявки)"
        return f"{t or 'тип не указан'} · {e or 'оборудование не указано'}"

    for r in created:
        c = combos.setdefault(combo_label(r), {"created": 0, "done": 0, "sum": 0})
        c["created"] += 1
    for r in done:
        c = combos.setdefault(combo_label(r), {"created": 0, "done": 0, "sum": 0})
        c["done"] += 1
        c["sum"] += price(r)
    combo_rows = sorted(combos.items(), key=lambda kv: (-kv[1]["created"] - kv[1]["done"], kv[0]))

    warranty_rows = []
    for title, flag in (("Платные", 0), ("Гарантийные", 1)):
        warranty_rows.append({
            "title": title,
            "created": sum(1 for r in created if bool(r["warranty"]) == bool(flag)),
            "done": sum(1 for r in done if bool(r["warranty"]) == bool(flag)),
            "sum": sum(price(r) for r in done if bool(r["warranty"]) == bool(flag)),
        })

    days = []
    months = []
    if key != "all" and start != end:
        cur = end
        while cur >= start:
            iso = cur.isoformat()
            dd = [r for r in done if r["done_at"][:10] == iso]
            days.append({
                "iso": iso,
                "label": cur.strftime("%d.%m"),
                "weekday": WEEKDAYS[cur.weekday()],
                "created": sum(1 for r in created if r["created_at"][:10] == iso),
                "done": len(dd),
                "sum": sum(price(r) for r in dd),
            })
            cur -= timedelta(days=1)
    if key == "all":
        keys = set()
        for r in created:
            keys.add(r["created_at"][:7])
        for r in done:
            keys.add(r["done_at"][:7])
        for k in sorted(keys, reverse=True)[:12]:
            md = [r for r in done if r["done_at"][:7] == k]
            months.append({
                "key": k,
                "created": sum(1 for r in created if r["created_at"][:7] == k),
                "done": len(md),
                "sum": sum(price(r) for r in md),
            })

    if start is None or start == end:
        range_text = ""
    else:
        range_text = f"{start.strftime('%d.%m')} — {end.strftime('%d.%m.%Y')}"

    return {
        "key": key, "caption": caption, "range_text": range_text,
        "day_value": end.isoformat() if key == "day" else "",
        "periods": PERIODS, "tiles": tiles, "brigades": brigades, "office": office,
        "pay_rows": pay_rows, "combo_rows": combo_rows, "warranty_rows": warranty_rows,
        "days": days, "months": months, "today": today.isoformat(),
    }
