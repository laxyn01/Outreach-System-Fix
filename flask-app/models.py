import os
from datetime import datetime

from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()

DATABASE_PATH = os.path.join(os.path.dirname(__file__), 'leads.db')


def _detect_public_url():
    """Auto-detect the public Replit domain for tracking URLs."""
    env_url = os.getenv('TRACKING_BASE_URL')
    if env_url:
        return env_url
    domains = os.getenv('REPLIT_DOMAINS', '').split(',')
    for d in domains:
        d = d.strip()
        if d:
            return f'https://{d}'
    dev = os.getenv('REPLIT_DEV_DOMAIN', '').strip()
    if dev:
        return f'https://{dev}'
    return None


class Campaign(db.Model):
    __tablename__ = 'campaigns'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False)
    status = db.Column(db.String(50), default='draft')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    # Legacy template FKs
    template_step1_id = db.Column(db.Integer, db.ForeignKey('templates.id'), nullable=True)
    template_step2_id = db.Column(db.Integer, db.ForeignKey('templates.id'), nullable=True)
    template_step3_id = db.Column(db.Integer, db.ForeignKey('templates.id'), nullable=True)
    # Steps as JSON: [{wait_days, variants:[{subject, body}]}]
    steps_json = db.Column(db.Text, default='[]')
    # Per-campaign schedule
    campaign_schedule_start = db.Column(db.String(10), default='')
    campaign_schedule_end = db.Column(db.String(10), default='')
    campaign_timezone = db.Column(db.String(100), default='')
    campaign_active_days = db.Column(db.String(100), default='')
    campaign_daily_limit = db.Column(db.Integer, default=0)
    tracking_enabled = db.Column(db.Boolean, default=True)
    stop_on_reply = db.Column(db.Boolean, default=True)

    template_step1 = db.relationship('Template', foreign_keys=[template_step1_id])
    template_step2 = db.relationship('Template', foreign_keys=[template_step2_id])
    template_step3 = db.relationship('Template', foreign_keys=[template_step3_id])

    def get_steps(self):
        import json
        if not self.steps_json:
            return []
        try:
            return json.loads(self.steps_json)
        except Exception:
            return []

    @property
    def leads_count(self):
        return CampaignLead.query.filter_by(campaign_id=self.id).count()

    @property
    def sent_count(self):
        return CampaignLead.query.filter(
            CampaignLead.campaign_id == self.id,
            CampaignLead.sequence_step > 0,
        ).count()

    @property
    def open_rate(self):
        sent = self.sent_count
        if not sent:
            return 0.0
        opened = CampaignLead.query.filter_by(campaign_id=self.id, opened=True).count()
        return round(opened / sent * 100, 1)

    @property
    def reply_rate(self):
        sent = self.sent_count
        if not sent:
            return 0.0
        replied = CampaignLead.query.filter_by(campaign_id=self.id, replied=True).count()
        return round(replied / sent * 100, 1)

    @property
    def failed_count(self):
        from models import EmailLog
        return EmailLog.query.filter_by(
            campaign_id=self.id,
            log_type='campaign',
            status='failed'
        ).count()

    @property
    def click_rate(self):
        sent = self.sent_count
        if not sent:
            return 0.0
        clicked = CampaignLead.query.filter_by(campaign_id=self.id, clicked=True).count()
        return round(clicked / sent * 100, 1)

    def status_color(self):
        return {
            'draft': 'gray',
            'active': 'green',
            'paused': 'yellow',
            'complete': 'blue',
        }.get(self.status, 'gray')


class Lead(db.Model):
    __tablename__ = 'leads'

    id = db.Column(db.Integer, primary_key=True)
    first_name = db.Column(db.String(120))
    last_name = db.Column(db.String(120))
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    company = db.Column(db.String(255))
    icebreaker = db.Column(db.Text)
    sequence_step = db.Column(db.Integer, default=0)
    last_sent_at = db.Column(db.DateTime)
    next_send_at = db.Column(db.DateTime)
    opened = db.Column(db.Boolean, default=False)
    opened_at = db.Column(db.DateTime)
    replied = db.Column(db.Boolean, default=False)
    unsubscribed = db.Column(db.Boolean, default=False)
    paused = db.Column(db.Boolean, default=False)
    clicked = db.Column(db.Boolean, default=False)
    clicked_at = db.Column(db.DateTime)
    assigned_account = db.Column(db.String(255))
    campaign_id = db.Column(db.Integer, db.ForeignKey('campaigns.id'), nullable=True)

    campaign = db.relationship('Campaign', backref='leads', foreign_keys=[campaign_id])

    @property
    def full_name(self):
        parts = [self.first_name or '', self.last_name or '']
        return ' '.join(p for p in parts if p).strip() or '—'

    def status_label(self):
        if self.unsubscribed:
            return 'Unsubscribed'
        if self.replied:
            return 'Replied'
        if self.paused:
            return 'Paused'
        if self.sequence_step == 0:
            return 'Pending'
        if self.sequence_step < 3:
            return 'Active'
        return 'Complete'


class CampaignLead(db.Model):
    __tablename__ = 'campaign_leads'
    __table_args__ = (
        db.UniqueConstraint('campaign_id', 'lead_id', name='uq_campaign_lead'),
    )

    id = db.Column(db.Integer, primary_key=True)
    campaign_id = db.Column(db.Integer, db.ForeignKey('campaigns.id'), nullable=False, index=True)
    lead_id = db.Column(db.Integer, db.ForeignKey('leads.id'), nullable=False, index=True)

    sequence_step = db.Column(db.Integer, default=0)
    last_sent_at = db.Column(db.DateTime)
    next_send_at = db.Column(db.DateTime)
    finished = db.Column(db.Boolean, default=False)

    opened = db.Column(db.Boolean, default=False)
    opened_at = db.Column(db.DateTime)
    clicked = db.Column(db.Boolean, default=False)
    clicked_at = db.Column(db.DateTime)
    replied = db.Column(db.Boolean, default=False)

    assigned_account = db.Column(db.String(255))

    lead = db.relationship('Lead', backref='campaign_memberships')
    campaign = db.relationship('Campaign', backref='campaign_leads_assoc')

    def status_label(self):
        lead = self.lead
        if lead and lead.unsubscribed:
            return 'Unsubscribed'
        if self.replied or (lead and lead.replied):
            return 'Replied'
        if lead and lead.paused:
            return 'Paused'
        if self.finished:
            return 'Complete'
        if self.sequence_step == 0:
            return 'Pending'
        return 'Active'


class EmailAccount(db.Model):
    __tablename__ = 'email_accounts'

    id = db.Column(db.Integer, primary_key=True)
    email_address = db.Column(db.String(255), unique=True, nullable=False)
    app_password = db.Column(db.String(255), nullable=False, default='')
    smtp_host = db.Column(db.String(255), default='smtp.gmail.com')
    smtp_port = db.Column(db.Integer, default=587)
    daily_sent_count = db.Column(db.Integer, default=0)
    last_reset_date = db.Column(db.Date, default=datetime.utcnow)
    warmup_enabled = db.Column(db.Boolean, default=False)
    warmup_day = db.Column(db.Integer, default=1)
    # ── NEW: OAuth support ──
    auth_type = db.Column(db.String(10), default='smtp')   # 'smtp' or 'oauth'
    oauth_token = db.Column(db.Text, default=None)          # JSON token from Google
    last_error = db.Column(db.Text)
    last_error_at = db.Column(db.DateTime)
    consecutive_failures = db.Column(db.Integer, default=0)
    is_paused_auto = db.Column(db.Boolean, default=False)

    # ── NEW: warmup system (kept 100% separate from daily_sent_count) ──
    # daily_sent_count is RESERVED for real campaign sends. Warmup sends are
    # counted here and nowhere else.
    warmup_sent_today = db.Column(db.Integer, default=0)
    warmup_last_reset_date = db.Column(db.Date)
    warmup_next_allowed_send_at = db.Column(db.DateTime)
    warmup_target_daily = db.Column(db.Integer, default=18)
    warmup_daily_goal = db.Column(db.Integer, default=0)

    def reset_daily_if_needed(self):
        today = datetime.utcnow().date()
        if self.last_reset_date != today:
            self.daily_sent_count = 0
            self.last_reset_date = today
            if self.warmup_enabled:
                self.warmup_day = min(self.warmup_day + 1, 30)

    def reset_warmup_daily_if_needed(self):
        """Roll the WARMUP counters for a new day.

        Delegates the ramp-day / campaign-counter roll to the existing
        reset_daily_if_needed() so warmup_day is still advanced exactly once
        per day by the original code path, then rolls the warmup-only counters.
        This never touches daily_sent_count itself.
        """
        self.reset_daily_if_needed()
        today = datetime.utcnow().date()
        if self.warmup_last_reset_date != today:
            self.warmup_sent_today = 0
            self.warmup_daily_goal = 0
            self.warmup_next_allowed_send_at = None
            self.warmup_last_reset_date = today


class Template(db.Model):
    __tablename__ = 'templates'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False)
    step = db.Column(db.Integer, nullable=False)
    subject = db.Column(db.String(500), nullable=False)
    body = db.Column(db.Text, nullable=False)
    is_html = db.Column(db.Boolean, default=False)


class EmailLog(db.Model):
    __tablename__ = 'email_logs'

    id = db.Column(db.Integer, primary_key=True)
    lead_id = db.Column(db.Integer, db.ForeignKey('leads.id'), nullable=True)
    account_used = db.Column(db.String(255))
    step = db.Column(db.Integer)
    subject = db.Column(db.String(500))
    sent_at = db.Column(db.DateTime, default=datetime.utcnow)
    opened_at = db.Column(db.DateTime)
    open_count = db.Column(db.Integer, default=0)
    clicked = db.Column(db.Boolean, default=False)
    clicked_at = db.Column(db.DateTime)
    log_type = db.Column(db.String(50), default='campaign')
    status = db.Column(db.String(50), default='sent')
    lead_email = db.Column(db.String(255))
    lead_name = db.Column(db.String(255))
    campaign_id = db.Column(db.Integer, db.ForeignKey('campaigns.id'), nullable=True)
    tracking_token = db.Column(db.String(64), index=True)
    message_id = db.Column(db.String(255))
    gmail_thread_id = db.Column(db.String(255))
    error_message = db.Column(db.Text)
    variant_index = db.Column(db.Integer, default=0)

    lead = db.relationship('Lead', backref='logs')
    campaign = db.relationship('Campaign', backref='logs')


class WarmupEmail(db.Model):
    """One row per warmup email SENT between two of our own accounts.

    This table is completely separate from EmailLog/Lead/CampaignLead so that
    warmup traffic can never leak into real campaign analytics. (A mirror row
    is still written to EmailLog with log_type='warmup' so the data is visible
    in one place if you ever want a warmup log view — every existing page
    filters on log_type='campaign', so it stays invisible to them.)
    """
    __tablename__ = 'warmup_emails'

    id = db.Column(db.Integer, primary_key=True)

    sender_account_id = db.Column(db.Integer, db.ForeignKey('email_accounts.id'), nullable=True, index=True)
    recipient_account_id = db.Column(db.Integer, db.ForeignKey('email_accounts.id'), nullable=True, index=True)
    sender_email = db.Column(db.String(255))
    recipient_email = db.Column(db.String(255), index=True)

    subject = db.Column(db.String(500))
    token = db.Column(db.String(64), index=True)
    message_id = db.Column(db.String(255), index=True)
    gmail_thread_id = db.Column(db.String(255))

    sent_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    status = db.Column(db.String(50), default='sent')     # 'sent' | 'failed'
    error_message = db.Column(db.Text)

    # Threading / loop protection
    is_reply = db.Column(db.Boolean, default=False, index=True)
    parent_id = db.Column(db.Integer, db.ForeignKey('warmup_emails.id'), nullable=True, index=True)

    # Deferred open simulation
    open_due_at = db.Column(db.DateTime, index=True)
    opened_at = db.Column(db.DateTime)

    # Deferred auto-reply
    delivered_detected_at = db.Column(db.DateTime)
    reply_scheduled = db.Column(db.Boolean, default=False, index=True)
    reply_due_at = db.Column(db.DateTime, index=True)
    reply_sent = db.Column(db.Boolean, default=False, index=True)
    replied_at = db.Column(db.DateTime)


class Settings(db.Model):
    __tablename__ = 'settings'

    id = db.Column(db.Integer, primary_key=True, default=1)
    sender_name = db.Column(db.String(255), default='Your Name')
    video_link_url = db.Column(db.String(500), default='')
    warmup_addresses = db.Column(db.Text, default='')
    tracking_base_url = db.Column(db.String(500), default='http://localhost:5000')
    pitch_text = db.Column(db.Text, default='')
    next_allowed_send_at = db.Column(db.DateTime)
    send_window_start = db.Column(db.String(10), default='09:00')
    send_window_end = db.Column(db.String(10), default='18:00')
    timezone = db.Column(db.String(100), default='Asia/Kolkata')
    active_days = db.Column(db.String(100), default='Mon,Tue,Wed,Thu,Fri')
    daily_limit_per_account = db.Column(db.Integer, default=15)

    @classmethod
    def get_singleton(cls):
        row = cls.query.get(1)
        if not row:
            row = cls(id=1)
            auto_url = _detect_public_url()
            if auto_url:
                row.tracking_base_url = auto_url
            db.session.add(row)
            db.session.commit()
        else:
            if row.tracking_base_url in ('http://localhost:5000', ''):
                auto_url = _detect_public_url()
                if auto_url:
                    row.tracking_base_url = auto_url
                    db.session.commit()
        return row


def init_db(app):
    app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URL', f'sqlite:///{DATABASE_PATH}')
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    'pool_pre_ping': True,
    'pool_recycle': 280,
}
    db.init_app(app)
    with app.app_context():
        db.create_all()
        _run_migrations()
        _verify_warmup_schema()
        _backfill_campaign_leads()
        Settings.get_singleton()


def _run_migrations():
    migrations = [
        "ALTER TABLE leads ADD COLUMN campaign_id INTEGER REFERENCES campaigns(id)",
        "ALTER TABLE email_logs ADD COLUMN campaign_id INTEGER REFERENCES campaigns(id)",
        "ALTER TABLE campaigns ADD COLUMN steps_json TEXT DEFAULT '[]'",
        "ALTER TABLE campaigns ADD COLUMN campaign_schedule_start TEXT DEFAULT ''",
        "ALTER TABLE campaigns ADD COLUMN campaign_schedule_end TEXT DEFAULT ''",
        "ALTER TABLE campaigns ADD COLUMN campaign_timezone TEXT DEFAULT ''",
        "ALTER TABLE campaigns ADD COLUMN campaign_active_days TEXT DEFAULT ''",
        "ALTER TABLE campaigns ADD COLUMN campaign_daily_limit INTEGER DEFAULT 0",
        "ALTER TABLE campaigns ADD COLUMN tracking_enabled INTEGER DEFAULT 1",
        "ALTER TABLE campaigns ADD COLUMN stop_on_reply INTEGER DEFAULT 1",
        # NEW: OAuth columns
        "ALTER TABLE email_accounts ADD COLUMN auth_type TEXT DEFAULT 'smtp'",
        "ALTER TABLE email_accounts ADD COLUMN oauth_token TEXT DEFAULT NULL",
        "ALTER TABLE email_logs ADD COLUMN tracking_token TEXT",
        "ALTER TABLE email_logs ADD COLUMN message_id TEXT",
        "ALTER TABLE email_logs ADD COLUMN gmail_thread_id TEXT",
        "ALTER TABLE email_logs ADD COLUMN error_message TEXT",
        "ALTER TABLE email_accounts ADD COLUMN last_error TEXT",
        "ALTER TABLE email_accounts ADD COLUMN last_error_at TIMESTAMP",
        "ALTER TABLE email_accounts ADD COLUMN consecutive_failures INTEGER DEFAULT 0",
        "ALTER TABLE email_accounts ADD COLUMN is_paused_auto BOOLEAN DEFAULT FALSE",
        "ALTER TABLE email_logs ADD COLUMN variant_index INTEGER DEFAULT 0",
        "ALTER TABLE leads ADD COLUMN icebreaker TEXT",
        # ── NEW: warmup system columns (valid PostgreSQL syntax) ──
        "ALTER TABLE email_accounts ADD COLUMN IF NOT EXISTS warmup_sent_today INTEGER DEFAULT 0",
        "ALTER TABLE email_accounts ADD COLUMN IF NOT EXISTS warmup_last_reset_date DATE",
        "ALTER TABLE email_accounts ADD COLUMN IF NOT EXISTS warmup_next_allowed_send_at TIMESTAMP",
        "ALTER TABLE email_accounts ADD COLUMN IF NOT EXISTS warmup_target_daily INTEGER DEFAULT 18",
        "ALTER TABLE email_accounts ADD COLUMN IF NOT EXISTS warmup_daily_goal INTEGER DEFAULT 0",
    ]
    with db.engine.connect() as conn:
        for sql in migrations:
            try:
                conn.execute(db.text(sql))
                conn.commit()
            except Exception:
                conn.rollback()


# Columns the warmup system cannot run without. The migration runner above
# swallows errors silently, so we verify afterwards and print LOUDLY if a
# column is missing instead of failing mysteriously at runtime later.
_WARMUP_REQUIRED_COLUMNS = {
    'email_accounts': [
        'warmup_enabled',
        'warmup_day',
        'warmup_sent_today',
        'warmup_last_reset_date',
        'warmup_next_allowed_send_at',
        'warmup_target_daily',
        'warmup_daily_goal',
    ],
    'warmup_emails': [
        'id', 'sender_account_id', 'recipient_account_id', 'sender_email',
        'recipient_email', 'subject', 'token', 'message_id', 'gmail_thread_id',
        'sent_at', 'status', 'error_message', 'is_reply', 'parent_id',
        'open_due_at', 'opened_at', 'delivered_detected_at',
        'reply_scheduled', 'reply_due_at', 'reply_sent', 'replied_at',
    ],
}


def _verify_warmup_schema():
    """Post-migration sanity check. Never raises — only reports."""
    try:
        from sqlalchemy import inspect as sa_inspect
        inspector = sa_inspect(db.engine)
        existing_tables = set(inspector.get_table_names())
        missing = []
        for table, cols in _WARMUP_REQUIRED_COLUMNS.items():
            if table not in existing_tables:
                missing.append(f'{table} (whole table missing)')
                continue
            present = {c['name'] for c in inspector.get_columns(table)}
            for col in cols:
                if col not in present:
                    missing.append(f'{table}.{col}')
        if missing:
            print('[WARMUP][SCHEMA] MISSING COLUMNS -> warmup will be DISABLED: '
                  + ', '.join(missing), flush=True)
        else:
            print('[WARMUP][SCHEMA] OK — all warmup columns/tables present.', flush=True)
        return missing
    except Exception as e:
        print(f'[WARMUP][SCHEMA] verification skipped: {e}', flush=True)
        return []


def _backfill_campaign_leads():
    sql = db.text("""
        INSERT OR IGNORE INTO campaign_leads
            (campaign_id, lead_id, sequence_step, last_sent_at,
             finished, opened, opened_at, clicked, clicked_at,
             replied, assigned_account)
        SELECT
            l.campaign_id,
            l.id,
            l.sequence_step,
            l.last_sent_at,
            CASE WHEN l.sequence_step >= 3 THEN 1 ELSE 0 END,
            l.opened,
            l.opened_at,
            l.clicked,
            l.clicked_at,
            l.replied,
            l.assigned_account
        FROM leads l
        WHERE l.campaign_id IS NOT NULL
    """)
    try:
        with db.engine.connect() as conn:
            conn.execute(sql)
            conn.commit()
    except Exception:
        pass
