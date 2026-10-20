"""
reporting.py
------------
Pure functions that turn lists of POs / invoices / payments into supplier
performance and month-end figures. No database, no network.

Conventions (shown in the UI so nobody has to guess):
  * PO value is NET (before VAT) - it is what was ordered.
  * Invoiced is the invoice TOTAL (including VAT), with the net figure
    alongside where the PDF showed one.
  * Voided invoices and rejected POs never count towards any total.
  * "Outstanding" = a live (not voided) invoice that hasn't been paid.
"""

from datetime import date, datetime, timedelta

from matching import normalise_supplier
from reminders import days_overdue

LIVE_PO_EXCLUDED = ("REJECTED",)
UNPAID_EXCLUDED = ("PAID", "VOIDED")


def _day(value):
    """Date part of an ISO-ish string, or None."""
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _month(value):
    d = _day(value)
    return d.strftime("%Y-%m") if d else None


def _round(x):
    return round(float(x or 0), 2)


def _supplier_key(name):
    return normalise_supplier(name) or str(name or "").strip().lower() or "(unknown)"


def _group_name(names):
    """Display name for a group: the most frequent raw spelling."""
    counts = {}
    for n in names:
        if n:
            counts[n] = counts.get(n, 0) + 1
    if not counts:
        return "(unknown)"
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]


def _paid_at_by_invoice(payments):
    out = {}
    for p in payments:
        out[p["invoice_id"]] = p.get("paid_at")
    return out


def _days_to_pay(invoice, paid_at):
    r, p = _day(invoice.get("received_at")), _day(paid_at)
    if not r or not p:
        return None
    return max((p - r).days, 0)


def supplier_report(pos, invoices, payments, today: date):
    """One row per supplier, biggest spend first."""
    paid_at = _paid_at_by_invoice(payments)
    groups = {}

    def grp(name):
        key = _supplier_key(name)
        g = groups.setdefault(key, {"names": [], "pos": [], "invoices": []})
        g["names"].append(name)
        return g

    for po in pos:
        if po["status"] in LIVE_PO_EXCLUDED:
            continue
        grp(po["supplier"])["pos"].append(po)
    for inv in invoices:
        if inv["status"] == "VOIDED":
            continue
        grp(inv["supplier"])["invoices"].append(inv)

    rows = []
    for key, g in groups.items():
        invs = g["invoices"]
        paid_invs = [i for i in invs if i["status"] == "PAID"]
        unpaid = [i for i in invs if i["status"] not in UNPAID_EXCLUDED]
        days = [d for d in (_days_to_pay(i, paid_at.get(i["id"])) for i in paid_invs) if d is not None]
        overdue = [i for i in unpaid if days_overdue(i.get("due_date"), today)]
        dates = [x for x in [_day(i.get("received_at")) for i in invs] + [_day(p.get("created_at")) for p in g["pos"]] if x]
        rows.append({
            "key": key,
            "supplier": _group_name(g["names"]),
            "po_count": len(g["pos"]),
            "po_value": _round(sum(p.get("actual_amount") or p["net_amount"] for p in g["pos"])),
            "invoice_count": len(invs),
            "invoiced_total": _round(sum(i["amount"] for i in invs)),
            "invoiced_net": _round(sum(i["net_amount"] for i in invs if i.get("net_amount") is not None)),
            "invoices_with_net": sum(1 for i in invs if i.get("net_amount") is not None),
            "paid_total": _round(sum(i["amount"] for i in paid_invs)),
            "outstanding": _round(sum(i["amount"] for i in unpaid)),
            "outstanding_count": len(unpaid),
            "avg_invoice": _round(sum(i["amount"] for i in invs) / len(invs)) if invs else 0.0,
            "avg_days_to_pay": round(sum(days) / len(days), 1) if days else None,
            "overdue_count": len(overdue),
            "overdue_total": _round(sum(i["amount"] for i in overdue)),
            "last_activity": max(dates).isoformat() if dates else None,
        })
    rows.sort(key=lambda r: (-(r["invoiced_total"] or r["po_value"]), r["supplier"].lower()))
    return rows


def supplier_detail(key, pos, invoices, payments, today: date):
    """Everything behind one supplier row: month-by-month invoiced / paid,
    plus the POs and invoices themselves."""
    paid_at = _paid_at_by_invoice(payments)
    my_pos = [p for p in pos if _supplier_key(p["supplier"]) == key and p["status"] not in LIVE_PO_EXCLUDED]
    my_invs = [i for i in invoices if _supplier_key(i["supplier"]) == key and i["status"] != "VOIDED"]

    months = {}

    def bucket(m):
        return months.setdefault(m, {"month": m, "invoiced": 0.0, "paid": 0.0, "po_value": 0.0})

    for i in my_invs:
        m = _month(i.get("invoice_date")) or _month(i.get("received_at"))
        if m:
            bucket(m)["invoiced"] += float(i["amount"] or 0)
        if i["status"] == "PAID":
            pm = _month(paid_at.get(i["id"]))
            if pm:
                bucket(pm)["paid"] += float(i["amount"] or 0)
    for p in my_pos:
        m = _month(p.get("created_at"))
        if m:
            bucket(m)["po_value"] += float(p.get("actual_amount") or p["net_amount"] or 0)

    month_rows = [{**v, "invoiced": _round(v["invoiced"]), "paid": _round(v["paid"]),
                   "po_value": _round(v["po_value"])} for _, v in sorted(months.items())]

    inv_rows = []
    for i in my_invs:
        inv_rows.append({
            "id": i["id"], "invoice_number": i["invoice_number"], "invoice_date": i["invoice_date"],
            "due_date": i.get("due_date"), "amount": _round(i["amount"]),
            "net_amount": i.get("net_amount"), "status": i["status"],
            "overdue_days": days_overdue(i.get("due_date"), today) if i["status"] not in UNPAID_EXCLUDED else None,
            "paid_at": paid_at.get(i["id"]),
            "days_to_pay": _days_to_pay(i, paid_at.get(i["id"])) if i["status"] == "PAID" else None,
        })
    inv_rows.sort(key=lambda r: r["invoice_date"] or "", reverse=True)
    po_rows = [{
        "id": p["id"], "po_number": p.get("po_number"), "description": p.get("description"),
        "net_amount": _round(p.get("actual_amount") or p["net_amount"]), "status": p["status"],
        "created_at": p.get("created_at"),
    } for p in my_pos]
    po_rows.sort(key=lambda r: r["created_at"] or "", reverse=True)
    return {"months": month_rows, "invoices": inv_rows, "pos": po_rows}


def month_end_pack(month: str, pos, invoices, payments):
    """Figures for one calendar month ('YYYY-MM').

    Outstanding is 'as at the last day of the month': invoices received by
    then that weren't yet paid or voided at that point."""
    try:
        year, mon = int(month[:4]), int(month[5:7])
        first = date(year, mon, 1)
    except (ValueError, IndexError):
        raise ValueError("Month must look like 2026-09.")
    next_first = date(year + (mon == 12), 1 if mon == 12 else mon + 1, 1)
    last = date.fromordinal(next_first.toordinal() - 1)

    inv_by_id = {i["id"]: i for i in invoices}

    pos_raised = [p for p in pos if _month(p.get("created_at")) == month and p["status"] not in LIVE_PO_EXCLUDED]
    inv_received = [i for i in invoices if _month(i.get("received_at")) == month and i["status"] != "VOIDED"]
    pays = [p for p in payments if _month(p.get("paid_at")) == month]
    paid_after = {p["invoice_id"]: _day(p.get("paid_at")) for p in payments}

    outstanding = []
    for i in invoices:
        received = _day(i.get("received_at"))
        if not received or received > last:
            continue
        voided = _day(i.get("voided_at"))
        if voided and voided <= last:
            continue
        if i["status"] == "VOIDED" and not voided:
            continue
        paid = paid_after.get(i["id"])
        if paid and paid <= last:
            continue
        outstanding.append(i)

    def po_row(p):
        return {"po_number": p.get("po_number") or "", "supplier": p["supplier"],
                "description": p.get("description") or "", "net_amount": _round(p.get("actual_amount") or p["net_amount"]),
                "status": p["status"], "date": (p.get("created_at") or "")[:10]}

    def inv_row(i):
        return {"invoice_number": i["invoice_number"], "supplier": i["supplier"],
                "invoice_date": i.get("invoice_date") or "", "due_date": i.get("due_date") or "",
                "amount": _round(i["amount"]),
                "net_amount": i.get("net_amount") if i.get("net_amount") is None else _round(i["net_amount"]),
                "status": i["status"], "received": (i.get("received_at") or "")[:10]}

    def pay_row(p):
        inv = inv_by_id.get(p["invoice_id"], {})
        return {"date": (p.get("paid_at") or "")[:10], "invoice_number": inv.get("invoice_number", ""),
                "supplier": inv.get("supplier", ""), "amount": _round(p["amount"]),
                "bank_reference": p.get("bank_reference") or "", "paid_by": p.get("paid_by") or ""}

    return {
        "month": month, "first_day": first.isoformat(), "last_day": last.isoformat(),
        "totals": {
            "pos_raised_count": len(pos_raised),
            "pos_raised_value": _round(sum(p.get("actual_amount") or p["net_amount"] for p in pos_raised)),
            "invoices_received_count": len(inv_received),
            "invoices_received_total": _round(sum(i["amount"] for i in inv_received)),
            "payments_count": len(pays),
            "payments_total": _round(sum(p["amount"] for p in pays)),
            "outstanding_count": len(outstanding),
            "outstanding_total": _round(sum(i["amount"] for i in outstanding)),
        },
        "pos": [po_row(p) for p in pos_raised],
        "invoices": [inv_row(i) for i in inv_received],
        "payments": [pay_row(p) for p in pays],
        "outstanding": [inv_row(i) for i in outstanding],
    }


def due_summary(invoices, today: date, soon_days: int = 7):
    """Unpaid invoices bucketed by due date. Invoices with no printed due
    date are counted separately and NEVER treated as overdue or on time."""
    live = [i for i in invoices if i["status"] not in UNPAID_EXCLUDED]
    buckets = {"overdue": [], "due_soon": [], "due_later": [], "no_date": []}
    for i in live:
        due = _day(i.get("due_date"))
        if due is None:
            buckets["no_date"].append(i)
        elif due < today:
            buckets["overdue"].append(i)
        elif (due - today).days <= soon_days:
            buckets["due_soon"].append(i)
        else:
            buckets["due_later"].append(i)

    def summ(items):
        return {"count": len(items), "total": _round(sum(i["amount"] for i in items))}

    overdue_rows = sorted(buckets["overdue"], key=lambda i: i["due_date"])
    soon_rows = sorted(buckets["due_soon"], key=lambda i: i["due_date"])
    return {
        "overdue": summ(buckets["overdue"]),
        "due_soon": summ(buckets["due_soon"]),
        "due_later": summ(buckets["due_later"]),
        "no_date": summ(buckets["no_date"]),
        "overdue_items": [{
            "id": i["id"], "supplier": i["supplier"], "invoice_number": i["invoice_number"],
            "amount": _round(i["amount"]), "due_date": i["due_date"], "status": i["status"],
            "days_overdue": days_overdue(i["due_date"], today),
        } for i in overdue_rows[:10]],
        "due_soon_items": [{
            "id": i["id"], "supplier": i["supplier"], "invoice_number": i["invoice_number"],
            "amount": _round(i["amount"]), "due_date": i["due_date"], "status": i["status"],
        } for i in soon_rows[:10]],
    }


# ---------------------------------------------------------------------------
# Weekly summary (Monday to Sunday)
# ---------------------------------------------------------------------------

def week_bounds(any_day):
    """(Monday, Sunday) of the week containing the given 'YYYY-MM-DD' date."""
    d = _day(any_day)
    if d is None:
        raise ValueError("Pick a date, e.g. 2026-10-12.")
    monday = d - timedelta(days=d.weekday())
    return monday, monday + timedelta(days=6)


def _dept(po, departments):
    name = (po or {}).get("department") or "Other"
    for d in departments:
        if name.lower() == d.lower():
            return d
    return "Other"


def weekly_summary(week, pos, invoices, payments, audit, departments):
    """Everything that happened to purchase orders and invoices in one week.

    * 'Raised' = requests people made (the per-supplier orders created when a
      multi-supplier request is issued are not new requests).
    * Approvals, rejections, issues, matches and payment approvals are taken
      from the audit trail, so they're counted on the day they happened.
    * Unpaid is 'as at the Sunday': invoices received by then and not yet paid
      or voided. Overdue only ever uses a due date that is really on the
      invoice."""
    start, end = week_bounds(week)

    def in_week(value):
        d = _day(value)
        return d is not None and start <= d <= end

    po_by_id = {p["id"]: p for p in pos}
    inv_by_id = {i["id"]: i for i in invoices}

    def inv_dept(inv):
        po = po_by_id.get(inv.get("po_id"))
        return _dept(po, departments) if po else None  # None = not matched to a PO yet

    raised = [p for p in pos if not p.get("parent_po_id") and in_week(p.get("created_at")) and p["status"] not in LIVE_PO_EXCLUDED]
    received = [i for i in invoices if in_week(i.get("received_at")) and i["status"] != "VOIDED"]
    pays = [p for p in payments if in_week(p.get("paid_at"))]

    po_events, inv_events = [], []
    approved = rejected = issued = matched = pay_approved = 0
    issued_by_dept = {d: [0, 0.0] for d in departments}
    for a in audit:
        if not in_week(a.get("timestamp")):
            continue
        action = a.get("action") or ""
        if a.get("entity_type") == "PO":
            po = po_by_id.get(a.get("entity_id"))
            if not po:
                continue
            event = None
            if action == "PO approved":
                approved += 1; event = "Approved"
            elif action == "Rejected":
                rejected += 1; event = "Rejected"
            elif action.startswith("PO issued in Xero"):
                issued += 1; event = "Issued"
                bucket = issued_by_dept[_dept(po, departments)]
                bucket[0] += 1; bucket[1] += float(po["net_amount"] or 0)
            if event:
                po_events.append({"date": a["timestamp"][:10], "po_number": po.get("po_number") or f"Request #{po['id']}",
                                  "supplier": po["supplier"], "department": _dept(po, departments), "event": event,
                                  "net_amount": _round(po.get("actual_amount") or po["net_amount"])})
        elif a.get("entity_type") == "INVOICE":
            inv = inv_by_id.get(a.get("entity_id"))
            if not inv:
                continue
            event = None
            if action == "Invoice matched to PO":
                matched += 1; event = "Matched to PO"
            elif action == "Payment approved" or action == "Payment approved - second of two approvals":
                pay_approved += 1; event = "Payment approved"
            if event:
                inv_events.append({"date": a["timestamp"][:10], "invoice_number": inv["invoice_number"],
                                   "supplier": inv["supplier"], "event": event, "amount": _round(inv["amount"])})
    po_events.sort(key=lambda e: e["date"]); inv_events.sort(key=lambda e: e["date"])

    paid_day = {p["invoice_id"]: _day(p.get("paid_at")) for p in payments}
    unpaid = []
    for i in invoices:
        r = _day(i.get("received_at"))
        if not r or r > end:
            continue
        v = _day(i.get("voided_at"))
        if v and v <= end:
            continue
        if i["status"] == "VOIDED" and not v:
            continue
        pd_ = paid_day.get(i["id"])
        if pd_ and pd_ <= end:
            continue
        unpaid.append(i)
    overdue = [i for i in unpaid if _day(i.get("due_date")) and _day(i["due_date"]) < end]

    # By department. Invoices count under the department of the PO they match.
    depts = list(departments)
    by = {d: dict(department=d, pos_raised_count=0, pos_raised_value=0.0, pos_issued_count=issued_by_dept[d][0],
                  pos_issued_value=_round(issued_by_dept[d][1]), invoices_received_count=0,
                  invoices_received_total=0.0, payments_total=0.0) for d in depts}
    for p in raised:
        b = by[_dept(p, departments)]
        b["pos_raised_count"] += 1; b["pos_raised_value"] += float(p.get("actual_amount") or p["net_amount"] or 0)
    unmatched = dict(department="Not matched to a PO yet", pos_raised_count=0, pos_raised_value=0.0, pos_issued_count=0,
                     pos_issued_value=0.0, invoices_received_count=0, invoices_received_total=0.0, payments_total=0.0)
    for i in received:
        d = inv_dept(i); b = by[d] if d else unmatched
        b["invoices_received_count"] += 1; b["invoices_received_total"] += float(i["amount"] or 0)
    for pm in pays:
        inv = inv_by_id.get(pm["invoice_id"])
        d = inv_dept(inv) if inv else None
        b = by[d] if d else unmatched
        b["payments_total"] += float(pm["amount"] or 0)
    dept_rows = [by[d] for d in depts] + ([unmatched] if unmatched["invoices_received_count"] or unmatched["payments_total"] else [])
    for r in dept_rows:
        for k in ("pos_raised_value", "invoices_received_total", "payments_total"):
            r[k] = _round(r[k])

    def po_row(p):
        return {"po_number": p.get("po_number") or "", "request_id": p["id"], "supplier": p["supplier"],
                "department": _dept(p, departments), "description": p.get("description") or "",
                "net_amount": _round(p.get("actual_amount") or p["net_amount"]), "status": p["status"],
                "date": (p.get("created_at") or "")[:10]}

    def inv_row(i):
        due = i.get("due_date") or ""
        d = inv_dept(i)
        return {"invoice_number": i["invoice_number"], "supplier": i["supplier"], "department": d or "",
                "invoice_date": i.get("invoice_date") or "", "due_date": due, "amount": _round(i["amount"]),
                "net_amount": i.get("net_amount") if i.get("net_amount") is None else _round(i["net_amount"]),
                "status": i["status"], "received": (i.get("received_at") or "")[:10],
                "overdue": bool(due and _day(due) and _day(due) < end)}

    def pay_row(pm):
        inv = inv_by_id.get(pm["invoice_id"], {})
        return {"date": (pm.get("paid_at") or "")[:10], "invoice_number": inv.get("invoice_number", ""),
                "supplier": inv.get("supplier", ""), "amount": _round(pm["amount"]),
                "bank_reference": pm.get("bank_reference") or "", "paid_by": pm.get("paid_by") or ""}

    return {
        "week_start": start.isoformat(), "week_end": end.isoformat(),
        "totals": {
            "pos_raised_count": len(raised), "pos_raised_value": _round(sum(p.get("actual_amount") or p["net_amount"] for p in raised)),
            "pos_approved": approved, "pos_rejected": rejected, "pos_issued": issued,
            "invoices_received_count": len(received), "invoices_received_total": _round(sum(i["amount"] for i in received)),
            "invoices_matched": matched, "payments_approved": pay_approved,
            "payments_count": len(pays), "payments_total": _round(sum(p["amount"] for p in pays)),
            "unpaid_count": len(unpaid), "unpaid_total": _round(sum(i["amount"] for i in unpaid)),
            "overdue_count": len(overdue), "overdue_total": _round(sum(i["amount"] for i in overdue)),
        },
        "by_department": dept_rows,
        "pos": [po_row(p) for p in raised],
        "po_activity": po_events,
        "invoices": [inv_row(i) for i in received],
        "invoice_activity": inv_events,
        "payments": [pay_row(p) for p in pays],
        "unpaid": [inv_row(i) for i in unpaid],
    }
