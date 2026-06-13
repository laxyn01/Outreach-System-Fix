# OutreachCommand

Cold email outreach system — manages leads, sends 3-step email sequences, tracks opens/clicks, handles unsubscribes and reply detection.

## Run & Operate

- `cd flask-app && python app.py` — run the Flask app (port 5000, workflow: "OutreachCommand (Flask)")
- `pnpm --filter @workspace/api-server run dev` — run the Node API server (port 8080, unused by Flask app)
- Required env: none required (SQLite DB created automatically at `flask-app/leads.db`)
- Optional env: `FLASK_SECRET_KEY`, `TRACKING_BASE_URL`, `PORT`

## Stack

- Python 3.11, Flask 3, Flask-SQLAlchemy, APScheduler
- DB: SQLite via SQLAlchemy (file: `flask-app/leads.db`)
- Email: smtplib STARTTLS (Gmail app passwords)
- IMAP: imaplib for reply detection

## Where things live

- `flask-app/app.py` — all routes and Flask app factory
- `flask-app/models.py` — SQLAlchemy models (Lead, EmailAccount, Template, EmailLog, Settings)
- `flask-app/email_sender.py` — SMTP send logic, warmup, preview
- `flask-app/sequence.py` — lead picking, account selection, send window logic
- `flask-app/tracker.py` — placeholder replacement, link wrapping, tracking pixel injection
- `flask-app/spintax.py` — spintax parser
- `flask-app/imap_replies.py` — IMAP reply checker
- `flask-app/scheduler.py` — APScheduler background jobs
- `flask-app/templates/` — Jinja2 HTML templates
- `flask-app/static/style.css` — all styles

## Architecture decisions

- SQLite used for simplicity; swap DATABASE_PATH in models.py for PostgreSQL URI if needed
- Upload preview passes rows as JSON in a hidden form field (json.dumps default=str) rather than session storage
- Scheduler runs every 60s but respects a per-send rate limit of 60–120s between emails
- Tracking pixel and click wrapping injected at send time using the base URL from Settings
- All placeholder replacement happens AFTER spintax parsing to avoid conflicts

## Product

Dashboard → upload CSV leads → configure email accounts (Gmail SMTP) → set 3 email templates → scheduler auto-sends step 1/2/3 with 3-day and 4-day gaps → tracks opens (pixel), clicks (redirect), replies (IMAP daily scan) → unsubscribe link in every email.

## User preferences

_Populate as you build — explicit user instructions worth remembering across sessions._

## Gotchas

- Gmail requires App Passwords (not account password) — enable 2FA then create one in Google account security
- TRACKING_BASE_URL must be your public domain for open/click tracking to work in real emails
- APScheduler runs in-process; if you use gunicorn with multiple workers, set up an external scheduler
- Warmup emails go to warmup_addresses from Settings; leave blank to skip warmup

## Pointers

- See the `pnpm-workspace` skill for the Node.js workspace structure (separate from Flask app)
