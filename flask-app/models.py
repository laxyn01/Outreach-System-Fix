import os
from datetime import datetime

from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()

DATABASE_PATH = os.path.join(os.path.dirname(__file__), 'leads.db')


class Lead(db.Model):
    __tablename__ = 'leads'

    id = db.Column(db.Integer, primary_key=True)
    first_name = db.Column(db.String(120))
    last_name = db.Column(db.String(120))
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    company = db.Column(db.String(255))
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


class EmailAccount(db.Model):
    __tablename__ = 'email_accounts'

    id = db.Column(db.Integer, primary_key=True)
    email_address = db.Column(db.String(255), unique=True, nullable=False)
    app_password = db.Column(db.String(255), nullable=False)
    smtp_host = db.Column(db.String(255), default='smtp.gmail.com')
    smtp_port = db.Column(db.Integer, default=587)
    daily_sent_count = db.Column(db.Integer, default=0)
    last_reset_date = db.Column(db.Date, default=datetime.utcnow)
    warmup_enabled = db.Column(db.Boolean, default=False)
    warmup_day = db.Column(db.Integer, default=1)

    def reset_daily_if_needed(self):
        today = datetime.utcnow().date()
        if self.last_reset_date != today:
            self.daily_sent_count = 0
            self.last_reset_date = today
            if self.warmup_enabled:
                self.warmup_day = min(self.warmup_day + 1, 30)


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

    lead = db.relationship('Lead', backref='logs')


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
            env_url = os.getenv('TRACKING_BASE_URL')
            if env_url:
                row.tracking_base_url = env_url
            db.session.add(row)
            db.session.commit()
        return row


def init_db(app):
    app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{DATABASE_PATH}'
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    db.init_app(app)
    with app.app_context():
        db.create_all()
        Settings.get_singleton()
