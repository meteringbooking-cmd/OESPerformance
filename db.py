"""
db.py
-----
Storage for the shared Purchase-to-Pay service. Uses SQLAlchemy Core so
the exact same code runs against Supabase's Postgres in production and
against a local SQLite file when testing this service on its own - only
the connection string (DATABASE_URL) changes.

This service now holds everything: every purchase order, invoice,
payment and audit entry for the whole office (not just pending email
approvals, which is all it originally did) - see workflow.py for why and
the full port of the business logic.
"""

import datetime
import os
import uuid
from zoneinfo import ZoneInfo

from sqlalchemy import (
    Column, DateTime, Integer, LargeBinary, MetaData, Numeric, String,
    Table, create_engine, inspect, select, text,
)

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///local_dev.db")

# Supabase (and most Postgres hosts) hand out a "postgres://" URL, but
# SQLAlchemy's modern driver name is "postgresql://" - swap it so both
# forms work without anyone needing to notice or edit it.
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# Name the driver explicitly. A bare "postgresql://" lets SQLAlchemy pick
# its own default, and newer SQLAlchemy releases default to psycopg (v3)
# rather than psycopg2 - which isn't installed here, so a fresh Render
# build crashed with "No module named 'psycopg'". requirements.txt ships
# psycopg2-binary, so say so.
if DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg2://", 1)

_connect_args = {}
if DATABASE_URL.startswith("postgresql"):
    # Without a timeout, a database that's slow or unreachable makes every
    # connection attempt hang, so the service never finishes starting and
    # Render just loops on its "waking up" screen with nothing in the logs
    # to say why. 10s turns that into a fast, visible error.
    _connect_args = {"connect_timeout": 10}

engine = create_engine(DATABASE_URL, pool_pre_ping=True, connect_args=_connect_args)
metadata = MetaData()

_UK = ZoneInfo("Europe/London")

# -- the original email-approval table (unchanged) -------------------------

pending_approvals = Table(
    "pending_approvals", metadata,
    Column("token", String, primary_key=True),
    Column("po_id", String, nullable=False),
    Column("po_number", String, nullable=False),
    Column("supplier", String, nullable=False),
    Column("description", String, nullable=False),
    Column("net_amount", Numeric, nullable=False),
    Column("approver_name", String, nullable=False),
    Column("approver_email", String, nullable=False),
    Column("decision", String, nullable=False, server_default=text("'pending'")),
    # What this approval is for: "po" (a purchase order - po_id/po_number
    # hold the PO) or "invoice" (a payment approval - po_id holds the
    # invoice's id and po_number its invoice number; the columns keep
    # their old names so existing rows and links carry on working).
    Column("kind", String, nullable=False, server_default=text("'po'")),
    Column("reject_reason", String, nullable=True),
    Column("created_at", DateTime, nullable=False),
    Column("decided_at", DateTime, nullable=True),
    Column("delivered_at", DateTime, nullable=True),
    # Chasers: how many reminder emails have gone out, and when the last did.
    Column("reminder_count", Integer, nullable=False, server_default=text("0")),
    Column("last_reminded_at", DateTime, nullable=True),
)

# -- everything else: the full Purchase-to-Pay dataset ---------------------
# asdecimal=False on every money column so these come back as plain
# Python floats - Flask's jsonify can't serialise decimal.Decimal.

purchase_orders = Table(
    "purchase_orders", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("po_number", String, nullable=True),
    Column("requester", String, nullable=False),
    Column("supplier", String, nullable=False),
    Column("description", String, nullable=False),
    Column("net_amount", Numeric(asdecimal=False), nullable=False),
    Column("actual_amount", Numeric(asdecimal=False), nullable=True),
    Column("approver", String, nullable=False),
    # The approver's email, captured at the moment they were assigned
    # (from whichever Approval Permissions band matched at the time) -
    # kept alongside the name so the approval-request email always goes
    # to the right inbox even if Settings is later changed to name
    # someone else for that value band. Nullable because rows created
    # before this column existed, and any band left unconfigured, won't
    # have one.
    Column("approver_email", String, nullable=True),
    Column("status", String, nullable=False),
    Column("reject_reason", String, nullable=True),
    Column("created_at", String, nullable=False),
    Column("updated_at", String, nullable=False),
    Column("xero_po_id", String, nullable=True),
    Column("source", String, nullable=False, server_default=text("'app'")),
    # Which part of the business the purchase order is for (Solar, Metering,
    # Training, Other). Blank - e.g. POs created straight in Xero - counts as Other.
    Column("department", String, nullable=True),
    # A Solar request with several suppliers is approved as ONE request (the
    # "parent", is_split = 1) and, when issued, becomes one real purchase
    # order per supplier (the "children", each pointing back with parent_po_id).
    Column("parent_po_id", Integer, nullable=True),
    Column("is_split", Integer, nullable=False, server_default=text("0")),
    # Set when the request was raised from another system, e.g. "pingu:J-4B31C51C"
    # for a solar job in Pingu - stops the same job's POs being raised twice.
    Column("external_ref", String, nullable=True),
)

# The suppliers inside a multi-supplier request. child_po_id fills in as each
# one is issued, so a retry after a Xero hiccup only creates the missing ones.
po_lines = Table(
    "po_lines", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("po_id", Integer, nullable=False),
    Column("supplier", String, nullable=False),
    Column("description", String, nullable=False),
    Column("net_amount", Numeric(asdecimal=False), nullable=False),
    Column("child_po_id", Integer, nullable=True),
)

advice_notes = Table(
    "advice_notes", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("po_id", Integer, nullable=False),
    Column("reference", String, nullable=False),
    Column("logged_by", String, nullable=False),
    Column("logged_at", String, nullable=False),
    Column("notes", String, nullable=True),
)

invoices = Table(
    "invoices", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("po_id", Integer, nullable=True),
    Column("supplier", String, nullable=False),
    Column("invoice_number", String, nullable=False),
    Column("invoice_date", String, nullable=False),
    Column("amount", Numeric(asdecimal=False), nullable=False),
    Column("attachment_name", String, nullable=True),
    # PDF bytes stored directly in the database, not on disk - Render's
    # free-tier filesystem is wiped on every redeploy, which would
    # silently lose any attachment saved as a file.
    Column("attachment_data", LargeBinary, nullable=True),
    Column("email_subject", String, nullable=True),
    Column("email_sender", String, nullable=True),
    Column("received_at", String, nullable=False),
    Column("status", String, nullable=False),
    Column("matched_at", String, nullable=True),
    Column("source", String, nullable=False, server_default=text("'simulated'")),
    Column("extraction_note", String, nullable=True),
    # Who has to approve payment of this invoice, fixed at the moment it is
    # submitted for payment approval (from the Invoice Approval Permissions
    # bands in Settings) - so the emailed approval link and the in-app
    # Approve button always agree, even if Settings changes afterwards.
    Column("approver", String, nullable=True),
    Column("approver_email", String, nullable=True),
    # Read from the PDF where it is shown: net (before VAT), VAT, the due
    # date (only ever a date actually printed on the invoice - never an
    # assumed payment term) and any purchase-order reference quoted on it.
    Column("net_amount", Numeric(asdecimal=False), nullable=True),
    Column("vat_amount", Numeric(asdecimal=False), nullable=True),
    Column("due_date", String, nullable=True),
    Column("po_reference", String, nullable=True),
    # Voiding keeps the row (so the audit trail and the Outlook de-dupe
    # still know about it) but takes it out of every live view and total.
    Column("voided_at", String, nullable=True),
    Column("void_reason", String, nullable=True),
    Column("voided_by", String, nullable=True),
    # Set when a person un-matches an invoice, so auto-match leaves it alone.
    Column("no_auto_match", Integer, nullable=False, server_default=text("0")),
    # PO status at the moment of matching, so un-matching / voiding puts the PO back exactly where it was.
    Column("po_prior_status", String, nullable=True),
    # Two-stage payment approval (only when the invoice exceeds its PO).
    Column("needs_second_approval", Integer, nullable=False, server_default=text("0")),
    Column("approval_stage", Integer, nullable=False, server_default=text("0")),
    Column("first_approver", String, nullable=True),
    Column("first_approved_at", String, nullable=True),
    Column("second_approver", String, nullable=True),
    Column("second_approver_email", String, nullable=True),
    Column("second_approval_reason", String, nullable=True),
)

payments = Table(
    "payments", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("invoice_id", Integer, nullable=False),
    Column("po_id", Integer, nullable=True),
    Column("amount", Numeric(asdecimal=False), nullable=False),
    Column("bank_reference", String, nullable=False),
    Column("paid_by", String, nullable=False),
    Column("paid_at", String, nullable=False),
)

audit_log = Table(
    "audit_log", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("entity_type", String, nullable=False),
    Column("entity_id", Integer, nullable=True),
    Column("reference", String, nullable=True),
    Column("action", String, nullable=False),
    Column("actor", String, nullable=False),
    Column("detail", String, nullable=True),
    Column("timestamp", String, nullable=False),
)

settings = Table(
    "settings", metadata,
    Column("key", String, primary_key=True),
    Column("value", String, nullable=True),
)

DEFAULT_SETTINGS = {
    # Superseded by the four Approval Permissions bands below, but left
    # here (and never deleted from an existing database) so nothing
    # breaks for anyone who upgrades - init_db() reads whatever these
    # already say, the one time it seeds the new band settings, so an
    # existing setup keeps routing to the same people it always did
    # until someone edits the new Approval Permissions table.
    "approval_threshold": "1000",
    "low_approver_name": "Barry Dixon",
    "low_approver_email": "barry.dixon@next-gen.uk",
    "high_approver_name": "David Stroud",
    "high_approver_email": "david.stroud@next-gen.uk",
    "advice_note_supplier": "CEF",
    "organisation_name": "Next-Gen",
    "po_number_prefix": "PO",
    "xero_connected": "0",
    "xero_client_id": "",
    "xero_access_token": "",
    "xero_refresh_token": "",
    "xero_token_expires_at": "0",
    "xero_tenant_id": "",
    "xero_tenant_name": "",
    "xero_po_sync_cursor": "",
    "outlook_connected": "0",
    "outlook_client_id": "",
    "outlook_tenant": "",
    "outlook_access_token": "",
    "outlook_refresh_token": "",
    "outlook_token_expires_at": "0",
    "outlook_account_email": "",
    "outlook_folder_name": "Invoices",
    "outlook_folder_id": "",
    "outlook_sync_cursor": "",
    "anthropic_api_key": "",
    # Background-sync pacing (see workflow.maybe_run_background_syncs) -
    # a plain unix timestamp as a string, shared across every connected
    # desktop app so they never all trigger the same sync at once.
    "last_xero_sync_at": "0",
    "last_outlook_sync_at": "0",
    "last_approval_sync_at": "0",
    "last_reminder_run_at": "0",
    # Approval reminders: first nudge after N working days waiting, then
    # every M working days, stopping after a maximum number of reminders.
    "auto_match_enabled": "1",
    "reminders_enabled": "1",
    "reminder_after_days": "2",
    "reminder_interval_days": "2",
    "reminder_max": "3",
}


# (table, column, column definition) - columns added after a table first
# existed. Additive only, checked against the live database on every start.
_COLUMN_MIGRATIONS = [
    ("purchase_orders", "approver_email", "VARCHAR"),
    ("invoices", "approver", "VARCHAR"),
    ("invoices", "approver_email", "VARCHAR"),
    ("pending_approvals", "kind", "VARCHAR NOT NULL DEFAULT 'po'"),
    ("pending_approvals", "reminder_count", "INTEGER NOT NULL DEFAULT 0"),
    ("pending_approvals", "last_reminded_at", "TIMESTAMP"),
    ("invoices", "net_amount", "NUMERIC"),
    ("invoices", "vat_amount", "NUMERIC"),
    ("invoices", "due_date", "VARCHAR"),
    ("invoices", "po_reference", "VARCHAR"),
    ("invoices", "voided_at", "VARCHAR"),
    ("invoices", "void_reason", "VARCHAR"),
    ("invoices", "voided_by", "VARCHAR"),
    ("invoices", "no_auto_match", "INTEGER NOT NULL DEFAULT 0"),
    ("invoices", "po_prior_status", "VARCHAR"),
    ("invoices", "needs_second_approval", "INTEGER NOT NULL DEFAULT 0"),
    ("invoices", "approval_stage", "INTEGER NOT NULL DEFAULT 0"),
    ("invoices", "first_approver", "VARCHAR"),
    ("invoices", "first_approved_at", "VARCHAR"),
    ("invoices", "second_approver", "VARCHAR"),
    ("invoices", "second_approver_email", "VARCHAR"),
    ("invoices", "second_approval_reason", "VARCHAR"),
    ("purchase_orders", "department", "VARCHAR"),
    ("purchase_orders", "parent_po_id", "INTEGER"),
    ("purchase_orders", "is_split", "INTEGER NOT NULL DEFAULT 0"),
    ("purchase_orders", "external_ref", "VARCHAR"),
]


def _apply_migrations():
    """Ad-hoc, additive-only schema changes for databases created before
    a column existed. No migration framework in use here, so this is
    deliberately narrow: check each column is missing, then add it -
    safe to run on every startup.

    Never allowed to stop the service starting: if one fails (or can't get
    the table lock quickly, e.g. because another connection is holding
    it) the error is printed to the service logs and retried on the next
    start, rather than leaving the service stuck booting forever."""
    for table, column, ddl in _COLUMN_MIGRATIONS:
        try:
            inspector = inspect(engine)
            if table not in inspector.get_table_names():
                continue
            existing_cols = {c["name"] for c in inspector.get_columns(table)}
            if column in existing_cols:
                continue
            with engine.begin() as conn:
                if engine.dialect.name == "postgresql":
                    conn.execute(text("SET LOCAL lock_timeout = '5s'"))
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
            print(f"[db] Added {table}.{column} column.", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[db] WARNING: could not add {table}.{column}, will retry on next start: {exc!r}", flush=True)


def init_db():
    metadata.create_all(engine)
    _apply_migrations()
    with engine.begin() as conn:
        existing_rows = {r.key: r.value for r in conn.execute(select(settings))}
        for key, value in DEFAULT_SETTINGS.items():
            if key not in existing_rows:
                conn.execute(settings.insert().values(key=key, value=value))

        # Seed the four Approval Permissions bands from whatever the old
        # two-tier low/high approver settings already say (falling back
        # to this file's hardcoded defaults for a brand new database) -
        # this only ever runs once per band, the first time each key is
        # missing, so it never overwrites a value someone has since set
        # via the Approval Permissions screen.
        low_name = existing_rows.get("low_approver_name", DEFAULT_SETTINGS["low_approver_name"])
        low_email = existing_rows.get("low_approver_email", DEFAULT_SETTINGS["low_approver_email"])
        high_name = existing_rows.get("high_approver_name", DEFAULT_SETTINGS["high_approver_name"])
        high_email = existing_rows.get("high_approver_email", DEFAULT_SETTINGS["high_approver_email"])
        band_defaults = {
            "approver_band1_name": low_name, "approver_band1_email": low_email,
            "approver_band2_name": low_name, "approver_band2_email": low_email,
            "approver_band3_name": high_name, "approver_band3_email": high_email,
            "approver_band4_name": high_name, "approver_band4_email": high_email,
        }
        for key, value in band_defaults.items():
            if key not in existing_rows:
                conn.execute(settings.insert().values(key=key, value=value))

        # Invoice (payment) approval has its own four bands, so purchase
        # orders and invoices can go to different people. Until someone
        # sets them, each starts out the same as the matching purchase
        # order band - i.e. exactly who approved payments before.
        for n in range(1, 5):
            for field in ("name", "email"):
                key = f"invoice_band{n}_{field}"
                if key not in existing_rows:
                    po_key = f"approver_band{n}_{field}"
                    value = existing_rows.get(po_key, band_defaults[po_key])
                    conn.execute(settings.insert().values(key=key, value=value))

        # Second approver for invoices that exceed their purchase order.
        # Deliberately blank: nobody is named until you choose them.
        for n in range(1, 5):
            for field in ("name", "email"):
                key = f"invoice2_band{n}_{field}"
                if key not in existing_rows:
                    conn.execute(settings.insert().values(key=key, value=""))


def now():
    return datetime.datetime.utcnow()


def now_iso():
    # UK wall-clock time for the audit trail, regardless of which
    # timezone Render's actual server machine runs in.
    return datetime.datetime.now(_UK).isoformat(timespec="seconds")


def new_token():
    return uuid.uuid4().hex


def create_pending(po_id, po_number, supplier, description, net_amount, approver_name, approver_email, kind="po"):
    token = new_token()
    with engine.begin() as conn:
        conn.execute(pending_approvals.insert().values(
            token=token, po_id=str(po_id), po_number=po_number, supplier=supplier,
            description=description, net_amount=net_amount, approver_name=approver_name,
            approver_email=approver_email, decision="pending", kind=kind, created_at=now(),
        ))
    return token


def get_pending(token):
    with engine.connect() as conn:
        row = conn.execute(select(pending_approvals).where(pending_approvals.c.token == token)).mappings().first()
        return dict(row) if row else None


def decide(token, decision, reject_reason=None):
    """Records approve/reject exactly once - a token that's already been
    decided (someone double-clicking the link, or refreshing the page)
    is left untouched rather than overwritten."""
    with engine.begin() as conn:
        row = conn.execute(
            select(pending_approvals).where(pending_approvals.c.token == token)
        ).mappings().first()
        if not row:
            return None
        if row["decision"] != "pending":
            return dict(row)  # already decided - report what actually happened
        conn.execute(
            pending_approvals.update().where(pending_approvals.c.token == token).values(
                decision=decision, reject_reason=reject_reason, decided_at=now(),
            )
        )
        row = conn.execute(
            select(pending_approvals).where(pending_approvals.c.token == token)
        ).mappings().first()
        return dict(row)


def undelivered_decisions():
    with engine.connect() as conn:
        rows = conn.execute(
            select(pending_approvals).where(
                pending_approvals.c.decision != "pending",
                pending_approvals.c.delivered_at.is_(None),
            )
        ).mappings().all()
        return [dict(r) for r in rows]


def mark_delivered(tokens):
    if not tokens:
        return
    with engine.begin() as conn:
        conn.execute(
            pending_approvals.update().where(pending_approvals.c.token.in_(tokens)).values(delivered_at=now())
        )


def cancel_pending_for(kind, entity_id):
    """Withdraws any approval request still awaiting an answer for one
    invoice/PO (e.g. the invoice was voided): marks it 'cancelled' and
    already-delivered, so the emailed link stops working and nothing is
    applied later."""
    with engine.begin() as conn:
        result = conn.execute(
            pending_approvals.update().where(
                pending_approvals.c.kind == kind,
                pending_approvals.c.po_id == str(entity_id),
                pending_approvals.c.decision == "pending",
            ).values(decision="cancelled", decided_at=now(), delivered_at=now())
        )
        return result.rowcount


def pending_requests():
    """Every approval request still awaiting an answer, newest first."""
    with engine.connect() as conn:
        rows = conn.execute(
            select(pending_approvals).where(pending_approvals.c.decision == "pending")
            .order_by(pending_approvals.c.created_at.desc())
        ).mappings().all()
        return [dict(r) for r in rows]


def record_reminder(token):
    with engine.begin() as conn:
        conn.execute(
            pending_approvals.update().where(pending_approvals.c.token == token).values(
                reminder_count=pending_approvals.c.reminder_count + 1, last_reminded_at=now(),
            )
        )
