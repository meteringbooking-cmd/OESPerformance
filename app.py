"""
app.py
------
The shared Purchase-to-Pay service. This used to only handle "email
David an approve/reject link"; it's now the whole shared backend for
Next-Gen Purchase-to-Pay - every purchase order, invoice, payment and
audit entry lives here (see workflow.py), so every desktop install talks
to this one service instead of keeping its own separate local database.

Three groups of routes:
  - /api/*             called by every connected desktop app, guarded by
                        a shared secret (X-API-Key header)
  - /api/invoices/<id>/attachment   also called by the desktop app, but
                        opened directly in an ordinary browser tab
                        (webbrowser.open), which can't send a custom
                        header - so this one route takes the shared key
                        as a query string instead, the one deliberate
                        exception to the header rule above
  - /approve/*, /reject/*   opened by whoever received an approval
                        email - no login, security comes from the token
                        itself being an unguessable random string that's
                        only ever sent to the approver's own inbox
"""

import io
import os

from flask import Flask, abort, jsonify, request, render_template_string, send_file

from sqlalchemy import text

import db
import emailer
import workflow

app = Flask(__name__)

# Some workflow calls hand back a whole invoice row, which includes the PDF's
# raw bytes (invoices.attachment_data). JSON can't carry bytes, so without
# this any response containing such a row (e.g. submitting an invoice that
# arrived from Outlook for payment approval) failed with a server error.
# The PDF itself is only ever served from /api/invoices/<id>/attachment.
from flask.json.provider import DefaultJSONProvider as _DefaultJSONProvider

_orig_json_default = _DefaultJSONProvider.default


def _json_default(o):
    if isinstance(o, (bytes, bytearray, memoryview)):
        return None
    return _orig_json_default(o)


app.json.default = _json_default

API_KEY = os.environ.get("APP_SHARED_SECRET", "")
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")

db.init_db()


def require_api_key():
    if not API_KEY:
        # Refuse to run wide open - an empty secret would mean anyone
        # could read or change the shared data.
        abort(500, "APP_SHARED_SECRET is not set on the server.")
    if request.headers.get("X-API-Key") != API_KEY:
        abort(401, "Invalid or missing X-API-Key header.")


def require_api_key_query():
    # The one route opened directly in a browser tab rather than called
    # over HTTPS by the desktop app's own code - see the module docstring.
    if not API_KEY:
        abort(500, "APP_SHARED_SECRET is not set on the server.")
    if request.args.get("key") != API_KEY:
        abort(401, "Invalid or missing key.")


def _json():
    return request.get_json(force=True, silent=True) or {}


# ---------------------------------------------------------------------------
# Pingu (the solar job system) - raise the purchase orders for a job's costs
# ---------------------------------------------------------------------------
# Pingu runs in a web browser on another website, so it must NOT be given the
# office-wide APP_SHARED_SECRET (anyone could read it from Pingu's page). It
# has its own key, PINGU_API_KEY, which can only do two things: raise ONE
# purchase order request per Pingu job, and read back that request's status.
# It can't approve, issue, pay or see anything else on this service.
# Requests raised this way go through exactly the same approval as any other.
PINGU_API_KEY = os.environ.get("PINGU_API_KEY", "")
# The web address(es) Pingu is opened from, comma-separated, e.g.
# "https://pingu-solar.web.app". Browsers only let pages from these addresses
# call the Pingu routes. Left blank, any address is allowed (the key still applies).
PINGU_ALLOWED_ORIGINS = [o.strip().rstrip("/") for o in os.environ.get("PINGU_ALLOWED_ORIGINS", "").split(",") if o.strip()]
# Supplier names used on the Edmundson and PVF purchase orders - these must
# match the contact names in Xero exactly. The Labour PO uses the job's lead engineer.
PINGU_SUPPLIERS = {
    "Labour": None,
    "Edmundson": os.environ.get("PINGU_EDMUNDSON_SUPPLIER", "Edmundson"),
    "PVF": os.environ.get("PINGU_PVF_SUPPLIER", "PVF"),
}
PINGU_MAX_LINE = 25000.0   # sanity limit per purchase order line, in pounds


def _pingu_origin_ok(origin):
    return bool(origin) and (not PINGU_ALLOWED_ORIGINS or origin.rstrip("/") in PINGU_ALLOWED_ORIGINS)


@app.after_request
def _pingu_cors(resp):
    # Lets the Pingu web page call the /api/pingu/* routes from its own website.
    if request.path.startswith("/api/pingu/"):
        origin = request.headers.get("Origin", "")
        if _pingu_origin_ok(origin):
            resp.headers["Access-Control-Allow-Origin"] = origin
            resp.headers["Vary"] = "Origin"
            resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Pingu-Key"
            resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
            resp.headers["Access-Control-Max-Age"] = "600"
    return resp


def require_pingu_key():
    if not PINGU_API_KEY:
        abort(503, "Pingu isn't switched on for this service yet (PINGU_API_KEY is not set).")
    if request.headers.get("X-Pingu-Key") != PINGU_API_KEY:
        abort(401, "Invalid or missing X-Pingu-Key header.")


def _pingu_ref(job_number):
    return "pingu:" + job_number.strip().upper()


def _pingu_find(job_number):
    from sqlalchemy import select as _select
    with db.engine.connect() as conn:
        row = conn.execute(
            _select(db.purchase_orders.c.id)
            .where(db.purchase_orders.c.external_ref == _pingu_ref(job_number))
            .order_by(db.purchase_orders.c.id.desc())
        ).first()
    return workflow.get_po(row[0]) if row else None


def _pingu_summary(po):
    """Just what Pingu needs to show - never the whole record."""
    if not po:
        return None
    out = {
        "id": po["id"], "status": po["status"], "po_number": po.get("po_number"),
        "approver": po.get("approver"), "net_amount": po.get("net_amount"),
        "created_at": po.get("created_at"), "reject_reason": po.get("reject_reason"),
        "lines": [],
    }
    if po.get("is_split"):
        for l in workflow.list_po_lines(po["id"]):
            out["lines"].append({"supplier": l["supplier"], "description": l["description"],
                                 "net_amount": l["net_amount"], "po_number": l.get("child_po_number"),
                                 "status": l.get("child_status")})
    else:
        out["lines"].append({"supplier": po["supplier"], "description": po["description"],
                             "net_amount": po["net_amount"], "po_number": po.get("po_number"),
                             "status": po["status"]})
    return out


@app.post("/api/pingu/pos")
def pingu_raise_pos():
    """Raise the purchase order request for one Pingu job: up to three lines
    (Labour, Edmundson, PVF). Two or more lines become one Solar
    multi-supplier request - approved once, then issued in Xero as one
    purchase order per supplier. Each line's description starts with the
    job number."""
    require_pingu_key()
    data = _json()
    job = str(data.get("job_number") or "").strip()
    if not job or len(job) > 60:
        return jsonify({"ok": False, "error": "A job number is needed."}), 400
    requester = (str(data.get("requester") or "").strip() or "Pingu")[:80]

    lines, seen = [], set()
    for ln in data.get("lines") or []:
        cat = str(ln.get("category") or "").strip()
        if cat not in PINGU_SUPPLIERS:
            return jsonify({"ok": False, "error": f"Unknown cost type '{cat}'."}), 400
        if cat in seen:
            return jsonify({"ok": False, "error": f"'{cat}' appears twice."}), 400
        seen.add(cat)
        try:
            amount = round(float(ln.get("net_amount") or 0), 2)
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": f"The {cat} amount isn't a number."}), 400
        if amount <= 0 or amount > PINGU_MAX_LINE:
            return jsonify({"ok": False, "error": f"The {cat} amount must be between £0.01 and £{PINGU_MAX_LINE:,.0f}."}), 400
        supplier = PINGU_SUPPLIERS[cat] or str(ln.get("supplier") or "").strip()[:120]
        if not supplier:
            return jsonify({"ok": False, "error": "The Labour purchase order needs the job's lead engineer."}), 400
        detail = str(ln.get("detail") or "").strip()[:120]
        lines.append({"supplier": supplier, "net_amount": amount,
                      "description": f"Job {job} - {cat}" + (f" ({detail})" if detail else "")})
    if not lines:
        return jsonify({"ok": False, "error": "There are no costs to raise purchase orders for."}), 400

    existing = _pingu_find(job)
    if existing and existing["status"] not in ("REJECTED", "VOIDED"):
        return jsonify({"ok": False, "already": True, "po": _pingu_summary(existing),
                        "error": f"Purchase orders for job {job} have already been raised (request #{existing['id']})."}), 409

    try:
        po = workflow.create_po(f"{requester} (via Pingu)", "", "", 0, "Solar", lines)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)}), 400
    with db.engine.begin() as conn:
        conn.execute(db.purchase_orders.update().where(db.purchase_orders.c.id == po["id"])
                     .values(external_ref=_pingu_ref(job), source="pingu"))
    return jsonify({"ok": True, "po": _pingu_summary(workflow.get_po(po["id"]))})


@app.get("/api/pingu/pos")
def pingu_po_status():
    """Where the job's purchase order request has got to."""
    require_pingu_key()
    job = str(request.args.get("job") or "").strip()
    if not job:
        return jsonify({"ok": False, "error": "A job number is needed."}), 400
    return jsonify({"ok": True, "po": _pingu_summary(_pingu_find(job))})


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/healthz/deep")
def healthz_deep():
    """Like /healthz, but also proves the database answers, and says how
    quickly. Deliberately open (no key) and reveals nothing but up/down and
    timing - the desktop app uses it to tell "the service is asleep" from
    "the service is up but its database isn't responding"."""
    import time as _time
    started = _time.time()
    try:
        with db.engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return {"ok": True, "db": True, "db_ms": int((_time.time() - started) * 1000)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": True, "db": False, "db_ms": int((_time.time() - started) * 1000),
                "error": type(exc).__name__}


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

@app.get("/api/settings")
def api_get_settings():
    require_api_key()
    return jsonify(workflow.get_settings())


@app.post("/api/settings")
def api_update_settings():
    require_api_key()
    data = _json()
    return jsonify(workflow.update_settings(data.get("values", {}), data.get("actor", "System")))


@app.post("/api/reset-demo-data")
def api_reset_demo_data():
    require_api_key()
    with db.engine.begin() as conn:
        for table in (db.payments, db.invoices, db.advice_notes, db.purchase_orders, db.audit_log):
            conn.execute(table.delete())
    # Also forget how far the Xero and Outlook syncs had got. Those remember
    # "only look at things newer than X", so without this, clearing the data
    # left them skipping every old email and Xero purchase order - which
    # then could never be brought back in. Clearing them means the next
    # check re-reads the whole Invoices folder and Xero from scratch.
    workflow.update_settings({
        "outlook_sync_cursor": "", "xero_po_sync_cursor": "",
        "last_outlook_sync_at": "0", "last_xero_sync_at": "0",
    })
    return jsonify({"ok": True})


@app.get("/api/heartbeat")
def api_heartbeat():
    require_api_key()
    return jsonify(workflow.maybe_run_background_syncs())


# -- Xero ---------------------------------------------------------------

@app.get("/api/xero/status")
def api_xero_status():
    require_api_key()
    return jsonify(workflow.xero_status())


@app.post("/api/xero/store-connection")
def api_xero_store_connection():
    require_api_key()
    data = _json()
    return jsonify(workflow.store_xero_connection(
        data.get("client_id", ""), data.get("access_token", ""), data.get("refresh_token", ""),
        data.get("expires_in", 1800), data.get("tenant_id", ""), data.get("tenant_name", ""),
        data.get("actor", "System"),
    ))


@app.post("/api/xero/disconnect")
def api_xero_disconnect():
    require_api_key()
    data = _json()
    return jsonify(workflow.disconnect_xero(data.get("actor", "System")))


# -- Microsoft 365 / Outlook ---------------------------------------------

@app.get("/api/outlook/status")
def api_outlook_status():
    require_api_key()
    return jsonify(workflow.outlook_status())


@app.post("/api/outlook/store-connection")
def api_outlook_store_connection():
    require_api_key()
    data = _json()
    return jsonify(workflow.store_outlook_connection(
        data.get("client_id", ""), data.get("tenant", ""), data.get("access_token", ""),
        data.get("refresh_token", ""), data.get("expires_in", 3600), data.get("account_email", ""),
        data.get("folder_name", "Invoices"), data.get("anthropic_api_key", ""), data.get("actor", "System"),
    ))


@app.post("/api/outlook/disconnect")
def api_outlook_disconnect():
    require_api_key()
    data = _json()
    return jsonify(workflow.disconnect_outlook(data.get("actor", "System")))


# -- purchase orders -------------------------------------------------------

@app.get("/api/pos")
def api_list_pos():
    require_api_key()
    return jsonify(workflow.list_pos(request.args.get("status") or None))


@app.get("/api/pos/matchable")
def api_matchable_pos():
    require_api_key()
    return jsonify(workflow.matchable_pos())


@app.get("/api/pos/<int:po_id>")
def api_get_po(po_id):
    require_api_key()
    po = workflow.get_po(po_id)
    if not po:
        return jsonify(None), 404
    if po.get("is_split"):
        po["lines"] = workflow.list_po_lines(po_id)
    return jsonify(po)


@app.post("/api/pos")
def api_create_po():
    require_api_key()
    data = _json()
    try:
        po = workflow.create_po(
            data.get("requester", ""), data.get("supplier", ""), data.get("description", ""),
            float(data.get("net_amount", 0) or 0), data.get("department") or "Other", data.get("lines") or None,
        )
        return jsonify({"ok": True, "po": po})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.post("/api/pos/<int:po_id>/approve")
def api_approve_po(po_id):
    require_api_key()
    data = _json()
    return jsonify(workflow.approve_po(po_id, data.get("actor", "")))


@app.post("/api/pos/<int:po_id>/reject")
def api_reject_po(po_id):
    require_api_key()
    data = _json()
    return jsonify(workflow.reject_po(po_id, data.get("actor", ""), data.get("reason", "")))


@app.post("/api/pos/<int:po_id>/issue")
def api_issue_po(po_id):
    require_api_key()
    data = _json()
    return jsonify(workflow.issue_po(po_id, data.get("actor", "")))


@app.get("/api/pos/<int:po_id>/whatsapp-message")
def api_whatsapp_message(po_id):
    require_api_key()
    return jsonify(workflow.whatsapp_message(po_id))


@app.post("/api/pos/<int:po_id>/whatsapp-sent")
def api_whatsapp_sent(po_id):
    require_api_key()
    data = _json()
    return jsonify(workflow.log_whatsapp_sent(po_id, data.get("actor", "")))


@app.post("/api/pos/<int:po_id>/goods-received")
def api_goods_received(po_id):
    require_api_key()
    data = _json()
    return jsonify(workflow.mark_goods_received(
        po_id, data.get("actor", ""), data.get("advice_reference", ""), data.get("notes", ""),
    ))


@app.get("/api/pos/<int:po_id>/advice-notes")
def api_advice_notes(po_id):
    require_api_key()
    return jsonify(workflow.list_advice_notes(po_id))


@app.post("/api/pos/<int:po_id>/update-costs")
def api_update_po_costs(po_id):
    require_api_key()
    data = _json()
    try:
        return jsonify(workflow.update_po_costs(
            po_id, data.get("actor", ""), float(data.get("actual_amount", 0)), data.get("note", ""),
        ))
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


# -- invoices / matching / payables -----------------------------------------

@app.post("/api/invoices/simulate")
def api_simulate_invoice():
    require_api_key()
    data = _json()
    return jsonify(workflow.simulate_incoming_email(data.get("po_id")))


@app.get("/api/invoices")
def api_list_invoices():
    require_api_key()
    return jsonify(workflow.list_invoices(request.args.get("status") or None))


@app.get("/api/invoices/<int:invoice_id>/attachment")
def api_invoice_attachment(invoice_id):
    require_api_key_query()
    result = workflow.get_invoice_attachment(invoice_id)
    if not result.get("ok"):
        return result.get("error", "Not found"), 404
    return send_file(
        io.BytesIO(result["bytes"]), mimetype="application/pdf",
        as_attachment=False, download_name=result["name"],
    )


@app.post("/api/invoices/<int:invoice_id>/match")
def api_match_invoice(invoice_id):
    require_api_key()
    data = _json()
    try:
        po_id = int(data.get("po_id"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "A purchase order must be selected."}), 400
    return jsonify(workflow.match_invoice(invoice_id, po_id, data.get("actor", "")))


@app.post("/api/invoices/<int:invoice_id>/push-to-payables")
def api_push_to_payables(invoice_id):
    require_api_key()
    data = _json()
    return jsonify(workflow.push_to_payables(invoice_id, data.get("actor", "")))


@app.post("/api/invoices/<int:invoice_id>/submit-for-payment-approval")
def api_submit_for_payment_approval(invoice_id):
    require_api_key()
    data = _json()
    return jsonify(workflow.submit_for_payment_approval(invoice_id, data.get("actor", "")))


@app.post("/api/invoices/<int:invoice_id>/approve-payment")
def api_approve_payment(invoice_id):
    require_api_key()
    data = _json()
    return jsonify(workflow.approve_payment(invoice_id, data.get("actor", "")))


@app.post("/api/invoices/<int:invoice_id>/reject-payment")
def api_reject_payment(invoice_id):
    require_api_key()
    data = _json()
    return jsonify(workflow.reject_payment(invoice_id, data.get("actor", ""), data.get("reason", "")))


@app.post("/api/invoices/<int:invoice_id>/run-bank-payment")
def api_run_bank_payment(invoice_id):
    require_api_key()
    data = _json()
    return jsonify(workflow.run_bank_payment(invoice_id, data.get("actor", "")))


@app.get("/api/invoices/<int:invoice_id>/suggestions")
def api_invoice_suggestions(invoice_id):
    require_api_key()
    return jsonify(workflow.suggest_matches(invoice_id))


@app.post("/api/invoices/<int:invoice_id>/unmatch")
def api_unmatch_invoice(invoice_id):
    require_api_key()
    data = _json()
    return jsonify(workflow.unmatch_invoice(invoice_id, data.get("actor", "")))


@app.post("/api/invoices/<int:invoice_id>/void")
def api_void_invoice(invoice_id):
    require_api_key()
    data = _json()
    return jsonify(workflow.void_invoice(invoice_id, data.get("actor", ""), data.get("reason", "")))


@app.post("/api/invoices/<int:invoice_id>/restore")
def api_restore_invoice(invoice_id):
    require_api_key()
    data = _json()
    return jsonify(workflow.restore_invoice(invoice_id, data.get("actor", "")))


@app.post("/api/invoices/<int:invoice_id>/details")
def api_update_invoice_details(invoice_id):
    require_api_key()
    data = _json()
    return jsonify(workflow.update_invoice_details(invoice_id, data.get("fields") or {}, data.get("actor", "")))


@app.post("/api/approvals/remind")
def api_remind_now():
    require_api_key()
    data = _json()
    try:
        entity_id = int(data.get("id"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Nothing selected."}), 400
    return jsonify(workflow.remind_now(data.get("kind") or "po", entity_id, data.get("actor", "")))


@app.get("/api/reports/suppliers")
def api_report_suppliers():
    require_api_key()
    return jsonify(workflow.report_suppliers())


@app.get("/api/reports/suppliers/<path:key>")
def api_report_supplier_detail(key):
    require_api_key()
    return jsonify(workflow.report_supplier_detail(key))


@app.get("/api/reports/weekly")
def api_report_weekly():
    require_api_key()
    return jsonify(workflow.report_weekly(request.args.get("week") or ""))


@app.get("/api/reports/month-end")
def api_report_month_end():
    require_api_key()
    return jsonify(workflow.report_month_end(request.args.get("month") or ""))


@app.get("/api/payments")
def api_list_payments():
    require_api_key()
    return jsonify(workflow.list_payments())


# -- dashboard / audit -------------------------------------------------------

@app.get("/api/dashboard")
def api_dashboard():
    require_api_key()
    return jsonify(workflow.dashboard_summary())


@app.get("/api/audit")
def api_audit():
    require_api_key()
    return jsonify(workflow.list_audit_log(request.args.get("search") or ""))


# ---------------------------------------------------------------------------
# The original email-approval endpoints, kept unchanged. Nothing calls
# /api/pending, /api/decisions or /api/decisions/ack any more (workflow.py
# now talks to db.py directly instead of making an HTTP call to itself),
# but there's no harm keeping them; /approve/<token> and /reject/<token>
# are still very much in use - they're what the emailed link opens.
# ---------------------------------------------------------------------------

@app.post("/api/pending")
def api_create_pending():
    require_api_key()
    data = request.get_json(force=True)
    required = ("po_id", "po_number", "supplier", "description", "net_amount", "approver_name", "approver_email")
    missing = [f for f in required if f not in data]
    if missing:
        return {"ok": False, "error": f"Missing fields: {', '.join(missing)}"}, 400

    token = db.create_pending(
        po_id=data["po_id"], po_number=data["po_number"] or "", supplier=data["supplier"],
        description=data["description"], net_amount=data["net_amount"],
        approver_name=data["approver_name"], approver_email=data["approver_email"],
    )

    base = PUBLIC_BASE_URL or request.url_root.rstrip("/")
    try:
        emailer.send_approval_request(
            to_email=data["approver_email"], po_number=data["po_number"], supplier=data["supplier"],
            description=data["description"], net_amount=float(data["net_amount"]),
            approve_url=f"{base}/approve/{token}", reject_url=f"{base}/reject/{token}",
        )
    except Exception as exc:
        return {"ok": False, "error": f"Created but could not send the email: {exc}", "token": token}, 502

    return {"ok": True, "token": token}


@app.get("/api/decisions")
def api_decisions():
    require_api_key()
    rows = db.undelivered_decisions()
    return {"ok": True, "decisions": rows}


@app.post("/api/decisions/ack")
def api_ack_decisions():
    require_api_key()
    data = request.get_json(force=True)
    tokens = data.get("tokens", [])
    db.mark_delivered(tokens)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Opened by the approver, from the email
# ---------------------------------------------------------------------------

_PAGE = """
<!DOCTYPE html><html><head><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Approval</title>
<style>
  body{ font-family:'Segoe UI',Arial,sans-serif; background:#f4f4f6; margin:0; padding:40px 16px;
        display:flex; justify-content:center; }
  .card{ background:#fff; border-radius:12px; padding:32px; max-width:420px; width:100%;
         box-shadow:0 1px 3px rgba(0,0,0,0.08); }
  h1{ font-size:20px; margin:0 0 12px; }
  table{ width:100%; margin:16px 0; }
  td{ padding:4px 0; }
  td.label{ color:#777; width:110px; }
  .btn{ display:inline-block; padding:10px 22px; border-radius:6px; text-decoration:none;
        font-weight:600; color:#fff; margin-right:10px; }
  .approve{ background:#16a34a; }
  .reject{ background:#dc2626; }
  textarea{ width:100%; padding:8px; border-radius:6px; border:1px solid #ccc; font-family:inherit; }
  .muted{ color:#777; font-size:13px; }
</style></head>
<body><div class="card">{{ body|safe }}</div></body></html>
"""


def _render(body_html):
    return render_template_string(_PAGE, body=body_html)


def _noun(row):
    return "invoice payment" if (row.get("kind") or "po") == "invoice" else "purchase order"


def _po_summary_table(row):
    label = "Invoice" if (row.get("kind") or "po") == "invoice" else "PO"
    return f"""
    <table>
      <tr><td class="label">{label}</td><td><b>{row['po_number'] or '(not yet issued)'}</b></td></tr>
      <tr><td class="label">Supplier</td><td>{row['supplier']}</td></tr>
      <tr><td class="label">Description</td><td>{row['description']}</td></tr>
      <tr><td class="label">Amount</td><td><b>£{float(row['net_amount']):,.2f}</b></td></tr>
    </table>"""


@app.get("/approve/<token>")
def approve(token):
    row = db.get_pending(token)
    if not row:
        return _render("<h1>Link not recognised</h1><p class='muted'>This approval link has expired or doesn't exist.</p>"), 404

    if row["decision"] == "pending":
        row = db.decide(token, "approved")

    if row["decision"] == "cancelled":
        return _render(f"<h1>No longer needed</h1>{_po_summary_table(row)}<p class='muted'>This request was withdrawn, so there is nothing to approve.</p>")

    if row["decision"] == "approved":
        return _render(f"<h1>Approved</h1>{_po_summary_table(row)}<p class='muted'>Recorded. You can close this page - Next-Gen Purchase-to-Pay will pick this up automatically.</p>")
    else:
        return _render(f"<h1>Already actioned</h1>{_po_summary_table(row)}<p class='muted'>This {_noun(row)} was already {row['decision']} on {row['decided_at']}.</p>")


@app.get("/reject/<token>")
def reject_form(token):
    row = db.get_pending(token)
    if not row:
        return _render("<h1>Link not recognised</h1><p class='muted'>This approval link has expired or doesn't exist.</p>"), 404

    if row["decision"] == "cancelled":
        return _render(f"<h1>No longer needed</h1>{_po_summary_table(row)}<p class='muted'>This request was withdrawn, so there is nothing to reject.</p>")

    if row["decision"] != "pending":
        return _render(f"<h1>Already actioned</h1>{_po_summary_table(row)}<p class='muted'>This {_noun(row)} was already {row['decision']} on {row['decided_at']}.</p>")

    return _render(f"""
      <h1>Reject this {_noun(row)}?</h1>
      {_po_summary_table(row)}
      <form method="post" action="/reject/{token}">
        <p class="muted">A short reason helps whoever raised it (optional).</p>
        <textarea name="reason" rows="3"></textarea><br><br>
        <button class="btn reject" type="submit" style="border:none;cursor:pointer;">Confirm rejection</button>
      </form>""")


@app.post("/reject/<token>")
def reject_confirm(token):
    row = db.get_pending(token)
    if not row:
        return _render("<h1>Link not recognised</h1>"), 404
    if row["decision"] == "cancelled":
        return _render(f"<h1>No longer needed</h1>{_po_summary_table(row)}<p class='muted'>This request was withdrawn, so there is nothing to reject.</p>")
    if row["decision"] == "pending":
        reason = request.form.get("reason", "").strip()
        row = db.decide(token, "rejected", reject_reason=reason or None)
    return _render(f"<h1>Rejected</h1>{_po_summary_table(row)}<p class='muted'>Recorded. You can close this page.</p>")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")))
