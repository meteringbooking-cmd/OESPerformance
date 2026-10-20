"""
workflow.py
------------
All of the business logic for Next-Gen Purchase-to-Pay lives here - this
used to be backend/workflow.py, running locally on one PC against a
SQLite file. It's been ported here almost line for line, because now
that more than one person uses this app at once, the logic that decides
what a purchase order or invoice is allowed to do next has to run
somewhere everyone shares, not on whichever PC happens to click first.

Differences worth knowing about, versus the original single-PC version:
  - Storage is SQLAlchemy Core against db.py's tables (Postgres in
    production, SQLite for local testing) instead of raw sqlite3 SQL.
  - Email approval (previously the whole point of this service) is now
    just a direct function call (_notify_approver_if_needed, sync_
    approval_decisions) rather than a separate HTTP hop to itself.
  - Invoice PDF attachments are stored as bytes in the database
    (invoices.attachment_data), not as files on disk - Render's free
    tier wipes its filesystem on every redeploy, which would otherwise
    silently lose them.
  - Signing in to Xero/Microsoft 365 still has to happen on someone's
    actual PC (it needs a real browser) - the desktop app's own small
    xero_client.py/outlook_client.py do that part and then hand the
    resulting tokens to store_xero_connection/store_outlook_connection
    below to keep centrally.
  - A new maybe_run_background_syncs() replaces the old three separate
    polling loops on each desktop app with one paced heartbeat, so many
    desktop apps polling at once never causes duplicate work.

Status lifecycle for a Purchase Order:

    DRAFT
      -> PENDING_APPROVAL      (PO request submitted)
      -> APPROVED / REJECTED
      -> ISSUED                (PO issued from Xero, PO number generated)
      -> GOODS_RECEIVED        (goods/service taken, advice note if CEF)
      -> COSTS_UPDATED         (actual costs entered against the PO)
      -> PENDING_COST_APPROVAL
      -> COST_APPROVED / REJECTED
      -> AWAITING_INVOICE      (ready to be matched to a supplier invoice)
      -> MATCHED                (invoice matched to this PO)
      -> PAID                   (once the matched invoice is paid)

Status lifecycle for an Invoice (simulated inbound email + attachment):

    NEW -> MATCHED -> IN_PAYABLES -> PENDING_PAYMENT_APPROVAL
        -> PAYMENT_APPROVED / REJECTED -> PAID
"""

import base64
import threading
import email.utils
import os
import random
import string
import time
import urllib.parse
from datetime import datetime, timedelta, timezone

import requests
from sqlalchemy import select

import db
import emailer
import xero_client
import outlook_client
import claude_extract
import matching
import reminders
import reporting

PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def get_settings():
    with db.engine.connect() as conn:
        rows = conn.execute(select(db.settings)).mappings().all()
        return {row["key"]: row["value"] for row in rows}


# Internal bookkeeping this service updates on its own, in the background,
# purely to make things work - a sync cursor, refreshed tokens, the
# heartbeat pacing timestamps. Nobody asked for these to change and
# nothing about them is worth an audit trail entry every time a
# background poll ticks over; the moments that actually matter
# (connecting/disconnecting Xero, saving the settings form) already log
# their own clearer entry alongside this.
_SILENT_SETTING_KEYS = {
    "xero_po_sync_cursor", "xero_access_token", "xero_refresh_token",
    "xero_token_expires_at", "xero_tenant_id", "xero_tenant_name",
    "outlook_access_token", "outlook_refresh_token", "outlook_token_expires_at",
    "outlook_folder_id", "outlook_sync_cursor",
    "last_xero_sync_at", "last_outlook_sync_at", "last_approval_sync_at", "last_reminder_run_at",
}


def update_settings(values: dict, actor: str = "System"):
    with db.engine.begin() as conn:
        for key, value in values.items():
            existing = conn.execute(select(db.settings.c.key).where(db.settings.c.key == key)).first()
            if existing:
                conn.execute(db.settings.update().where(db.settings.c.key == key).values(value=str(value)))
            else:
                conn.execute(db.settings.insert().values(key=key, value=str(value)))
    if not set(values.keys()) <= _SILENT_SETTING_KEYS:
        _log("SETTING", None, None, "Settings updated", actor, ", ".join(values.keys()))
    return get_settings()


# Approval Permissions: four fixed value bands, each independently
# configurable in Settings with a name and email address. (upper_bound,
# settings_key_prefix, label) - upper_bound is inclusive, and None means
# "no upper limit" (must be the last entry).
APPROVAL_BANDS = [
    (100, "approver_band1", "Less than £100"),
    (1000, "approver_band2", "£101 to £1,000"),
    (10000, "approver_band3", "£1,001 to £10,000"),
    (None, "approver_band4", "Over £10,000"),
]


def get_approver_for_amount(amount: float, kind: str = "po") -> dict:
    """Looks up which Approval Permissions band a value falls into and
    returns who's named against it. name/email come back as "" when that
    band hasn't been configured yet. kind is "po" for purchase orders or
    "invoice" for payment approval - each has its own set of four people
    in Settings, over the same four value ranges."""
    s = get_settings()
    amount = float(amount or 0)
    for upper, key_prefix, label in APPROVAL_BANDS:
        if kind == "invoice":
            key_prefix = key_prefix.replace("approver_band", "invoice_band")
        elif kind == "invoice2":
            key_prefix = key_prefix.replace("approver_band", "invoice2_band")
        if upper is None or amount <= upper:
            return {
                "name": (s.get(f"{key_prefix}_name") or "").strip(),
                "email": (s.get(f"{key_prefix}_email") or "").strip(),
                "band": label,
            }
    return {"name": "", "email": "", "band": ""}  # unreachable - APPROVAL_BANDS always ends in None


def assign_approver(amount: float) -> str:
    name = get_approver_for_amount(amount)["name"]
    return name or "Unassigned"


# ---------------------------------------------------------------------------
# Email approval for whichever approver a PO is routed to
#
# When a PO needs approving, this emails the assigned approver an
# Approve/Reject link, so they can decide from their phone without
# opening the app. This is now a direct part of this service (it used to
# be a separate HTTP call from the desktop app to this same service) -
# nothing to configure beyond the names/emails on the Approval
# Permissions screen in Settings.
# ---------------------------------------------------------------------------

def _notify_approver_if_needed(po: dict):
    if not po or po["status"] not in ("PENDING_APPROVAL", "PENDING_COST_APPROVAL"):
        return
    approver_email = (po.get("approver_email") or "").strip()
    if not approver_email:
        return  # this band has no email configured - nothing sensible to send

    amount = po["actual_amount"] if po["status"] == "PENDING_COST_APPROVAL" and po["actual_amount"] else po["net_amount"]
    ref = po["po_number"] or f"PO request #{po['id']}"

    token = db.create_pending(
        po_id=po["id"], po_number=po["po_number"] or "", supplier=po["supplier"],
        description=po["description"], net_amount=amount,
        approver_name=po["approver"], approver_email=approver_email,
    )
    try:
        emailer.send_approval_request(
            to_email=approver_email, po_number=po["po_number"], supplier=po["supplier"],
            description=po["description"], net_amount=float(amount),
            approve_url=f"{PUBLIC_BASE_URL}/approve/{token}", reject_url=f"{PUBLIC_BASE_URL}/reject/{token}",
        )
        _log("PO", po["id"], ref, "Approval email sent", "System",
             f"Emailed {po['approver']} at {approver_email} for approval.")
    except Exception as exc:
        _log("PO", po["id"], ref, "Approval email failed", "System", str(exc))


def _apply_invoice_decision(invoice_id: int, decision: dict) -> bool:
    """Applies an Approve/Reject click on an emailed invoice link exactly as
    if the named approver had pressed the button in the app."""
    invoice = get_invoice(invoice_id)
    if not invoice or invoice["status"] != "PENDING_PAYMENT_APPROVAL":
        return False
    approver = _invoice_approver(invoice)
    if decision["decision"] not in ("approved", "rejected"):
        return False  # e.g. 'cancelled' - nothing to apply
    if decision["decision"] == "approved":
        outcome = approve_payment(invoice_id, approver)
    else:
        reason = decision.get("reject_reason") or "Rejected via emailed approval link."
        outcome = reject_payment(invoice_id, approver, reason)
    if outcome.get("ok"):
        _log("INVOICE", invoice_id, invoice["invoice_number"],
             f"Payment decision applied ({decision['decision']})", approver,
             "Actioned from the emailed approval link.")
        return True
    return False


def sync_approval_decisions(actor: str = "Email approval"):
    """Picks up any Approve/Reject decision made from an emailed link and
    applies it exactly as if the approver had clicked the button inside
    the app."""
    applied = 0
    handled_tokens = []
    for decision in db.undelivered_decisions():
        token = decision.get("token")
        try:
            po_id = int(decision.get("po_id"))
        except (TypeError, ValueError):
            handled_tokens.append(token)
            continue
        if decision.get("kind") == "invoice":
            if _apply_invoice_decision(po_id, decision):
                applied += 1
            handled_tokens.append(token)
            continue

        po = get_po(po_id)
        if not po or po["status"] not in ("PENDING_APPROVAL", "PENDING_COST_APPROVAL"):
            handled_tokens.append(token)
            continue

        if decision["decision"] == "approved":
            outcome = approve_po(po_id, po["approver"])
        else:
            reason = decision.get("reject_reason") or "Rejected via emailed approval link."
            outcome = reject_po(po_id, po["approver"], reason)

        if outcome.get("ok"):
            applied += 1
            _log("PO", po_id, po["po_number"] or f"PO request #{po_id}", f"Approval decision applied ({decision['decision']})",
                 po["approver"], "Actioned from the emailed approval link.")
        handled_tokens.append(token)

    if handled_tokens:
        db.mark_delivered(handled_tokens)
    return {"ok": True, "applied": applied}


# ---------------------------------------------------------------------------
# Xero connection
# ---------------------------------------------------------------------------

def xero_status():
    s = get_settings()
    return {"connected": s.get("xero_connected") == "1", "tenant_name": s.get("xero_tenant_name", "")}


def store_xero_connection(client_id: str, access_token: str, refresh_token: str, expires_in: int,
                           tenant_id: str, tenant_name: str, actor: str):
    update_settings({
        "xero_client_id": (client_id or "").strip(),
        "xero_connected": "1",
        "xero_access_token": access_token,
        "xero_refresh_token": refresh_token,
        "xero_token_expires_at": str(time.time() + float(expires_in)),
        "xero_tenant_id": tenant_id,
        "xero_tenant_name": tenant_name,
    })
    _log("SETTING", None, None, "Connected to Xero", actor, tenant_name)
    return {"ok": True, "tenant_name": tenant_name}


def disconnect_xero(actor: str):
    update_settings({
        "xero_connected": "0", "xero_access_token": "", "xero_refresh_token": "",
        "xero_tenant_id": "", "xero_tenant_name": "",
    })
    _log("SETTING", None, None, "Disconnected from Xero", actor, "")
    return {"ok": True}


def _ensure_xero_token():
    """Refreshes the stored Xero access token if it's expired or close to
    it. Every real Xero API call goes through this first."""
    s = get_settings()
    if s.get("xero_connected") != "1":
        return {"ok": False, "error": "Xero is not connected."}
    expires_at = float(s.get("xero_token_expires_at") or 0)
    if time.time() < expires_at - 60:
        return {"ok": True}
    result = xero_client.refresh(s["xero_client_id"], s["xero_refresh_token"])
    if not result.get("ok"):
        update_settings({"xero_connected": "0"})
        return result
    update_settings({
        "xero_access_token": result["access_token"],
        "xero_refresh_token": result["refresh_token"],
        "xero_token_expires_at": str(time.time() + result["expires_in"]),
    })
    return {"ok": True}


def _xero_headers():
    s = get_settings()
    return {
        "Authorization": f"Bearer {s['xero_access_token']}",
        "Xero-tenant-id": s["xero_tenant_id"],
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _xero_find_or_create_contact(supplier_name: str):
    ready = _ensure_xero_token()
    if not ready.get("ok"):
        return ready
    safe_name = supplier_name.replace('"', "")
    try:
        resp = requests.get(
            f"{xero_client.API_BASE}/Contacts", headers=_xero_headers(),
            params={"where": f'Name=="{safe_name}"'}, timeout=30,
        )
    except requests.RequestException as exc:
        return {"ok": False, "error": f"Could not reach Xero to look up the supplier: {exc}"}
    if resp.status_code == 200 and resp.json().get("Contacts"):
        return {"ok": True, "contact_id": resp.json()["Contacts"][0]["ContactID"]}

    try:
        create_resp = requests.post(
            f"{xero_client.API_BASE}/Contacts", headers=_xero_headers(),
            json={"Contacts": [{"Name": supplier_name}]}, timeout=30,
        )
    except requests.RequestException as exc:
        return {"ok": False, "error": f"Could not reach Xero to create the supplier: {exc}"}
    if create_resp.status_code not in (200, 201):
        return {"ok": False, "error": f"Xero would not create the supplier '{supplier_name}' ({create_resp.status_code}): {create_resp.text[:200]}"}
    return {"ok": True, "contact_id": create_resp.json()["Contacts"][0]["ContactID"]}


# ---------------------------------------------------------------------------
# Microsoft 365 / Outlook connection
# ---------------------------------------------------------------------------

def outlook_status():
    s = get_settings()
    return {
        "connected": s.get("outlook_connected") == "1",
        "account_email": s.get("outlook_account_email", ""),
        "folder_name": s.get("outlook_folder_name") or "Invoices",
        "claude_configured": bool(s.get("anthropic_api_key")),
    }


def store_outlook_connection(client_id: str, tenant: str, access_token: str, refresh_token: str,
                              expires_in: int, account_email: str, folder_name: str,
                              anthropic_api_key: str, actor: str):
    update_settings({
        "outlook_client_id": (client_id or "").strip(),
        "outlook_tenant": (tenant or "").strip(),
        "outlook_connected": "1",
        "outlook_access_token": access_token,
        "outlook_refresh_token": refresh_token,
        "outlook_token_expires_at": str(time.time() + float(expires_in)),
        "outlook_account_email": account_email or "",
        "outlook_folder_name": (folder_name or "Invoices").strip() or "Invoices",
        "outlook_folder_id": "",  # re-resolved on next sync, in case it changed
        "outlook_sync_cursor": "",
        "anthropic_api_key": (anthropic_api_key or "").strip(),
    })
    _log("SETTING", None, None, "Connected to Microsoft 365", actor, account_email or "")
    return {"ok": True, "account_email": account_email or ""}


def disconnect_outlook(actor: str):
    update_settings({
        "outlook_connected": "0", "outlook_access_token": "", "outlook_refresh_token": "",
        "outlook_account_email": "", "outlook_folder_id": "", "outlook_sync_cursor": "",
    })
    _log("SETTING", None, None, "Disconnected from Microsoft 365", actor, "")
    return {"ok": True}


def _ensure_outlook_token():
    """Refreshes the stored Microsoft 365 access token if it's expired or
    close to it. Every real Graph API call goes through this first."""
    s = get_settings()
    if s.get("outlook_connected") != "1":
        return {"ok": False, "error": "Microsoft 365 is not connected."}
    expires_at = float(s.get("outlook_token_expires_at") or 0)
    if time.time() < expires_at - 60:
        return {"ok": True}
    result = outlook_client.refresh(s["outlook_client_id"], s.get("outlook_tenant", ""), s["outlook_refresh_token"])
    if not result.get("ok"):
        update_settings({"outlook_connected": "0"})
        return result
    update_settings({
        "outlook_access_token": result["access_token"],
        "outlook_refresh_token": result["refresh_token"],
        "outlook_token_expires_at": str(time.time() + result["expires_in"]),
    })
    return {"ok": True}


def _outlook_headers():
    s = get_settings()
    return {"Authorization": f"Bearer {s['outlook_access_token']}", "Accept": "application/json"}


def _outlook_find_folder_id(folder_name: str):
    """Looks for a mail folder by name, first among top-level folders, then
    as a subfolder of Inbox - covers the two most common places someone
    creates a folder like "Invoices" in Outlook."""
    headers = _outlook_headers()
    safe_name = folder_name.strip().lower()
    try:
        for endpoint in (f"{outlook_client.GRAPH_BASE}/me/mailFolders",
                         f"{outlook_client.GRAPH_BASE}/me/mailFolders/inbox/childFolders"):
            resp = requests.get(endpoint, headers=headers, params={"$top": 100}, timeout=30)
            if resp.status_code != 200:
                continue
            for folder in resp.json().get("value", []):
                if folder.get("displayName", "").strip().lower() == safe_name:
                    return {"ok": True, "folder_id": folder["id"]}
    except requests.RequestException as exc:
        return {"ok": False, "error": f"Could not reach Microsoft 365 to look up the '{folder_name}' folder: {exc}"}
    return {
        "ok": False,
        "error": f"Could not find a mail folder named '{folder_name}'. Create it in Outlook - either as its "
                 "own top-level folder or as a subfolder of Inbox - then try again.",
    }


_OUTLOOK_SYNC_LOCK = threading.Lock()


def sync_invoices_from_outlook(actor: str = "Outlook sync"):
    """Runs the Outlook invoice check, but never two at once. Reading a PDF
    with Claude can take longer than the gap between background checks, so
    without this a second check started before the first had saved its
    progress and re-imported the same emails."""
    if not _OUTLOOK_SYNC_LOCK.acquire(blocking=False):
        return {"ok": True, "imported": 0}
    try:
        return _sync_invoices_from_outlook(actor)
    finally:
        _OUTLOOK_SYNC_LOCK.release()


def _tidy_outlook_invoices(api_key: str):
    """Housekeeping on invoices imported from Outlook that nobody has acted
    on yet (status NEW): remove exact duplicates of the same email
    attachment, and re-read any that Claude couldn't make sense of the
    first time (no invoice number / no amount) using the PDF already
    stored - so improvements to the reader apply to invoices that
    already came in, without anyone re-sending them. Each is re-read at
    most once, so a genuinely unreadable PDF doesn't cost an API call
    every minute."""
    removed = 0
    reread = 0
    with db.engine.begin() as conn:
        rows = conn.execute(
            select(db.invoices.c.id, db.invoices.c.email_sender, db.invoices.c.email_subject,
                   db.invoices.c.attachment_name, db.invoices.c.received_at)
            .where(db.invoices.c.source == "outlook", db.invoices.c.status == "NEW")
            .order_by(db.invoices.c.id)
        ).mappings().all()
        seen = {}
        for r in rows:
            key = (r["email_sender"], r["email_subject"], r["attachment_name"], r["received_at"])
            if key in seen:
                conn.execute(db.invoices.delete().where(db.invoices.c.id == r["id"]))
                removed += 1
            else:
                seen[key] = r["id"]
    if removed:
        _log("INVOICE", None, None, "Duplicate invoice emails removed", "System",
             f"{removed} duplicate import(s) of the same email attachment were removed.")

    if not api_key:
        return
    with db.engine.connect() as conn:
        candidates = conn.execute(
            select(db.invoices).where(
                db.invoices.c.source == "outlook", db.invoices.c.status == "NEW",
                db.invoices.c.attachment_data.is_not(None),
            ).order_by(db.invoices.c.id)
        ).mappings().all()
    for inv in candidates:
        if reread >= 3:
            break
        note = inv["extraction_note"] or ""
        unreadable = str(inv["invoice_number"]).startswith("UNKNOWN-") or not inv["amount"]
        # "[v2]" = read with the newer reader that also captures net/VAT,
        # due date and PO reference - invoices imported before that get one
        # more look so matching and due-date tracking can use those too.
        missing_new_fields = inv.get("net_amount") is None and inv.get("due_date") is None and not inv.get("po_reference")
        if "[v2]" in note or not (unreadable or missing_new_fields):
            continue
        reread += 1
        extracted = claude_extract.extract_invoice_fields(bytes(inv["attachment_data"]), api_key)
        values = {}
        if extracted.get("ok"):
            if unreadable:
                # Only overwrite the core fields when they were unreadable -
                # never a value someone may have corrected by hand.
                if extracted.get("invoice_number"):
                    values["invoice_number"] = extracted["invoice_number"]
                if extracted.get("invoice_date"):
                    values["invoice_date"] = extracted["invoice_date"][:10]
                if extracted.get("amount") is not None:
                    values["amount"] = extracted["amount"]
                if extracted.get("supplier_name"):
                    values["supplier"] = extracted["supplier_name"]
            values.update(_extra_invoice_fields(extracted))
            new_note = extracted.get("notes") or ""
        else:
            new_note = f"Could not read this invoice automatically ({extracted.get('error', 'unknown error')})."
        values["extraction_note"] = (new_note + " [v2]").strip()
        with db.engine.begin() as conn:
            conn.execute(db.invoices.update().where(db.invoices.c.id == inv["id"]).values(**values))
        _log("INVOICE", inv["id"], values.get("invoice_number") or inv["invoice_number"],
             "Invoice re-read", "System", values["extraction_note"])


def _extra_invoice_fields(extracted: dict) -> dict:
    """The net / VAT / due date / PO reference columns from an extraction
    result - only the ones actually found, so nothing is blanked."""
    out = {}
    for key in ("net_amount", "vat_amount", "due_date", "po_reference"):
        if extracted.get(key) is not None:
            out[key] = extracted[key]
    return out


def _sync_invoices_from_outlook(actor: str = "Outlook sync"):
    """Picks up new invoice emails from the configured folder, downloads any
    PDF attachment, asks Claude to read the invoice number/date/amount/
    supplier off it, and adds it to the invoice inbox exactly like a
    manually simulated one - just without anyone having to type those
    details in."""
    s = get_settings()
    if s.get("outlook_connected") != "1":
        return {"ok": True, "imported": 0}

    ready = _ensure_outlook_token()
    if not ready.get("ok"):
        return ready

    folder_name = s.get("outlook_folder_name") or "Invoices"
    folder_id = s.get("outlook_folder_id") or ""
    if not folder_id:
        found = _outlook_find_folder_id(folder_name)
        if not found.get("ok"):
            return found
        folder_id = found["folder_id"]
        update_settings({"outlook_folder_id": folder_id})

    _tidy_outlook_invoices((s.get("anthropic_api_key") or "").strip())

    headers = _outlook_headers()
    cursor = s.get("outlook_sync_cursor") or ""
    # Microsoft Graph rejects a message query ("The restriction or sort
    # order is too complex") unless every property used in $orderby also
    # appears in $filter, and appears FIRST. So receivedDateTime always
    # leads the filter - with an epoch default on the very first sync -
    # rather than only being added once a cursor exists.
    filter_clause = f"receivedDateTime gt {cursor or '1970-01-01T00:00:00Z'} and hasAttachments eq true"

    try:
        resp = requests.get(
            f"{outlook_client.GRAPH_BASE}/me/mailFolders/{folder_id}/messages",
            headers=headers,
            params={
                "$filter": filter_clause,
                "$orderby": "receivedDateTime asc",
                "$top": 25,
                "$select": "id,subject,from,receivedDateTime,hasAttachments",
            },
            timeout=30,
        )
    except requests.RequestException as exc:
        return {"ok": False, "error": f"Could not reach Microsoft 365 to check the '{folder_name}' folder: {exc}"}

    if resp.status_code == 404:
        update_settings({"outlook_folder_id": ""})
        return {"ok": False, "error": f"The '{folder_name}' folder could not be found - please try again."}
    if resp.status_code != 200:
        return {"ok": False, "error": f"Microsoft 365 would not list messages ({resp.status_code}): {resp.text[:300]}"}

    messages = resp.json().get("value", [])
    imported = 0
    latest_received = cursor
    api_key = s.get("anthropic_api_key") or ""

    for message in messages:
        received_at = message.get("receivedDateTime") or ""
        if received_at > latest_received:
            latest_received = received_at
        sender = (message.get("from") or {}).get("emailAddress") or {}
        subject = message.get("subject") or "(no subject)"
        for attachment in _outlook_fetch_pdf_attachments(headers, message["id"]):
            with db.engine.connect() as conn:
                already = conn.execute(select(db.invoices.c.id).where(
                    db.invoices.c.source == "outlook",
                    db.invoices.c.email_sender == (sender.get("address") or "unknown@unknown"),
                    db.invoices.c.email_subject == subject,
                    db.invoices.c.attachment_name == attachment["name"],
                    db.invoices.c.received_at == received_at,
                )).first()
            if already:
                continue  # this exact email attachment is already in the inbox
            invoice_id = _import_outlook_invoice(message, sender, subject, attachment, api_key)
            if invoice_id:
                imported += 1
        # Save progress after every email, not only at the end of the run.
        if latest_received != cursor:
            update_settings({"outlook_sync_cursor": latest_received})
            cursor = latest_received

    if latest_received != cursor:
        update_settings({"outlook_sync_cursor": latest_received})
    if imported:
        _log("INVOICE", None, None, "Invoice emails imported from Microsoft 365", actor,
             f"{imported} new invoice email(s) from the '{folder_name}' folder.")
    return {"ok": True, "imported": imported}


def _outlook_fetch_pdf_attachments(headers, message_id):
    try:
        resp = requests.get(
            f"{outlook_client.GRAPH_BASE}/me/messages/{message_id}/attachments", headers=headers, timeout=30,
        )
    except requests.RequestException:
        return []
    if resp.status_code != 200:
        return []
    result = []
    for att in resp.json().get("value", []):
        if att.get("@odata.type") != "#microsoft.graph.fileAttachment":
            continue
        name = att.get("name") or "attachment.pdf"
        content_type = (att.get("contentType") or "").lower()
        if not (name.lower().endswith(".pdf") or content_type == "application/pdf"):
            continue
        content_b64 = att.get("contentBytes")
        if not content_b64:
            continue
        try:
            result.append({"name": name, "bytes": base64.b64decode(content_b64)})
        except (ValueError, TypeError):
            continue
    return result


def _import_outlook_invoice(message, sender, subject, attachment, api_key):
    extracted = claude_extract.extract_invoice_fields(attachment["bytes"], api_key)
    sender_address = sender.get("address") or "unknown@unknown"
    received_at = message.get("receivedDateTime") or db.now_iso()

    if extracted.get("ok"):
        supplier = extracted.get("supplier_name") or sender.get("name") or sender_address.split("@")[0]
        invoice_number = extracted.get("invoice_number") or f"UNKNOWN-{message['id'][:8]}"
        invoice_date = extracted.get("invoice_date") or received_at[:10]
        amount = extracted.get("amount")
        note = extracted.get("notes") or ""
        if amount is None:
            note = (note + " Amount could not be read from the PDF - please check the attachment and correct it below.").strip()
            amount = 0.0
    else:
        supplier = sender.get("name") or sender_address.split("@")[0]
        invoice_number = f"UNKNOWN-{message['id'][:8]}"
        invoice_date = received_at[:10]
        amount = 0.0
        note = f"Could not read this invoice automatically ({extracted.get('error', 'unknown error')}) - please check the attachment and fill in the details."

    with db.engine.begin() as conn:
        result = conn.execute(db.invoices.insert().values(
            po_id=None, supplier=supplier, invoice_number=invoice_number, invoice_date=invoice_date[:10],
            amount=amount, attachment_name=attachment["name"], attachment_data=attachment["bytes"],
            email_subject=subject, email_sender=sender_address, received_at=received_at,
            status="NEW", source="outlook", extraction_note=((note + " [v2]").strip() if extracted.get("ok") else note) or None,
            **(_extra_invoice_fields(extracted) if extracted.get("ok") else {}),
        ))
        invoice_id = result.inserted_primary_key[0]

    detail = f"From {sender_address} - £{amount:,.2f}"
    due = extracted.get("due_date") if extracted.get("ok") else None
    _log("INVOICE", invoice_id, invoice_number, "Invoice email received (Microsoft 365)", "System",
         detail + (f", due {due}" if due else "") + (f" - {note}" if note else ""))
    return invoice_id


def get_invoice_attachment(invoice_id: int):
    """Returns {"ok": True, "name", "bytes"} for the saved PDF of an
    invoice imported from a real Outlook sync, or {"ok": False, "error"}.
    Simulated invoices never have a real attachment."""
    with db.engine.connect() as conn:
        row = conn.execute(select(db.invoices.c.attachment_name, db.invoices.c.attachment_data)
                           .where(db.invoices.c.id == invoice_id)).mappings().first()
    if not row or not row["attachment_data"]:
        return {"ok": False, "error": "No attachment is saved for this invoice."}
    return {"ok": True, "name": row["attachment_name"] or "invoice.pdf", "bytes": row["attachment_data"]}


# ---------------------------------------------------------------------------
# Audit trail
# ---------------------------------------------------------------------------

def _log(entity_type, entity_id, reference, action, actor, detail=""):
    with db.engine.begin() as conn:
        conn.execute(db.audit_log.insert().values(
            entity_type=entity_type, entity_id=entity_id, reference=reference, action=action,
            actor=actor, detail=detail, timestamp=db.now_iso(),
        ))


def list_audit_log(search: str = ""):
    with db.engine.connect() as conn:
        stmt = select(db.audit_log).order_by(db.audit_log.c.id.desc())
        if search:
            like = f"%{search}%"
            stmt = select(db.audit_log).where(
                (db.audit_log.c.reference.ilike(like)) | (db.audit_log.c.actor.ilike(like)) |
                (db.audit_log.c.action.ilike(like)) | (db.audit_log.c.detail.ilike(like))
            ).order_by(db.audit_log.c.id.desc())
        rows = conn.execute(stmt).mappings().all()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Purchase orders
# ---------------------------------------------------------------------------

def list_pos(status: str = None):
    with db.engine.connect() as conn:
        stmt = select(db.purchase_orders).order_by(db.purchase_orders.c.id.desc())
        if status:
            stmt = stmt.where(db.purchase_orders.c.status == status)
        rows = conn.execute(stmt).mappings().all()
        return [dict(r) for r in rows]


def get_po(po_id: int):
    with db.engine.connect() as conn:
        row = conn.execute(select(db.purchase_orders).where(db.purchase_orders.c.id == po_id)).mappings().first()
        return dict(row) if row else None


DEPARTMENTS = ["Solar", "Metering", "Training", "Other"]
MULTI_SUPPLIER_DEPARTMENT = "Solar"


def normalise_department(value) -> str:
    v = (value or "").strip()
    for d in DEPARTMENTS:
        if v.lower() == d.lower():
            return d
    return "Other"


def create_po(requester: str, supplier: str, description: str, net_amount: float,
              department: str = "Other", lines: list = None):
    """Raises a purchase order request.

    A Solar request can carry several suppliers (lines). It is still ONE
    request to approve - routed by the combined value - and is split into one
    purchase order per supplier when it is issued."""
    department = normalise_department(department)
    clean_lines = []
    for ln in (lines or []):
        sup = str(ln.get("supplier") or "").strip()
        desc = str(ln.get("description") or "").strip()
        amt = float(ln.get("net_amount") or 0)
        if not sup and not desc and not amt:
            continue  # an empty row the user never filled in
        if not sup or not desc or amt <= 0:
            raise ValueError("Every supplier line needs a supplier, a description and an amount above zero.")
        clean_lines.append({"supplier": sup, "description": desc, "net_amount": round(amt, 2)})

    if len(clean_lines) == 1:  # one supplier is just a normal purchase order
        supplier, description, net_amount = (clean_lines[0]["supplier"], clean_lines[0]["description"],
                                             clean_lines[0]["net_amount"])
        clean_lines = []
    if len(clean_lines) > 1 and department != MULTI_SUPPLIER_DEPARTMENT:
        raise ValueError(f"Only {MULTI_SUPPLIER_DEPARTMENT} purchase orders can have more than one supplier.")

    is_split = len(clean_lines) > 1
    if is_split:
        net_amount = round(sum(l["net_amount"] for l in clean_lines), 2)
        supplier = f"Multiple suppliers ({len(clean_lines)})"
        description = f"{department} - " + "; ".join(
            f"{l['supplier']}: {l['description']} £{l['net_amount']:,.2f}" for l in clean_lines)

    band = get_approver_for_amount(net_amount)
    approver = band["name"] or "Unassigned"
    with db.engine.begin() as conn:
        result = conn.execute(db.purchase_orders.insert().values(
            po_number=None, requester=requester, supplier=supplier, description=description,
            net_amount=net_amount, approver=approver, approver_email=band["email"] or None, status="PENDING_APPROVAL",
            created_at=db.now_iso(), updated_at=db.now_iso(), department=department, is_split=1 if is_split else 0,
        ))
        po_id = result.inserted_primary_key[0]
        for l in clean_lines:
            conn.execute(db.po_lines.insert().values(po_id=po_id, **l))
    if is_split:
        detail = (f"{department} - {len(clean_lines)} suppliers, £{net_amount:,.2f} in total, routed to {approver}. "
                  "It will be issued as one purchase order per supplier. " +
                  "; ".join(f"{l['supplier']} £{l['net_amount']:,.2f}" for l in clean_lines))
    else:
        detail = f"{department} - {supplier} - {description} - £{net_amount:,.2f} - routed to {approver}"
    _log("PO", po_id, f"PO request #{po_id}", "PO requested", requester, detail)
    po = get_po(po_id)
    _notify_approver_if_needed(po)
    return po


def list_po_lines(po_id: int):
    with db.engine.connect() as conn:
        rows = conn.execute(select(db.po_lines).where(db.po_lines.c.po_id == po_id)
                            .order_by(db.po_lines.c.id)).mappings().all()
        lines = [dict(r) for r in rows]
        for l in lines:
            l["child_po_number"] = None
            l["child_status"] = None
            if l["child_po_id"]:
                c = conn.execute(select(db.purchase_orders.c.po_number, db.purchase_orders.c.status)
                                 .where(db.purchase_orders.c.id == l["child_po_id"])).first()
                if c:
                    l["child_po_number"], l["child_status"] = c[0], c[1]
        return lines


def _set_status(po_id, status, actor, action, detail=""):
    po = get_po(po_id)
    with db.engine.begin() as conn:
        conn.execute(db.purchase_orders.update().where(db.purchase_orders.c.id == po_id).values(
            status=status, updated_at=db.now_iso(),
        ))
    ref = po["po_number"] if po and po["po_number"] else f"PO request #{po_id}"
    _log("PO", po_id, ref, action, actor, detail)


def approve_po(po_id: int, actor: str):
    po = get_po(po_id)
    if not po:
        return {"ok": False, "error": "Purchase order not found."}
    if po["status"] not in ("PENDING_APPROVAL", "PENDING_COST_APPROVAL"):
        return {"ok": False, "error": "This purchase order is not currently awaiting approval."}
    if actor != po["approver"]:
        return {"ok": False, "error": f"Only {po['approver']} is authorised to approve this purchase order."}

    if po["status"] == "PENDING_APPROVAL":
        _set_status(po_id, "APPROVED", actor, "PO approved",
                     f"Approved by {actor} - £{po['net_amount']:,.2f}")
    else:
        _set_status(po_id, "COST_APPROVED", actor, "Updated costs approved",
                     f"Approved by {actor} - £{po['actual_amount']:,.2f}")
        _set_status(po_id, "AWAITING_INVOICE", actor, "Ready for invoice matching", "")
    updated = get_po(po_id)
    _push_po_decision_to_xero(updated, "approved")
    return {"ok": True, "po": updated}


def reject_po(po_id: int, actor: str, reason: str):
    po = get_po(po_id)
    if not po:
        return {"ok": False, "error": "Purchase order not found."}
    if po["status"] not in ("PENDING_APPROVAL", "PENDING_COST_APPROVAL"):
        return {"ok": False, "error": "This purchase order is not currently awaiting approval."}
    if actor != po["approver"]:
        return {"ok": False, "error": f"Only {po['approver']} is authorised to action this purchase order."}
    with db.engine.begin() as conn:
        conn.execute(db.purchase_orders.update().where(db.purchase_orders.c.id == po_id).values(
            status="REJECTED", reject_reason=reason, updated_at=db.now_iso(),
        ))
    _log("PO", po_id, po["po_number"] or f"PO request #{po_id}", "Rejected", actor, reason)
    updated = get_po(po_id)
    _push_po_decision_to_xero(updated, "rejected", reason)
    return {"ok": True, "po": updated}


def _push_po_decision_to_xero(po: dict, decision: str, reason: str = ""):
    """For a PO that started life in Xero (raised there directly, then
    imported), reflects an approve/reject decision made in this app back
    onto that same record in Xero, so accounts see it there too. Never
    raises - a failure here shouldn't stop the approval/rejection from
    being recorded, which has already happened by the time this runs."""
    if not po or po.get("source") != "xero" or not po.get("xero_po_id"):
        return
    if get_settings().get("xero_connected") != "1":
        return

    xero_po_id = po["xero_po_id"]
    try:
        ready = _ensure_xero_token()
        if not ready.get("ok"):
            raise RuntimeError(ready.get("error", "Could not refresh the Xero connection."))

        resp = requests.get(f"{xero_client.API_BASE}/PurchaseOrders/{xero_po_id}", headers=_xero_headers(), timeout=30)
        if resp.status_code != 200:
            raise RuntimeError(f"Could not fetch it from Xero ({resp.status_code}): {resp.text[:300]}")
        current = resp.json()["PurchaseOrders"][0]

        body = {
            "PurchaseOrderID": xero_po_id,
            "Contact": {"ContactID": current["Contact"]["ContactID"]},
            "Date": current["Date"],
            "LineItems": current.get("LineItems", []),
        }
        if current.get("DeliveryDate"):
            body["DeliveryDate"] = current["DeliveryDate"]

        if decision == "approved":
            body["Status"] = "AUTHORISED"
            body["Reference"] = current.get("Reference") or ""
        else:
            body["Status"] = current.get("Status", "DRAFT")
            flag = f"REJECTED in Purchase-to-Pay - {reason}".strip(" -")
            existing_ref = current.get("Reference") or ""
            body["Reference"] = f"{flag}. {existing_ref}".strip() if existing_ref else flag

        resp = requests.post(f"{xero_client.API_BASE}/PurchaseOrders", headers=_xero_headers(), json={"PurchaseOrders": [body]}, timeout=30)
        if resp.status_code not in (200, 201):
            raise RuntimeError(f"Xero rejected the update ({resp.status_code}): {resp.text[:300]}")
    except Exception as exc:
        _log("PO", po["id"], po["po_number"] or f"PO request #{po['id']}", "Xero update failed",
             "System", f"Could not reflect this {decision} decision back onto the Xero record: {exc}")
        return

    _log("PO", po["id"], po["po_number"] or f"PO request #{po['id']}", "Xero updated",
         "System", f"Marked {'Authorised' if decision == 'approved' else 'flagged as rejected'} on the matching record in Xero.")


def _create_xero_po(supplier: str, description: str, net_amount: float, requester: str):
    """Creates one Authorised purchase order in Xero. Returns
    {"ok": True, "po_number", "xero_po_id"} or {"ok": False, "error"}."""
    contact = _xero_find_or_create_contact(supplier)
    if not contact.get("ok"):
        return contact
    body = {
        "PurchaseOrders": [{
            "Contact": {"ContactID": contact["contact_id"]},
            "Date": db.now_iso()[:10],
            "Reference": f"Requested by {requester}",
            "Status": "AUTHORISED",
            "LineItems": [{
                "Description": description,
                "Quantity": 1,
                "UnitAmount": net_amount,
            }],
        }]
    }
    try:
        resp = requests.post(f"{xero_client.API_BASE}/PurchaseOrders", headers=_xero_headers(), json=body, timeout=30)
    except requests.RequestException as exc:
        return {"ok": False, "error": f"Could not reach Xero to issue this purchase order: {exc}"}
    if resp.status_code not in (200, 201):
        return {"ok": False, "error": f"Xero rejected the purchase order ({resp.status_code}): {resp.text[:300]}"}
    created = resp.json()["PurchaseOrders"][0]
    return {"ok": True, "po_number": created["PurchaseOrderNumber"], "xero_po_id": created.get("PurchaseOrderID")}


def _simulated_po_number() -> str:
    """Next PO-YYYY-NNNN number, used when Xero isn't connected."""
    with db.engine.connect() as conn:
        count = conn.execute(
            select(db.purchase_orders.c.id).where(db.purchase_orders.c.po_number.isnot(None))
        ).fetchall()
    return f"PO-{db.now_iso()[:4]}-{len(count) + 1:04d}"


def issue_po(po_id: int, actor: str):
    po = get_po(po_id)
    if not po or po["status"] != "APPROVED":
        return {"ok": False, "error": "Only an approved purchase order can be issued in Xero."}

    if po.get("is_split"):
        return _issue_split_po(po, actor)

    if po.get("xero_po_id"):
        _set_status(po_id, "ISSUED", actor, "Approval confirmed",
                     f"Already live in Xero as {po['po_number']} - no further action needed there.")
        return {"ok": True, "po": get_po(po_id)}

    if get_settings().get("xero_connected") == "1":
        res = _create_xero_po(po["supplier"], po["description"], po["net_amount"], po["requester"])
        if not res.get("ok"):
            return res
        po_number, xero_po_id = res["po_number"], res["xero_po_id"]
        with db.engine.begin() as conn:
            conn.execute(db.purchase_orders.update().where(db.purchase_orders.c.id == po_id).values(
                po_number=po_number, xero_po_id=xero_po_id, status="ISSUED", updated_at=db.now_iso(),
            ))
        _log("PO", po_id, po_number, "PO issued in Xero", actor, f"{po['supplier']} - £{po['net_amount']:,.2f}")
        return {"ok": True, "po": get_po(po_id)}

    # Not connected to Xero - fall back to the simulation.
    po_number = _simulated_po_number()
    with db.engine.begin() as conn:
        conn.execute(db.purchase_orders.update().where(db.purchase_orders.c.id == po_id).values(
            po_number=po_number, status="ISSUED", updated_at=db.now_iso(),
        ))
    _log("PO", po_id, po_number, "PO issued in Xero (simulated)", actor,
         f"{po['supplier']} - £{po['net_amount']:,.2f}")
    return {"ok": True, "po": get_po(po_id)}


def _issue_split_po(parent: dict, actor: str):
    """Issues a multi-supplier request as one purchase order per supplier.
    Each is recorded the moment Xero accepts it, so if one fails the ones
    already made are kept and a second press of Issue only creates the rest."""
    parent_id = parent["id"]
    lines = list_po_lines(parent_id)
    if not lines:
        return {"ok": False, "error": "This request has no supplier lines to issue."}
    connected = get_settings().get("xero_connected") == "1"
    parent_ref = f"PO request #{parent_id}"
    todo = [l for l in lines if not l["child_po_id"]]

    for n, line in enumerate(todo, start=1):
        if connected:
            res = _create_xero_po(line["supplier"], line["description"], line["net_amount"], parent["requester"])
            if not res.get("ok"):
                made = len(lines) - len(todo) + (n - 1)
                _log("PO", parent_id, parent_ref, "Split issue incomplete", actor,
                     f"{made} of {len(lines)} purchase orders created; failed on {line['supplier']}: {res.get('error')}")
                return {"ok": False, "error": (
                    f"{res.get('error')} ({made} of {len(lines)} purchase orders were created before this; "
                    "press Issue again to create the rest - the ones already made won't be duplicated.)")}
            number, xero_id = res["po_number"], res["xero_po_id"]
        else:
            number, xero_id = _simulated_po_number(), None
        with db.engine.begin() as conn:
            child = conn.execute(db.purchase_orders.insert().values(
                po_number=number, requester=parent["requester"], supplier=line["supplier"],
                description=line["description"], net_amount=line["net_amount"], approver=parent["approver"],
                approver_email=parent.get("approver_email"), status="ISSUED", created_at=db.now_iso(),
                updated_at=db.now_iso(), xero_po_id=xero_id, department=parent.get("department"),
                parent_po_id=parent_id,
            ))
            child_id = child.inserted_primary_key[0]
            conn.execute(db.po_lines.update().where(db.po_lines.c.id == line["id"]).values(child_po_id=child_id))
        _log("PO", child_id, number, "PO issued in Xero" + ("" if connected else " (simulated)"), actor,
             f"{line['supplier']} - £{line['net_amount']:,.2f}. Part of {parent_ref} (approved as one request).")

    with db.engine.begin() as conn:
        conn.execute(db.purchase_orders.update().where(db.purchase_orders.c.id == parent_id).values(
            status="SPLIT", updated_at=db.now_iso()))
    final = list_po_lines(parent_id)
    _log("PO", parent_id, parent_ref, "Split into separate purchase orders", actor,
         f"{len(final)} purchase orders: " + ", ".join(f"{l['child_po_number']} ({l['supplier']})" for l in final))
    return {"ok": True, "po": get_po(parent_id), "children": final}


def sync_pos_from_xero(actor: str = "Xero sync"):
    """Pulls in any purchase order that was created directly in Xero rather
    than raised through this app - e.g. by someone else in the business
    working straight in Xero. Anything already known here (because this
    app issued it, or a previous sync already imported it) is left
    untouched. Imported POs land straight at ISSUED: whoever created them
    in Xero already had the standing to do so, so there's no reason to
    send them through the approval step in here as well."""
    s = get_settings()
    if s.get("xero_connected") != "1":
        return {"ok": True, "imported": 0}

    ready = _ensure_xero_token()
    if not ready.get("ok"):
        return ready

    headers = _xero_headers()
    cursor = s.get("xero_po_sync_cursor") or ""
    if cursor:
        headers["If-Modified-Since"] = cursor

    try:
        resp = requests.get(f"{xero_client.API_BASE}/PurchaseOrders", headers=headers, timeout=30)
    except requests.RequestException as exc:
        return {"ok": False, "error": f"Could not reach Xero to check for new purchase orders: {exc}"}

    sync_time = email.utils.format_datetime(datetime.now(timezone.utc), usegmt=True)

    if resp.status_code == 304:
        update_settings({"xero_po_sync_cursor": sync_time})
        return {"ok": True, "imported": 0}
    if resp.status_code != 200:
        return {"ok": False, "error": f"Xero would not return purchase orders ({resp.status_code}): {resp.text[:300]}"}

    imported = 0
    newly_imported_ids = []
    with db.engine.begin() as conn:
        for p in resp.json().get("PurchaseOrders", []):
            if p.get("Status") == "DELETED":
                continue
            xero_po_id = p.get("PurchaseOrderID")
            if not xero_po_id:
                continue
            if conn.execute(select(db.purchase_orders.c.id).where(db.purchase_orders.c.xero_po_id == xero_po_id)).first():
                continue  # already tracked - either issued from this app, or imported before

            po_number = p.get("PurchaseOrderNumber") or ""
            if po_number:
                existing = conn.execute(select(db.purchase_orders.c.id).where(db.purchase_orders.c.po_number == po_number)).first()
                if existing:
                    conn.execute(db.purchase_orders.update().where(db.purchase_orders.c.id == existing[0]).values(xero_po_id=xero_po_id))
                    continue

            line_items = p.get("LineItems") or []
            net_amount = p.get("SubTotal")
            if net_amount is None:
                net_amount = sum(li.get("LineAmount", 0) for li in line_items)
            description = "; ".join(li.get("Description", "") for li in line_items if li.get("Description")) \
                or "(no description given in Xero)"
            supplier = (p.get("Contact") or {}).get("Name") or "Unknown supplier"
            band = get_approver_for_amount(net_amount)
            approver = band["name"] or "Unassigned"

            result = conn.execute(db.purchase_orders.insert().values(
                po_number=po_number or None, requester="Created directly in Xero", supplier=supplier,
                description=description, net_amount=net_amount, approver=approver, approver_email=band["email"] or None,
                status="PENDING_APPROVAL",
                created_at=db.now_iso(), updated_at=db.now_iso(), xero_po_id=xero_po_id, source="xero",
            ))
            new_id = result.inserted_primary_key[0]
            imported += 1
            _log("PO", new_id, po_number or f"PO import #{new_id}", "PO imported from Xero", actor,
                 f"{supplier} - £{net_amount:,.2f} - created directly in Xero, not through this app - "
                 f"routed to {approver} for approval")
            newly_imported_ids.append(new_id)

    for new_id in newly_imported_ids:
        _notify_approver_if_needed(get_po(new_id))

    update_settings({"xero_po_sync_cursor": sync_time})
    return {"ok": True, "imported": imported}


def whatsapp_message(po_id: int):
    po = get_po(po_id)
    if not po:
        return {"ok": False, "error": "Purchase order not found."}
    text = (
        f"Hello, please find Purchase Order {po['po_number'] or '(not yet issued)'} "
        f"for {po['supplier']} - {po['description']} - £{po['net_amount']:,.2f}. "
        f"Please confirm receipt."
    )
    link = "https://api.whatsapp.com/send?text=" + urllib.parse.quote(text)
    return {"ok": True, "message": text, "link": link}


def log_whatsapp_sent(po_id: int, actor: str):
    """Opening WhatsApp itself is a local action on whichever PC clicked
    it (see the desktop app's open_whatsapp) - this just records that it
    happened, in the shared audit trail."""
    po = get_po(po_id)
    if not po:
        return {"ok": False, "error": "Purchase order not found."}
    _log("PO", po_id, po["po_number"], "PO sent via WhatsApp", actor, "")
    return {"ok": True}


def mark_goods_received(po_id: int, actor: str, advice_reference: str = "", notes: str = ""):
    po = get_po(po_id)
    if not po or po["status"] != "ISSUED":
        return {"ok": False, "error": "Only an issued purchase order can be marked as goods/service received."}

    s = get_settings()
    cef_supplier = (s.get("advice_note_supplier") or "").strip().lower()
    requires_advice_note = bool(cef_supplier) and po["supplier"].strip().lower() == cef_supplier

    if requires_advice_note and not advice_reference.strip():
        return {
            "ok": False,
            "error": f"{po['supplier']} requires an advice note reference before goods receipt can be confirmed.",
        }

    with db.engine.begin() as conn:
        if requires_advice_note:
            conn.execute(db.advice_notes.insert().values(
                po_id=po_id, reference=advice_reference.strip(), logged_by=actor,
                logged_at=db.now_iso(), notes=notes.strip(),
            ))
        conn.execute(db.purchase_orders.update().where(db.purchase_orders.c.id == po_id).values(
            status="GOODS_RECEIVED", updated_at=db.now_iso(),
        ))

    detail = f"Advice note {advice_reference.strip()}" if requires_advice_note else "No advice note required"
    _log("PO", po_id, po["po_number"], "Goods/service receipt confirmed", actor, detail)
    return {"ok": True, "po": get_po(po_id)}


def list_advice_notes(po_id: int):
    with db.engine.connect() as conn:
        rows = conn.execute(
            select(db.advice_notes).where(db.advice_notes.c.po_id == po_id).order_by(db.advice_notes.c.id.desc())
        ).mappings().all()
        return [dict(r) for r in rows]


def update_po_costs(po_id: int, actor: str, actual_amount: float, note: str = ""):
    po = get_po(po_id)
    if not po or po["status"] != "GOODS_RECEIVED":
        return {"ok": False, "error": "Costs can only be updated once goods/service receipt has been confirmed."}

    new_band = get_approver_for_amount(actual_amount)
    new_approver = new_band["name"] or "Unassigned"
    with db.engine.begin() as conn:
        conn.execute(db.purchase_orders.update().where(db.purchase_orders.c.id == po_id).values(
            actual_amount=actual_amount, approver=new_approver, approver_email=new_band["email"] or None,
            status="PENDING_COST_APPROVAL", updated_at=db.now_iso(),
        ))

    variance = actual_amount - po["net_amount"]
    detail = f"Actual £{actual_amount:,.2f} vs original £{po['net_amount']:,.2f} (variance £{variance:,.2f}). {note}".strip()
    _log("PO", po_id, po["po_number"], "PO costs updated", actor, detail)
    updated = get_po(po_id)
    _notify_approver_if_needed(updated)
    return {"ok": True, "po": updated}


# ---------------------------------------------------------------------------
# Invoices
# ---------------------------------------------------------------------------

_DEMO_SUPPLIERS = [
    ("CEF", 1),
    ("Toolstation", 1),
    ("Screwfix", 1),
    ("Rexel", 1),
    ("City Plumbing", 1),
]


def simulate_incoming_email(po_id: int = None):
    """Creates a plausible-looking inbound invoice email/attachment,
    optionally referencing a real PO in the system so it can be
    matched."""
    po = get_po(po_id) if po_id else None

    if po:
        supplier = po["supplier"]
        base_amount = po["actual_amount"] or po["net_amount"]
        amount = round(base_amount + random.choice([0, 0, 0, 3.50, -2.10, 12.00]), 2)
        subject = f"Invoice for {po['po_number'] or 'your recent order'} - {supplier}"
    else:
        supplier = random.choice(_DEMO_SUPPLIERS)[0]
        amount = round(random.uniform(45, 1800), 2)
        subject = f"Your invoice from {supplier}"

    invoice_number = "INV-" + "".join(random.choices(string.digits, k=6))
    attachment = f"{invoice_number}.pdf"
    sender = supplier.lower().replace(" ", "") + "@suppliers.example.com"

    with db.engine.begin() as conn:
        result = conn.execute(db.invoices.insert().values(
            po_id=po["id"] if po else None, supplier=supplier, invoice_number=invoice_number,
            invoice_date=db.now_iso()[:10], amount=amount, attachment_name=attachment,
            email_subject=subject, email_sender=sender, received_at=db.now_iso(), status="NEW",
        ))
        invoice_id = result.inserted_primary_key[0]

    _log("INVOICE", invoice_id, invoice_number, "Invoice email received (simulated Outlook inbox)",
         "system", f"From {sender} - £{amount:,.2f}")
    return get_invoice(invoice_id)


# Every invoice column except the PDF itself. The PDF bytes live in the
# database and can be megabytes each - they are only ever fetched on their
# own (get_invoice_attachment), never as part of a list or a lookup.
_INVOICE_COLS = [c for c in db.invoices.c if c.name != "attachment_data"]


def get_invoice(invoice_id: int):
    with db.engine.connect() as conn:
        row = conn.execute(
            select(*_INVOICE_COLS, db.invoices.c.attachment_data.isnot(None).label("has_attachment"))
            .where(db.invoices.c.id == invoice_id)
        ).mappings().first()
        if not row:
            return None
        d = dict(row)
        d["has_attachment"] = bool(d.get("has_attachment"))
        return d


def list_invoices(status: str = None):
    with db.engine.connect() as conn:
        # The PDF bytes are never read here (only "is there one?"), so the
        # list stays small and fast however many invoices there are.
        stmt = select(*_INVOICE_COLS, db.invoices.c.attachment_data.isnot(None).label("has_attachment")
                      ).order_by(db.invoices.c.id.desc())
        if status:
            stmt = stmt.where(db.invoices.c.status == status)
        rows = conn.execute(stmt).mappings().all()
        out = []
        for r in rows:
            d = dict(r)
            d["has_attachment"] = bool(d.get("has_attachment"))
            out.append(d)
    today = _today()
    candidate_pos = None
    for d in out:
        # Due-date helpers: only ever set when a real due date exists, so
        # "no date" is never mistaken for "on time" or "late".
        live_unpaid = d["status"] not in ("PAID", "VOIDED")
        d["days_overdue"] = reminders.days_overdue(d.get("due_date"), today) if live_unpaid else None
        d["days_until_due"] = reminders.days_until_due(d.get("due_date"), today) if live_unpaid else None
        d["suggested_match"] = None
        if d["status"] == "NEW" and d["amount"]:
            if candidate_pos is None:
                candidate_pos = matchable_pos()
            best = matching.best_suggestion(d, candidate_pos)
            if best:
                d["suggested_match"] = {k: best[k] for k in (
                    "po_id", "po_number", "supplier", "po_amount", "variance", "net_basis", "confidence", "reasons")}
    return out


def _today():
    """Today's date in UK time (the same clock as every stored timestamp)."""
    return datetime.fromisoformat(db.now_iso()).date()


# An invoice can be matched to any PO that has been issued to the supplier -
# it doesn't have to wait for goods to be booked in (invoices often arrive
# first). Automatic matching stays stricter: it only touches POs whose goods
# have been confirmed (AWAITING_INVOICE), so nothing skips goods receipt
# without a person choosing it.
MATCHABLE_PO_STATUSES = ("ISSUED", "GOODS_RECEIVED", "AWAITING_INVOICE")


def matchable_pos(auto: bool = False):
    """POs that an invoice can be matched against."""
    statuses = ("AWAITING_INVOICE",) if auto else MATCHABLE_PO_STATUSES
    with db.engine.connect() as conn:
        rows = conn.execute(
            select(db.purchase_orders).where(db.purchase_orders.c.status.in_(statuses))
            .order_by(db.purchase_orders.c.id.desc())
        ).mappings().all()
        return [dict(r) for r in rows]


def match_invoice(invoice_id: int, po_id: int, actor: str):
    invoice = get_invoice(invoice_id)
    po = get_po(po_id)
    if not invoice or invoice["status"] != "NEW":
        return {"ok": False, "error": "This invoice is not available to match."}
    if not po or po["status"] not in MATCHABLE_PO_STATUSES:
        return {"ok": False, "error": "This purchase order is not ready for invoice matching (it must be issued first)."}
    prior_status = po["status"]

    po_amount = matching.po_value(po)
    # Compared BEFORE VAT: the PO is a net figure, so the invoice's net
    # figure is the like-for-like number. If the PDF gave no net figure it
    # is estimated from the total (flagged via net_basis).
    net, basis = matching.invoice_net(invoice)
    if net is None:
        net, basis = float(invoice["amount"] or 0), "none"
    variance = round(net - po_amount, 2)
    tolerance = matching.tolerance_for(po_amount)
    within_tolerance = abs(variance) <= tolerance

    with db.engine.begin() as conn:
        conn.execute(db.invoices.update().where(db.invoices.c.id == invoice_id).values(
            po_id=po_id, status="MATCHED", matched_at=db.now_iso(), po_prior_status=prior_status,
        ))

    goods_note = " Goods/service receipt had not been confirmed yet." if prior_status == "ISSUED" else ""
    basis_note = {"read": "net", "derived": "net", "estimated": "net (estimated at 20% VAT)"}.get(basis, "total")
    _set_status(po_id, "MATCHED", actor, "Invoice matched to PO",
                f"{invoice['invoice_number']} - {basis_note} £{net:,.2f} vs PO £{po_amount:,.2f} "
                f"(variance £{variance:,.2f}); invoice total £{invoice['amount']:,.2f}.{goods_note}")

    return {
        "ok": True,
        "variance": variance,
        "net_basis": basis,
        "invoice_net": net,
        "within_tolerance": within_tolerance,
        "goods_not_received": prior_status == "ISSUED",
        "invoice": get_invoice(invoice_id),
        "po": get_po(po_id),
    }


def suggest_matches(invoice_id: int):
    """Ranked purchase-order candidates for one invoice, with confidence
    and the reasons behind each."""
    invoice = get_invoice(invoice_id)
    if not invoice:
        return {"ok": False, "error": "Invoice not found."}
    pos = matchable_pos()
    ranked = matching.rank_candidates(invoice, pos)
    best = matching.best_suggestion(invoice, pos)
    return {"ok": True, "candidates": ranked, "best_po_id": best["po_id"] if best else None}


def auto_match_new_invoices(actor: str = "Auto-match"):
    """Links an incoming invoice to its purchase order by itself - but only
    when it is certain: our PO number is quoted on the invoice, its net
    figure is read from the PDF and is within tolerance, the supplier
    agrees, and no other PO looks as good. Everything less certain is left
    as a one-click suggestion for a person. Each is logged and can be
    undone with Undo match (which stops auto-match retrying it)."""
    if get_settings().get("auto_match_enabled", "1") != "1":
        return {"ok": True, "matched": 0}
    matched = 0
    with db.engine.connect() as conn:
        new_ids = [r[0] for r in conn.execute(
            select(db.invoices.c.id).where(db.invoices.c.status == "NEW", db.invoices.c.no_auto_match == 0)
            .order_by(db.invoices.c.id)
        ).all()]
    for invoice_id in new_ids:
        invoice = get_invoice(invoice_id)
        if not invoice or not invoice["amount"]:
            continue
        pos = matchable_pos(auto=True)
        if not pos:
            break
        best = matching.best_suggestion(invoice, pos)
        if not best or best["confidence"] != "certain":
            continue
        result = match_invoice(invoice_id, best["po_id"], actor)
        if result.get("ok"):
            matched += 1
            _log("INVOICE", invoice_id, invoice["invoice_number"], "Invoice auto-matched to PO", actor,
                 f"{best['po_number']} - " + "; ".join(best["reasons"]) + ". Use Undo match if this is wrong.")
    return {"ok": True, "matched": matched}


def unmatch_invoice(invoice_id: int, actor: str):
    """Reverses a match made before the invoice went to payables: the
    invoice goes back to the inbox and the PO back to awaiting-invoice."""
    invoice = get_invoice(invoice_id)
    if not invoice or invoice["status"] != "MATCHED":
        return {"ok": False, "error": "Only a matched invoice that hasn't gone to payables can be un-matched."}
    po_id = invoice["po_id"]
    with db.engine.begin() as conn:
        conn.execute(db.invoices.update().where(db.invoices.c.id == invoice_id).values(
            po_id=None, status="NEW", matched_at=None, no_auto_match=1,
        ))
    po = get_po(po_id) if po_id else None
    if po and po["status"] == "MATCHED":
        _set_status(po_id, invoice.get("po_prior_status") or "AWAITING_INVOICE", actor, "Invoice match undone",
                    f"{invoice['invoice_number']} un-matched")
    _log("INVOICE", invoice_id, invoice["invoice_number"], "Invoice match undone", actor,
         f"Back in the inbox; was matched to {po['po_number'] if po else 'a PO'}.")
    return {"ok": True, "invoice": get_invoice(invoice_id)}


# Statuses an invoice can still be voided from (anything not yet paid).
_VOIDABLE = ("NEW", "MATCHED", "IN_PAYABLES", "PENDING_PAYMENT_APPROVAL", "PAYMENT_APPROVED", "PAYMENT_REJECTED")


def void_invoice(invoice_id: int, actor: str, reason: str):
    """Takes an invoice out of play - a duplicate, an invoice raised in
    error, one the supplier has credited. The row is kept (so the audit
    trail is complete and Outlook can't re-import the same email) but it
    disappears from every live list and total."""
    reason = (reason or "").strip()
    if not reason:
        return {"ok": False, "error": "Please give a reason for voiding this invoice."}
    invoice = get_invoice(invoice_id)
    if not invoice:
        return {"ok": False, "error": "Invoice not found."}
    if invoice["status"] == "VOIDED":
        return {"ok": False, "error": "This invoice is already voided."}
    if invoice["status"] not in _VOIDABLE:
        return {"ok": False, "error": "A paid invoice can't be voided - the payment has already gone out."}

    # Was a real bill created in Xero for it? Then it needs voiding there too.
    with db.engine.connect() as conn:
        xero_bill = conn.execute(select(db.audit_log.c.id).where(
            db.audit_log.c.entity_type == "INVOICE", db.audit_log.c.entity_id == invoice_id,
            db.audit_log.c.action == "Bill created in Xero payables ledger",
        )).first()

    prior_po = get_po(invoice["po_id"]) if invoice["po_id"] else None
    with db.engine.begin() as conn:
        conn.execute(db.invoices.update().where(db.invoices.c.id == invoice_id).values(
            status="VOIDED", voided_at=db.now_iso(), void_reason=reason, voided_by=actor, po_id=None,
        ))
    cancelled = db.cancel_pending_for("invoice", invoice_id)
    # Release the PO so the right invoice can be matched to it instead.
    if prior_po and prior_po["status"] == "MATCHED":
        _set_status(prior_po["id"], invoice.get("po_prior_status") or "AWAITING_INVOICE", actor,
                    "Invoice voided - PO released",
                    f"{invoice['invoice_number']} voided; the PO is open for an invoice again.")

    detail = reason
    if prior_po:
        detail += f" (was matched to {prior_po['po_number']})"
    if cancelled:
        detail += "; pending approval request withdrawn"
    if xero_bill:
        detail += "; a bill exists in Xero and must be voided there"
    _log("INVOICE", invoice_id, invoice["invoice_number"], "Invoice voided", actor, detail)
    return {"ok": True, "invoice": get_invoice(invoice_id), "xero_bill_warning": bool(xero_bill),
            "po_released": bool(prior_po and prior_po["status"] == "MATCHED")}


def restore_invoice(invoice_id: int, actor: str):
    """Undoes a void: the invoice returns to the inbox as NEW."""
    invoice = get_invoice(invoice_id)
    if not invoice or invoice["status"] != "VOIDED":
        return {"ok": False, "error": "Only a voided invoice can be restored."}
    with db.engine.begin() as conn:
        conn.execute(db.invoices.update().where(db.invoices.c.id == invoice_id).values(
            status="NEW", voided_at=None, void_reason=None, voided_by=None, no_auto_match=1,
        ))
    _log("INVOICE", invoice_id, invoice["invoice_number"], "Invoice restored", actor, "Back in the inbox.")
    return {"ok": True, "invoice": get_invoice(invoice_id)}


_DATE_FIELDS = ("invoice_date", "due_date")
_MONEY_FIELDS = ("amount", "net_amount", "vat_amount")
_CORE_FIELDS = ("invoice_number", "supplier", "invoice_date", "amount", "net_amount", "vat_amount")
_EDITABLE_AT_ANY_LIVE_STATUS = ("due_date", "po_reference")


def update_invoice_details(invoice_id: int, fields: dict, actor: str):
    """Corrects what was read from the PDF. Amounts, supplier, number and
    dates can only change while the invoice is still in the inbox or just
    matched - after that a bill/approval already exists using them. The due
    date and PO reference can be added or changed any time before payment
    (a blank due date means 'not stated', never an assumed one)."""
    invoice = get_invoice(invoice_id)
    if not invoice:
        return {"ok": False, "error": "Invoice not found."}
    if invoice["status"] in ("PAID", "VOIDED"):
        return {"ok": False, "error": f"A {invoice['status'].lower()} invoice can't be edited."}
    editable_core = invoice["status"] in ("NEW", "MATCHED")

    values, changes = {}, []
    for key in _CORE_FIELDS + _EDITABLE_AT_ANY_LIVE_STATUS:
        if key not in fields:
            continue
        raw = fields[key]
        if key in _CORE_FIELDS and not editable_core:
            return {"ok": False, "error": "The amount, supplier, number and invoice date are locked once the invoice is in payables."}
        text = ("" if raw is None else str(raw)).strip()
        if key in _DATE_FIELDS:
            if text:
                try:
                    datetime.strptime(text, "%Y-%m-%d")
                except ValueError:
                    return {"ok": False, "error": f"{key.replace('_', ' ').capitalize()} must be a date like 2026-10-31."}
            elif key == "invoice_date":
                return {"ok": False, "error": "Invoice date can't be blank."}
            val = text or None
        elif key in _MONEY_FIELDS:
            if text == "":
                if key == "amount":
                    return {"ok": False, "error": "The invoice total can't be blank."}
                val = None
            else:
                try:
                    val = round(float(text.replace(",", "").replace("£", "")), 2)
                except ValueError:
                    return {"ok": False, "error": f"{key.replace('_', ' ').capitalize()} must be a number."}
                if val < 0:
                    return {"ok": False, "error": f"{key.replace('_', ' ').capitalize()} can't be negative."}
        else:
            if key in ("invoice_number", "supplier") and not text:
                return {"ok": False, "error": f"{key.replace('_', ' ').capitalize()} can't be blank."}
            val = text or None
        if val != invoice.get(key):
            values[key] = val
            changes.append(f"{key.replace('_', ' ')}: {invoice.get(key) if invoice.get(key) not in (None, '') else '-'} -> {val if val not in (None, '') else '-'}")

    if not values:
        return {"ok": True, "invoice": invoice, "warning": None}

    merged = {**invoice, **values}
    warning = None
    if merged.get("net_amount") is not None and merged.get("vat_amount") is not None:
        if abs(float(merged["net_amount"]) + float(merged["vat_amount"]) - float(merged["amount"])) > 0.02:
            warning = "Net + VAT doesn't equal the invoice total - please double-check the figures."
    with db.engine.begin() as conn:
        conn.execute(db.invoices.update().where(db.invoices.c.id == invoice_id).values(**values))
    _log("INVOICE", invoice_id, merged["invoice_number"], "Invoice details edited", actor, "; ".join(changes))
    return {"ok": True, "invoice": get_invoice(invoice_id), "warning": warning}


def push_to_payables(invoice_id: int, actor: str):
    invoice = get_invoice(invoice_id)
    if not invoice or invoice["status"] != "MATCHED":
        return {"ok": False, "error": "Only a matched invoice can be pushed to the Xero payables ledger."}

    if get_settings().get("xero_connected") == "1":
        contact = _xero_find_or_create_contact(invoice["supplier"])
        if not contact.get("ok"):
            return contact
        po = get_po(invoice["po_id"]) if invoice["po_id"] else None
        bill = {
            "Type": "ACCPAY",
            "Contact": {"ContactID": contact["contact_id"]},
            "Date": invoice["invoice_date"],
            "InvoiceNumber": invoice["invoice_number"],
            "Reference": po["po_number"] if po else "",
            "Status": "AUTHORISED",
            "LineItems": [{
                "Description": f"Invoice {invoice['invoice_number']} from {invoice['supplier']}",
                "Quantity": 1,
                "UnitAmount": invoice["amount"],
            }],
        }
        # Only a due date that is actually on the invoice is sent. Without
        # one, Xero applies the supplier's own payment terms - we never
        # invent a due date of our own.
        if invoice.get("due_date"):
            bill["DueDate"] = invoice["due_date"]
        body = {"Invoices": [bill]}
        try:
            resp = requests.post(f"{xero_client.API_BASE}/Invoices", headers=_xero_headers(), json=body, timeout=30)
        except requests.RequestException as exc:
            return {"ok": False, "error": f"Could not reach Xero to create this bill: {exc}"}
        if resp.status_code not in (200, 201):
            return {"ok": False, "error": f"Xero rejected the bill ({resp.status_code}): {resp.text[:300]}"}
        created = resp.json()["Invoices"][0]
        bill_reference = created.get("InvoiceNumber") or created.get("InvoiceID")
        with db.engine.begin() as conn:
            conn.execute(db.invoices.update().where(db.invoices.c.id == invoice_id).values(status="IN_PAYABLES"))
        _log("INVOICE", invoice_id, invoice["invoice_number"], "Bill created in Xero payables ledger", actor,
             f"Xero bill reference {bill_reference} - £{invoice['amount']:,.2f}")
        return {"ok": True, "invoice": get_invoice(invoice_id), "bill_reference": bill_reference}

    # Not connected to Xero - fall back to the simulation.
    bill_ref = "BILL-" + "".join(random.choices(string.digits, k=5))
    with db.engine.begin() as conn:
        conn.execute(db.invoices.update().where(db.invoices.c.id == invoice_id).values(status="IN_PAYABLES"))
    _log("INVOICE", invoice_id, invoice["invoice_number"], "Bill created in Xero payables ledger (simulated)",
         actor, f"Xero bill reference {bill_ref} - £{invoice['amount']:,.2f}")
    return {"ok": True, "invoice": get_invoice(invoice_id), "bill_reference": bill_ref}


def _invoice_approver(invoice: dict) -> str:
    """Who has to approve this invoice's payment: whoever was assigned when
    it was submitted. Invoices submitted before that was stored fall back to
    the current Invoice Approval Permissions band for their amount."""
    return invoice.get("approver") or assign_invoice_approver(invoice["amount"])


def assign_invoice_approver(amount: float) -> str:
    return get_approver_for_amount(amount, "invoice")["name"] or "Unassigned"


def _notify_invoice_approver_if_needed(invoice: dict):
    """Emails whoever is named for this invoice's value band an
    Approve/Reject link - the same mechanism purchase orders use, just for
    payment approval. Silent if that band has no email set."""
    if not invoice or invoice["status"] != "PENDING_PAYMENT_APPROVAL":
        return
    approver_email = (invoice.get("approver_email") or "").strip()
    if not approver_email:
        return
    ref = invoice["invoice_number"]
    # Two-stage approvals say so in the email, so the second approver knows
    # why it has reached them and who has already signed it off.
    two_stage = bool(invoice.get("needs_second_approval"))
    stage = int(invoice.get("approval_stage") or 1)
    ref_label = f"{ref} (approval {stage} of 2)" if two_stage else ref
    stage_note = ""
    if two_stage:
        stage_note = f" - {invoice.get('second_approval_reason') or 'exceeds its purchase order'}; needs two approvals."
        if stage == 2 and invoice.get("first_approver"):
            stage_note += f" First approved by {invoice['first_approver']}."
    token = db.create_pending(
        po_id=invoice["id"], po_number=ref_label, supplier=invoice["supplier"],
        description=f"Invoice {ref} from {invoice['supplier']}" + (
            f" (received {invoice['email_subject']})" if invoice.get("email_subject") else "") + stage_note,
        net_amount=invoice["amount"], approver_name=invoice.get("approver") or "",
        approver_email=approver_email, kind="invoice",
    )
    try:
        emailer.send_approval_request(
            to_email=approver_email, po_number=ref_label, supplier=invoice["supplier"],
            description=f"Invoice {ref}{stage_note}", net_amount=float(invoice["amount"]),
            approve_url=f"{PUBLIC_BASE_URL}/approve/{token}", reject_url=f"{PUBLIC_BASE_URL}/reject/{token}",
            kind="invoice",
        )
        _log("INVOICE", invoice["id"], ref, "Payment approval email sent", "System",
             f"Emailed {invoice.get('approver')} at {approver_email} for payment approval.")
    except Exception as exc:  # noqa: BLE001
        _log("INVOICE", invoice["id"], ref, "Payment approval email failed", "System", str(exc))


def _second_approval_check(invoice: dict):
    """Does this invoice need a second approver? Yes when it is more than
    the purchase order it is matched to (before VAT, allowing the usual
    matching tolerance so pennies of rounding don't trigger it)."""
    po = get_po(invoice["po_id"]) if invoice.get("po_id") else None
    if not po:
        return None
    po_amount = matching.po_value(po)
    net, basis = matching.invoice_net(invoice)
    if net is None:
        net = float(invoice["amount"] or 0)
    excess = round(net - po_amount, 2)
    if excess <= matching.tolerance_for(po_amount):
        return None
    est = " (net estimated from the total)" if basis == "estimated" else ""
    return {"excess": excess, "po_number": po["po_number"], "po_amount": po_amount,
            "reason": f"invoice is £{excess:,.2f} over {po['po_number'] or 'its purchase order'}{est}"}


def _same_person(a: dict, b: dict) -> bool:
    if (a.get("name") or "").strip().lower() == (b.get("name") or "").strip().lower() and (a.get("name") or "").strip():
        return True
    ea, eb = (a.get("email") or "").strip().lower(), (b.get("email") or "").strip().lower()
    return bool(ea) and ea == eb


def submit_for_payment_approval(invoice_id: int, actor: str):
    invoice = get_invoice(invoice_id)
    if not invoice or invoice["status"] != "IN_PAYABLES":
        return {"ok": False, "error": "Only a bill in the payables ledger can be submitted for payment approval."}
    band = get_approver_for_amount(invoice["amount"], "invoice")
    approver = band["name"] or "Unassigned"

    second = _second_approval_check(invoice)
    second_band = None
    if second:
        second_band = get_approver_for_amount(invoice["amount"], "invoice2")
        if not second_band["name"]:
            return {"ok": False, "error": (
                f"This invoice is {second['reason']}, so it needs a second approver - but nobody is named for "
                f"{second_band['band'] or 'this value'} under 'Second approver' in Settings > Approvals. "
                "Add them there, then submit again.")}
        if _same_person(band, second_band):
            return {"ok": False, "error": (
                f"This invoice needs a second approver, but {second_band['name']} is named for both approvals at "
                f"{second_band['band']}. The second approver must be a different person - change it in Settings > Approvals.")}

    values = dict(status="PENDING_PAYMENT_APPROVAL", approver=approver, approver_email=band["email"] or None,
                  needs_second_approval=1 if second else 0, approval_stage=1 if second else 0,
                  first_approver=None, first_approved_at=None,
                  second_approver=second_band["name"] if second else None,
                  second_approver_email=(second_band["email"] or None) if second else None,
                  second_approval_reason=second["reason"] if second else None)
    with db.engine.begin() as conn:
        conn.execute(db.invoices.update().where(db.invoices.c.id == invoice_id).values(**values))
    detail = f"Routed to {approver} - £{invoice['amount']:,.2f}"
    if second:
        detail += f". Needs two approvals: {second['reason']}; {second_band['name']} will approve second."
    _log("INVOICE", invoice_id, invoice["invoice_number"], "Submitted for payment approval", actor, detail)
    updated = get_invoice(invoice_id)
    _notify_invoice_approver_if_needed(updated)
    return {"ok": True, "invoice": updated, "approver": approver,
            "needs_second_approval": bool(second), "second_approver": second_band["name"] if second else None}


def approve_payment(invoice_id: int, actor: str):
    invoice = get_invoice(invoice_id)
    if not invoice or invoice["status"] != "PENDING_PAYMENT_APPROVAL":
        return {"ok": False, "error": "This invoice is not awaiting payment approval."}
    approver = _invoice_approver(invoice)
    if actor != approver:
        return {"ok": False, "error": f"Only {approver} is authorised to approve this payment."}

    two_stage = bool(invoice.get("needs_second_approval"))
    stage = int(invoice.get("approval_stage") or 0)

    if two_stage and stage <= 1:
        # First of two approvals: record it and pass the invoice to the
        # second approver. It stays "awaiting payment approval".
        second_name = (invoice.get("second_approver") or "").strip()
        if not second_name:
            return {"ok": False, "error": "No second approver is set for this invoice - add one in Settings > Approvals."}
        if second_name.lower() == actor.strip().lower():
            return {"ok": False, "error": "The second approver must be a different person from the first."}
        with db.engine.begin() as conn:
            conn.execute(db.invoices.update().where(db.invoices.c.id == invoice_id).values(
                approval_stage=2, first_approver=actor, first_approved_at=db.now_iso(),
                approver=second_name, approver_email=invoice.get("second_approver_email") or None,
            ))
        _log("INVOICE", invoice_id, invoice["invoice_number"], "Payment approved - first of two approvals", actor,
             f"£{invoice['amount']:,.2f}; passed to {second_name} for the second approval.")
        updated = get_invoice(invoice_id)
        _notify_invoice_approver_if_needed(updated)
        return {"ok": True, "invoice": updated, "awaiting_second_approval": True, "second_approver": second_name}

    if two_stage and stage == 2 and (invoice.get("first_approver") or "").strip().lower() == actor.strip().lower():
        return {"ok": False, "error": "You gave the first approval - a different person must give the second."}

    with db.engine.begin() as conn:
        conn.execute(db.invoices.update().where(db.invoices.c.id == invoice_id).values(status="PAYMENT_APPROVED"))
    if two_stage:
        _log("INVOICE", invoice_id, invoice["invoice_number"], "Payment approved - second of two approvals", actor,
             f"£{invoice['amount']:,.2f}; first approved by {invoice.get('first_approver')}.")
    else:
        _log("INVOICE", invoice_id, invoice["invoice_number"], "Payment approved", actor, f"£{invoice['amount']:,.2f}")
    return {"ok": True, "invoice": get_invoice(invoice_id)}


def reject_payment(invoice_id: int, actor: str, reason: str):
    invoice = get_invoice(invoice_id)
    if not invoice or invoice["status"] != "PENDING_PAYMENT_APPROVAL":
        return {"ok": False, "error": "This invoice is not awaiting payment approval."}
    approver = _invoice_approver(invoice)
    if actor != approver:
        return {"ok": False, "error": f"Only {approver} is authorised to action this payment."}
    with db.engine.begin() as conn:
        conn.execute(db.invoices.update().where(db.invoices.c.id == invoice_id).values(status="PAYMENT_REJECTED"))
    stage_note = ""
    if invoice.get("needs_second_approval"):
        stage_note = f" (at approval {int(invoice.get('approval_stage') or 1)} of 2)"
    _log("INVOICE", invoice_id, invoice["invoice_number"], f"Payment rejected{stage_note}", actor, reason)
    return {"ok": True, "invoice": get_invoice(invoice_id)}


def run_bank_payment(invoice_id: int, actor: str):
    invoice = get_invoice(invoice_id)
    if not invoice or invoice["status"] != "PAYMENT_APPROVED":
        return {"ok": False, "error": "Only an approved payment can be released to the bank."}
    bank_ref = "BACS-" + "".join(random.choices(string.digits, k=8))
    with db.engine.begin() as conn:
        conn.execute(db.invoices.update().where(db.invoices.c.id == invoice_id).values(status="PAID"))
        conn.execute(db.payments.insert().values(
            invoice_id=invoice_id, po_id=invoice["po_id"], amount=invoice["amount"],
            bank_reference=bank_ref, paid_by=actor, paid_at=db.now_iso(),
        ))
    if invoice["po_id"]:
        _set_status(invoice["po_id"], "PAID", actor, "Purchase order paid", f"Bank reference {bank_ref}")
    _log("INVOICE", invoice_id, invoice["invoice_number"], "Bank payment released (simulated)", actor,
         f"Reference {bank_ref} - £{invoice['amount']:,.2f}")
    return {"ok": True, "invoice": get_invoice(invoice_id), "bank_reference": bank_ref}


def list_payments():
    with db.engine.connect() as conn:
        rows = conn.execute(
            select(
                db.payments, db.invoices.c.invoice_number, db.invoices.c.supplier,
            ).select_from(
                db.payments.join(db.invoices, db.invoices.c.id == db.payments.c.invoice_id)
            ).order_by(db.payments.c.id.desc())
        ).mappings().all()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

def dashboard_summary():
    with db.engine.connect() as conn:
        pos = [dict(r) for r in conn.execute(select(db.purchase_orders)).mappings().all()]
        invoices = [dict(r) for r in conn.execute(
            select(*_INVOICE_COLS)).mappings().all()]
        payments = [dict(r) for r in conn.execute(select(db.payments)).mappings().all()]

    # A multi-supplier request that has been issued is represented by its
    # per-supplier purchase orders; counting it too would double the value.
    pos = [p for p in pos if p["status"] != "SPLIT"]
    open_pos = [p for p in pos if p["status"] not in ("PAID", "REJECTED")]
    pending_po_approval = [p for p in pos if p["status"] in ("PENDING_APPROVAL", "PENDING_COST_APPROVAL")]
    awaiting_match = [i for i in invoices if i["status"] == "NEW"]
    pending_payment_approval = [i for i in invoices if i["status"] == "PENDING_PAYMENT_APPROVAL"]
    awaiting_bank_run = [i for i in invoices if i["status"] == "PAYMENT_APPROVED"]
    total_committed = sum(p["net_amount"] for p in pos if p["status"] not in ("REJECTED",))
    total_paid = sum(i["amount"] for i in invoices if i["status"] == "PAID")

    cutoff = (datetime.now() - timedelta(days=7)).isoformat()
    committed_last_7d = sum(
        p["net_amount"] for p in pos if p["status"] != "REJECTED" and p["created_at"] >= cutoff
    )
    paid_last_7d = sum(pmt["amount"] for pmt in payments if pmt["paid_at"] >= cutoff)

    today = _today()
    due = reporting.due_summary(invoices, today)
    stale = waiting_approvals(only_stale=True)
    suggested = 0
    if awaiting_match:
        awaiting_pos = [p for p in pos if p["status"] == "AWAITING_INVOICE"]
        suggested = sum(1 for i in awaiting_match if i["amount"] and matching.best_suggestion(i, awaiting_pos))

    return {
        "due_summary": due,
        "stale_approvals": stale,
        "suggested_matches": suggested,
        "open_pos": len(open_pos),
        "pending_po_approval": len(pending_po_approval),
        "awaiting_match": len(awaiting_match),
        "pending_payment_approval": len(pending_payment_approval),
        "awaiting_bank_run": len(awaiting_bank_run),
        "total_committed": round(total_committed, 2),
        "total_paid": round(total_paid, 2),
        "committed_last_7d": round(committed_last_7d, 2),
        "paid_last_7d": round(paid_last_7d, 2),
        "po_count": len(pos),
    }


# ---------------------------------------------------------------------------
# Approval reminders
#
# A PO or invoice payment that has sat unanswered gets a nudge email to the
# same approver (re-using the same Approve/Reject links): after N working
# days, then every M working days, up to a maximum - all set in Settings.
# ---------------------------------------------------------------------------

def _reminder_settings():
    s = get_settings()

    def num(key, default):
        try:
            return max(int(float(s.get(key) or default)), 0)
        except ValueError:
            return default
    return {
        "enabled": s.get("reminders_enabled", "1") == "1",
        "after": max(num("reminder_after_days", 2), 1),
        "interval": max(num("reminder_interval_days", 2), 1),
        "max": num("reminder_max", 3),
    }


def _live_pending_requests():
    """Still-unanswered approval requests whose PO/invoice really is still
    waiting (a request answered inside the app is skipped), newest request
    per item only. Each row is annotated with its entity."""
    seen, out = set(), []
    for row in db.pending_requests():
        kind = row.get("kind") or "po"
        key = (kind, str(row["po_id"]))
        if key in seen:
            continue
        seen.add(key)
        try:
            entity_id = int(row["po_id"])
        except (TypeError, ValueError):
            continue
        if kind == "invoice":
            entity = get_invoice(entity_id)
            if not entity or entity["status"] != "PENDING_PAYMENT_APPROVAL":
                continue
        else:
            entity = get_po(entity_id)
            if not entity or entity["status"] not in ("PENDING_APPROVAL", "PENDING_COST_APPROVAL"):
                continue
        out.append({**row, "kind": kind, "entity_id": entity_id})
    return out


def waiting_approvals(only_stale: bool = False):
    """Approval requests still waiting, with how long (in working days) and
    how many reminders have gone. only_stale limits it to those that have
    waited at least the first-reminder threshold."""
    cfg = _reminder_settings()
    today = db.now().date()
    out = []
    for row in _live_pending_requests():
        waited = reminders.working_days_between(row["created_at"].date(), today)
        if only_stale and waited < cfg["after"]:
            continue
        out.append({
            "kind": row["kind"], "entity_id": row["entity_id"], "reference": row["po_number"] or f"PO request #{row['entity_id']}",
            "supplier": row["supplier"], "amount": float(row["net_amount"]), "approver": row["approver_name"],
            "working_days_waiting": waited, "reminders_sent": row.get("reminder_count") or 0,
        })
    out.sort(key=lambda r: -r["working_days_waiting"])
    return out[:20]


def _send_reminder(row, cfg_waited: int):
    token = row["token"]
    count = (row.get("reminder_count") or 0) + 1
    base = PUBLIC_BASE_URL
    try:
        emailer.send_approval_request(
            to_email=row["approver_email"], po_number=row["po_number"], supplier=row["supplier"],
            description=row["description"], net_amount=float(row["net_amount"]),
            approve_url=f"{base}/approve/{token}", reject_url=f"{base}/reject/{token}",
            kind=row["kind"], reminder_number=count, days_waiting=cfg_waited,
        )
    except Exception as exc:  # noqa: BLE001
        _log("INVOICE" if row["kind"] == "invoice" else "PO", row["entity_id"], row["po_number"] or "",
             "Approval reminder failed", "System", str(exc))
        return False
    db.record_reminder(token)
    _log("INVOICE" if row["kind"] == "invoice" else "PO", row["entity_id"], row["po_number"] or f"PO request #{row['entity_id']}",
         f"Approval reminder {count} sent", "System",
         f"Reminded {row['approver_name']} at {row['approver_email']} - waiting {cfg_waited} working days.")
    return True


def run_reminders():
    """Sends whatever reminders are due today. Safe to call often - each
    request only ever gets one when its turn comes."""
    cfg = _reminder_settings()
    if not cfg["enabled"] or cfg["max"] <= 0:
        return {"ok": True, "sent": 0}
    now = db.now()
    uk_hour = int(db.now_iso()[11:13])
    if uk_hour < 8 or uk_hour >= 18:
        return {"ok": True, "sent": 0}  # not out of hours
    sent = 0
    for row in _live_pending_requests():
        if not (row.get("approver_email") or "").strip():
            continue
        if reminders.reminder_due(row["created_at"], row.get("last_reminded_at"), row.get("reminder_count") or 0,
                                  now.date(), cfg["after"], cfg["interval"], cfg["max"]):
            waited = reminders.working_days_between(row["created_at"].date(), now.date())
            if _send_reminder(row, waited):
                sent += 1
    return {"ok": True, "sent": sent}


def remind_now(kind: str, entity_id: int, actor: str):
    """Manual nudge from the app - sends the reminder straight away
    (it counts towards the maximum)."""
    for row in _live_pending_requests():
        if row["kind"] == (kind or "po") and row["entity_id"] == int(entity_id):
            if not (row.get("approver_email") or "").strip():
                return {"ok": False, "error": "No email address is set for this approver."}
            waited = reminders.working_days_between(row["created_at"].date(), db.now().date())
            if _send_reminder(row, waited):
                return {"ok": True}
            return {"ok": False, "error": "The reminder email could not be sent - see the audit trail."}
    return {"ok": False, "error": "Nothing is waiting for approval on that item."}


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

def _report_data():
    with db.engine.connect() as conn:
        pos = [dict(r) for r in conn.execute(select(db.purchase_orders)).mappings().all()]
        invs = [dict(r) for r in conn.execute(select(*_INVOICE_COLS)).mappings().all()]
        pays = [dict(r) for r in conn.execute(select(db.payments)).mappings().all()]
    return pos, invs, pays


def _supplier_pos(pos):
    """For supplier figures: a multi-supplier request is represented by its
    per-supplier purchase orders, never by the umbrella request."""
    return [p for p in pos if not p.get("is_split")]


def report_suppliers():
    pos, invs, pays = _report_data()
    pos = _supplier_pos(pos)
    return {"ok": True, "suppliers": reporting.supplier_report(pos, invs, pays, _today())}


def report_supplier_detail(key: str):
    pos, invs, pays = _report_data()
    pos = _supplier_pos(pos)
    return {"ok": True, **reporting.supplier_detail(key, pos, invs, pays, _today())}


def _requests_only(pos):
    """For 'raised' figures: what people asked for. The per-supplier orders
    made when a multi-supplier request is issued aren't new requests."""
    return [p for p in pos if not p.get("parent_po_id")]


def report_weekly(week: str = ""):
    """Monday-to-Sunday summary of purchase orders and invoices for the week
    containing the given date (default: the last complete week)."""
    week = (week or "").strip()
    if not week:
        today = _today()
        week = (today - timedelta(days=today.weekday() + 7)).isoformat()
    pos, invs, pays = _report_data()
    try:
        start = reporting.week_bounds(week)[0]
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    end = start + timedelta(days=6)
    with db.engine.connect() as conn:
        audit = [dict(r) for r in conn.execute(
            select(db.audit_log).where(db.audit_log.c.timestamp >= start.isoformat(),
                                       db.audit_log.c.timestamp < (end + timedelta(days=1)).isoformat())
        ).mappings().all()]
    pack = reporting.weekly_summary(week, pos, invs, pays, audit, DEPARTMENTS)
    return {"ok": True, **pack}


def report_month_end(month: str = ""):
    month = (month or "").strip() or db.now_iso()[:7]
    pos, invs, pays = _report_data()
    pos = _requests_only(pos)
    try:
        pack = reporting.month_end_pack(month, pos, invs, pays)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, **pack}


# ---------------------------------------------------------------------------
# Background sync heartbeat - called by every connected desktop app every
# ~60-90 seconds (see ui/app.js). Paces itself using the last-run
# settings so it keeps running exactly once per interval no matter how
# many desktop apps are open and polling at once.
# ---------------------------------------------------------------------------

def maybe_run_background_syncs():
    s = get_settings()
    now_ts = time.time()
    results = {}

    if s.get("xero_connected") == "1" and now_ts - float(s.get("last_xero_sync_at") or 0) >= 60:
        update_settings({"last_xero_sync_at": str(now_ts)})
        results["xero"] = sync_pos_from_xero()

    if s.get("outlook_connected") == "1" and now_ts - float(s.get("last_outlook_sync_at") or 0) >= 60:
        update_settings({"last_outlook_sync_at": str(now_ts)})
        results["outlook"] = sync_invoices_from_outlook()

    if now_ts - float(s.get("last_approval_sync_at") or 0) >= 30:
        update_settings({"last_approval_sync_at": str(now_ts)})
        results["approval"] = sync_approval_decisions()
        results["auto_match"] = auto_match_new_invoices()

    # Approval reminders: checked every 30 minutes (they only ever go out
    # on working days, 08:00-18:00 UK time).
    if now_ts - float(s.get("last_reminder_run_at") or 0) >= 1800:
        update_settings({"last_reminder_run_at": str(now_ts)})
        results["reminders"] = run_reminders()

    return {"ok": True, "results": results}
