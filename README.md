# The shared Purchase-to-Pay service - setup

This is no longer just "the thing that emails David an approval link." It is
now the **whole shared backend** for Next-Gen Purchase-to-Pay: every purchase
order, invoice, payment and audit entry lives here, in one database, so
everyone's copy of the desktop app sees the same live data instead of each
person having their own separate one. Email approval is still part of it
(exactly as before), but it's now one feature of a proper shared service
rather than the whole point of it.

It runs somewhere other than any one person's PC, on purpose - so it keeps
working (and David can still approve from his phone) even if every desktop
app is closed, and so a handful of people in the same office all read and
write the same purchase orders, invoices and payments.

Being honest about the size of this: it needs four free accounts (none need
a card) and about 20-30 minutes the first time. If you already set this up
for email approval before, you don't need to redo any of the accounts below
- you only need to re-upload the changed code (step 1) and Render will
redeploy the bigger service automatically.

## 1. GitHub (to hold the code so Render can deploy it)

1. Go to [github.com/join](https://github.com/join) and create a free account, if you don't have one already.
2. If you already have the `ptp-approval-service` repository from before: open it, and for each of these files, open it, click the pencil (edit) icon, delete the old contents, paste in the new version from this `approval-service` folder, and commit: `app.py`, `db.py`, `requirements.txt`. Then add the new files the same way (**Add file -> Create new file**, paste the contents, commit): `workflow.py`, `xero_client.py`, `outlook_client.py`, `claude_extract.py`.
3. If this is the first time: click **New** to create a new repository, name it something like `ptp-approval-service`, leave it **Private**, don't add a README, then click **Create repository**, then **uploading an existing file** and drag in every file from this `approval-service` folder: `app.py`, `db.py`, `workflow.py`, `xero_client.py`, `outlook_client.py`, `claude_extract.py`, `emailer.py`, `requirements.txt`, `Procfile`. (Skip `.env.example` and this `README.md` - they're just for reference, not needed by the service itself.)
4. Click **Commit changes**. Render will pick this up and redeploy automatically within a minute or two (see step 4 below for how to watch that).

## 2. Supabase (the shared database)

Already set up if you did this before - nothing new to do. If this is the
first time:

1. Go to [supabase.com](https://supabase.com) and sign up for free.
2. Click **New project**. Give it any name, set a database password (write it down), pick the region closest to you, and create it - takes a minute or two to provision.
3. Once it's ready, click the **Connect** button near the top of the project page. In the dialog, choose **Shared pooler, session mode** (it's the one labelled for IPv4-only networks - Render needs that one specifically, the plain "Direct connection" option won't work from there). Copy the connection string shown (starts `postgresql://`) and replace `[YOUR-PASSWORD]` in it with the database password you set when creating the project.
4. Keep this connection string safe for step 4 below - it's `DATABASE_URL`.

The service will automatically create the extra tables it now needs
(purchase orders, invoices, payments, audit log, settings) the next time it
starts up - nothing to do by hand in Supabase itself.

## 3. SMTP2GO (sends the approval emails)

Already set up if you did this before - nothing new to do. If this is the
first time:

1. Go to [smtp2go.com](https://www.smtp2go.com) and sign up for the free plan (1,000 emails/month, no card needed).
2. Go to **Sender Domains / Verified Senders** and verify the email address you want these to come from (e.g. `purchasing@next-gen.uk`, or your own address). SMTP2GO emails that address a confirmation link - click it.
   - If that fails specifically because your domain already has DMARC set up (common with Microsoft 365), you'll need to verify the whole domain instead - SMTP2GO's own instructions cover adding a couple of DNS records for this; happy to help with that step if you hit it.
3. Go to **Settings -> SMTP Users** and create one. Note down the **username** and **password** it gives you - that's `SMTP2GO_USERNAME` and `SMTP2GO_PASSWORD` below.

## 4. Render (hosts the service, always on, free)

If you already have this service on Render: open it, go to the **Logs** tab,
and watch it redeploy after step 1's commit - it should say the new version
is live within a couple of minutes. Nothing else to change here. Skip to
step 5.

If this is the first time:

1. Go to [render.com](https://render.com) and sign up free, using your GitHub account (makes the next step easier).
2. Click **New -> Web Service**, and pick the `ptp-approval-service` repository you created in step 1.
3. Leave the build settings as Render suggests (it should detect Python automatically from `requirements.txt`). Under **Instance Type**, pick the free tier.
4. Before creating it, scroll to **Environment Variables** and add these (see `.env.example` in this folder for the full explanation of each one):

   | Key | Value |
   |---|---|
   | `DATABASE_URL` | the Supabase connection string from step 2 |
   | `SMTP2GO_USERNAME` | from step 3 |
   | `SMTP2GO_PASSWORD` | from step 3 |
   | `SMTP2GO_FROM_EMAIL` | the address you verified in step 3 |
   | `SMTP2GO_FROM_NAME` | `Next-Gen Purchase-to-Pay` |
   | `APP_SHARED_SECRET` | make up any long random value yourself - e.g. mash the keyboard for 30 characters. This is the one key every desktop app in the office will be given, so everyone can reach the shared data. |
   | `PUBLIC_BASE_URL` | leave this blank for now |

5. Click **Create Web Service**. Render will build and deploy it - watch the log until it says the service is live.
6. Render will show you this service's URL at the top of the page, something like `https://ptp-approval-service.onrender.com`. Go back into **Environment**, set `PUBLIC_BASE_URL` to that exact URL, and save (this makes Render redeploy - that's expected).
7. Check it's alive: open `https://your-service-name.onrender.com/healthz` in a browser - it should show `{"ok":true}`.

   One thing to know about Render's free tier: it goes to sleep after 15 minutes with no requests, and takes maybe 30-60 seconds to wake up on the next one. That just means the first thing anyone does after a quiet spell might take a little longer - after that it's instant again for everyone until it goes quiet again.

## 5. Connect each desktop app

This is now a required step for the app to do anything at all, not an
optional extra - every purchase order, invoice and payment lives on this
service, so each of the 4-10 people's installs needs to be pointed at it
once.

1. Open Next-Gen Purchase-to-Pay on that person's PC. If it isn't connected yet you'll see a banner at the top prompting you to Settings.
2. Go to **Settings**, find the **Shared Purchase-to-Pay service** panel near the top, and fill in:
   - **Service URL**: the Render URL from step 4.6 (e.g. `https://ptp-approval-service.onrender.com`)
   - **Shared key**: the exact same value used for `APP_SHARED_SECRET` in step 4.4
   - **Your name**: whatever that person wants to appear as in the audit trail when they act (raise a PO, mark goods received, and so on)
3. Click **Save**.

Repeat step 5 on every PC that will use the app. Everyone then sees the same
live purchase orders, invoices and payments, and email approval works
exactly as before: any purchase order routed to a named approver - whether
raised through the app, updated with actual costs, or imported from Xero -
emails them automatically, and whoever picks up the app next sees their
decision applied within about a minute.

Xero and Microsoft 365 connections (Settings -> Connections) now live on
this shared service too, not on any one PC - so only one person needs to
connect each of those once, from whichever PC is convenient, and everyone
benefits. Signing in still has to happen in a real browser on that one PC
(Microsoft/Xero require that), but the resulting connection is then stored
centrally, and if that PC is later switched off or that install is
uninstalled, the connection carries on working for everyone else exactly as
before.


## Matching, due dates, reminders and reports (service side)

New code: `matching.py` (PO suggestions), `reminders.py` (working-day maths), `reporting.py` (supplier and month-end reports). New database columns are added automatically on start-up. New settings (all in the `settings` table, editable in the app): `auto_match_enabled`, `reminders_enabled`, `reminder_after_days` (2), `reminder_interval_days` (2), `reminder_max` (3).

New endpoints (all need the X-API-Key header): `GET /api/invoices/<id>/suggestions`; `POST /api/invoices/<id>/unmatch`, `/void`, `/restore`, `/details`; `POST /api/approvals/remind`; `GET /api/reports/suppliers`, `/api/reports/suppliers/<key>`, `/api/reports/month-end?month=YYYY-MM`.

Reminders are sent by the same background check that syncs Outlook (every 30 minutes while the service is awake), only Mon-Fri 08:00-18:00 UK time. After pushing these files to GitHub, wait for Render to show **Live**.

New settings keys `invoice2_band1..4_name/_email` hold the second approver per value band (blank until set). New invoice columns (added automatically): `needs_second_approval`, `approval_stage`, `first_approver`, `first_approved_at`, `second_approver`, `second_approver_email`, `second_approval_reason`, `po_prior_status`. Invoice lists no longer read the PDF bytes from the database, which makes the inbox, ledger and match dialog much faster.

New purchase-order columns (added automatically): `department`, `parent_po_id`, `is_split`; new table `po_lines` holds the suppliers inside a multi-supplier Solar request. Endpoints: `POST /api/pos` now accepts `department` and `lines`; `GET /api/pos/<id>` returns `lines` for a split request; `GET /api/reports/weekly?week=YYYY-MM-DD` (any day in the week).

## Keeping the service awake (recommended)

Render's free tier puts the service to sleep after 15 minutes with no requests, and it then takes up to a minute to wake. While the desktop app is open it checks in every few minutes, which keeps things awake - but an approver tapping an emailed Approve link from their phone at 7pm, with nobody's app open, would wait a minute for the page.

To avoid that, add a free uptime monitor that visits the service every 5 minutes:

1. Go to [uptimerobot.com](https://uptimerobot.com) and sign up for the free plan.
2. Click **Add New Monitor**, choose **HTTP(s)**, and enter `https://your-service-name.onrender.com/healthz` as the URL.
3. Set the monitoring interval to **5 minutes** and save.

`/healthz` needs no key and reveals nothing - it just answers `{"ok": true}`. (There's also `/healthz/deep`, which additionally checks the database answers; the desktop app's status indicator uses that one.)

## Invoice (payment) approval

Settings -> **Invoice Approval Permissions** holds four names and emails, one per value range, for approving payment of supplier invoices - separate from the purchase order approvers. Nothing extra to set up on the service: it uses the same email settings as purchase order approval. Upgrading an existing database is automatic (new columns are added on startup).

## Pingu (solar jobs) raising purchase orders

Pingu's project page (Pingu-Project.html, Costs tab) has a **Raise POs in Purchase-to-Pay** button. For one job it raises a single Solar request with up to three lines - **Labour** (to the job's lead engineer), **Edmundson** and **PVF** - each described as `Job <number> - <type>`. It goes through the normal approval, and when issued becomes one Xero purchase order per supplier, exactly like any multi-supplier Solar request. Pingu then shows the request's status and the PO numbers.

Pingu runs in a web browser on its own website, so it does **not** use `APP_SHARED_SECRET`. It has its own key, which can only raise one request per job and read that request's status - it can't see, approve, issue or pay anything else.

**To switch it on (Render -> this service -> Environment), add:**

| Key | Value |
|---|---|
| `PINGU_API_KEY` | the key written in Pingu-Project.html as `PTP_PINGU_KEY` (they must match exactly) |
| `PINGU_ALLOWED_ORIGINS` | the web address Pingu is opened from, e.g. `https://pingu-solar.web.app` (just the start of the address, no page name or trailing slash). Several can be comma-separated. |
| `PINGU_EDMUNDSON_SUPPLIER` | optional - Xero contact name for Edmundson (default `Edmundson`) |
| `PINGU_PVF_SUPPLIER` | optional - Xero contact name for PVF (default `PVF`) |

New database column `purchase_orders.external_ref` (e.g. `pingu:J-4B31C51C`) is added automatically on start-up; it stops the same job's purchase orders being raised twice. A job can be raised again only if its last request was rejected or voided. Requests from Pingu have `source = 'pingu'`.

New endpoints (need the `X-Pingu-Key` header, not `X-API-Key`): `POST /api/pingu/pos` and `GET /api/pingu/pos?job=<job number>`.
