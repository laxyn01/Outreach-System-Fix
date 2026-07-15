import base64
import json
import random
import smtplib
import secrets
import uuid
import re
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr

from models import Campaign, CampaignLead, EmailAccount, EmailLog, Lead, Settings, Template, db
from sequence import (
    advance_campaign_lead_step,
    advance_sequence_step,
    get_available_accounts,
    is_within_send_window,
    pick_account,
    pick_next_campaign_lead,
    pick_next_lead,
    pick_template,
)
from spintax import parse_spintax
from tracker import (
    append_unsubscribe,
    ensure_html_wrapper,
    inject_tracking_pixel,
    replace_placeholders,
    wrap_links,
)


def clean_email(raw: str):
    if not raw:
        return None
    email_val = str(raw).split(',')[0].split('#')[0].strip()
    if '@' not in email_val:
        return None
    local, _, domain = email_val.partition('@')
    if '.' not in domain:
        return None
    return email_val.lower()


def _get_fresh_settings():
    """Always re-query settings from DB, never use stale SQLAlchemy cache."""
    db.session.expire_all()
    return Settings.get_singleton()


def prepare_content(subject_raw: str, body_raw: str, lead, settings, step: int, tracking_enabled: bool = True, tracking_token: str = None):
    """Prepare email content from raw subject/body strings (spintax + placeholders)."""
    sender_name = (settings.sender_name or '').strip() or 'Your Name'
    video_link = settings.video_link_url or ''
    base_url = settings.tracking_base_url or 'http://localhost:5000'

    lead.pitch_text = settings.pitch_text or ''

    subject = parse_spintax(subject_raw or '')
    body = parse_spintax(body_raw or '')

    subject = replace_placeholders(subject, lead, sender_name, video_link)
    body = replace_placeholders(body, lead, sender_name, video_link)

    has_html_tags = bool(re.search(r'<[a-z][\s\S]*>', body, re.IGNORECASE))
    plain, html = ensure_html_wrapper(body, has_html_tags)
    if html and tracking_enabled:
        html = wrap_links(html, lead.id, step, base_url)
        html = inject_tracking_pixel(html, tracking_token or f'{lead.id}-{step}', base_url)

    plain, html = append_unsubscribe(plain, html, lead.id, base_url)
    return subject, plain, html


def prepare_template_content(template, lead, settings, step: int):
    """Legacy: prepare content from a Template model."""
    return prepare_content(template.subject, template.body, lead, settings, step)


def send_smtp(account: EmailAccount, to_email: str, subject: str, plain: str, html: str, sender_name: str = '', in_reply_to: str = None, references: str = None):
    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    display = (sender_name or '').strip() or account.email_address
    msg['From'] = formataddr((display, account.email_address))
    msg['To'] = to_email
    msg['X-Mailer'] = 'Microsoft Outlook 16.0'
    msg['X-Priority'] = '3'
    msg['Importance'] = 'Normal'
    msg['Precedence'] = 'bulk'

    new_message_id = f'<{uuid.uuid4()}@{account.email_address.split("@")[-1]}>'
    msg['Message-ID'] = new_message_id

    if in_reply_to:
        msg['In-Reply-To'] = in_reply_to
        msg['References'] = references or in_reply_to

    if plain:
        msg.attach(MIMEText(plain, 'plain', 'utf-8'))
    if html:
        msg.attach(MIMEText(html, 'html', 'utf-8'))

    with smtplib.SMTP(account.smtp_host, account.smtp_port) as server:
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.login(account.email_address, account.app_password)
        server.sendmail(account.email_address, to_email, msg.as_string())

    return new_message_id, None


# ── NEW: Gmail API sender ─────────────────────────────────────────────────────

def send_gmail_api(account: EmailAccount, to_email: str, subject: str, plain: str, html: str, sender_name: str = '', in_reply_to: str = None, references: str = None, thread_id: str = None):
    """Send email via Gmail API using stored OAuth token."""
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build

    token_data = json.loads(account.oauth_token)
    creds = Credentials(
        token=token_data.get('token'),
        refresh_token=token_data.get('refresh_token'),
        token_uri='https://oauth2.googleapis.com/token',
        client_id=token_data.get('client_id'),
        client_secret=token_data.get('client_secret'),
        scopes=token_data.get('scopes'),
    )

    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        token_data['token'] = creds.token
        account.oauth_token = json.dumps(token_data)
        db.session.commit()

    service = build('gmail', 'v1', credentials=creds)

    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    display = (sender_name or '').strip() or account.email_address
    msg['From'] = formataddr((display, account.email_address))
    msg['To'] = to_email
    msg['X-Mailer'] = 'Microsoft Outlook 16.0'
    msg['X-Priority'] = '3'
    msg['Importance'] = 'Normal'
    msg['Precedence'] = 'bulk'

    new_message_id = f'<{uuid.uuid4()}@{account.email_address.split("@")[-1]}>'
    msg['Message-ID'] = new_message_id

    if in_reply_to:
        msg['In-Reply-To'] = in_reply_to
        msg['References'] = references or in_reply_to

    if plain:
        msg.attach(MIMEText(plain, 'plain', 'utf-8'))
    if html:
        msg.attach(MIMEText(html, 'html', 'utf-8'))

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    body = {'raw': raw}
    if thread_id:
        body['threadId'] = thread_id

    sent = service.users().messages().send(userId='me', body=body).execute()
    return new_message_id, sent.get('threadId')


# ─────────────────────────────────────────────────────────────────────────────
def _send_email(account: EmailAccount, to_email: str, subject: str, plain: str, html: str, sender_name: str = '', in_reply_to: str = None, references: str = None, thread_id: str = None):
    """Smart dispatcher — uses OAuth if available, falls back to SMTP."""
    if account.auth_type == 'oauth' and account.oauth_token:
        return send_gmail_api(account, to_email, subject, plain, html, sender_name, in_reply_to, references, thread_id)
    else:
        return send_smtp(account, to_email, subject, plain, html, sender_name, in_reply_to, references)


def send_test_email(account: EmailAccount) -> dict:
    try:
        subject = 'SMTP Test — OutreachCommand'
        plain = (
            f'This is a test email from OutreachCommand.\n'
            f'Sent at {datetime.utcnow().isoformat()} UTC\n\n'
            f'If you see this, SMTP is working correctly.'
        )
        _send_email(account, account.email_address, subject, plain, '', account.email_address)
        return {'ok': True, 'message': 'Test email sent successfully.'}
    except Exception as e:
        return {'ok': False, 'message': str(e)}
        
def _pick_campaign_content(campaign: Campaign, step: int):
    if not campaign:
        return None, None
    steps = campaign.get_steps()
    if not steps:
        return None, None
    step_idx = step - 1
    if step_idx < 0 or step_idx >= len(steps):
        return None, None
    step_data = steps[step_idx]
    variants = step_data.get('variants', [])
    if not variants:
        return None, None
    variant = random.choice(variants)
    return variant.get('subject', ''), variant.get('body', '')


def _pick_legacy_template(campaign: Campaign, step: int):
    if not campaign:
        return None
    tid = getattr(campaign, f'template_step{step}_id', None)
    if tid:
        return Template.query.get(tid)
    return None


def try_send_next_email() -> dict:
    settings = _get_fresh_settings()
    now = datetime.utcnow()

    if settings.next_allowed_send_at and now < settings.next_allowed_send_at:
        wait_secs = int((settings.next_allowed_send_at - now).total_seconds())
        return {
            'sent': 0, 'skipped': 1, 'errors': [],
            'reason': 'rate_limit', 'wait_seconds': wait_secs,
        }

    if not is_within_send_window(settings, now):
        return {'sent': 0, 'skipped': 1, 'errors': [], 'reason': 'outside_window'}

    cl = pick_next_campaign_lead(now)

    if cl:
        lead = cl.lead
        campaign = cl.campaign
        steps = campaign.get_steps() if campaign else []
        total_steps = len(steps) if steps else 3
        step = cl.sequence_step + 1

        accounts = get_available_accounts(settings)
        account = pick_account(accounts, lead)
        if not account:
            return {
                'sent': 0, 'skipped': 1,
                'errors': ['No email accounts available or daily limit reached.'],
                'reason': 'no_account',
            }

        subject_raw, body_raw = _pick_campaign_content(campaign, step)
        if subject_raw is None:
            template = _pick_legacy_template(campaign, step) or pick_template(step)
            if not template:
                return {'sent': 0, 'skipped': 1, 'errors': [f'No template for step {step}'], 'reason': 'no_template'}
            subject_raw = template.subject
            body_raw = template.body

        tracking_on = campaign.tracking_enabled if campaign else True
        tracking_token = secrets.token_hex(16) if tracking_on else None
        subject, plain, html = prepare_content(subject_raw, body_raw, lead, settings, step, tracking_on, tracking_token)
        sender_name = (settings.sender_name or '').strip() or 'Your Name'

        # Fetch previous step's Message-ID for threading
        # Fetch previous step's Message-ID for threading
        in_reply_to = None
        references = None
        thread_id = None
        if step > 1:
            prev_log = EmailLog.query.filter_by(
                lead_id=lead.id, campaign_id=cl.campaign_id, step=step - 1
            ).order_by(EmailLog.sent_at.desc()).first()
            if prev_log and prev_log.message_id:
                in_reply_to = prev_log.message_id
                references = prev_log.message_id
                thread_id = prev_log.gmail_thread_id
                # Reuse the ORIGINAL thread's subject so Gmail groups it
                orig_subject = prev_log.subject or subject
                if orig_subject.lower().startswith('re:'):
                    subject = orig_subject
                else:
                    subject = f'Re: {orig_subject}'

        try:
            new_message_id, new_thread_id = _send_email(account, lead.email, subject, plain, html, sender_name, in_reply_to, references, thread_id)
            advance_campaign_lead_step(cl, now, steps)
            cl.assigned_account = account.email_address
            lead.assigned_account = account.email_address
            account.daily_sent_count += 1
            settings.next_allowed_send_at = now + timedelta(seconds=random.randint(60, 120))
            log = EmailLog(
                lead_id=lead.id, account_used=account.email_address, step=step,
                subject=subject, sent_at=now, log_type='campaign', status='sent',
                lead_email=lead.email, lead_name=lead.full_name, campaign_id=cl.campaign_id,
                tracking_token=tracking_token, message_id=new_message_id, gmail_thread_id=new_thread_id,
            )
            db.session.add(log)
            db.session.commit()
            return {'sent': 1, 'skipped': 0, 'errors': [], 'lead': lead.email, 'step': step, 'account': account.email_address}
        except Exception as e:
            log = EmailLog(
                lead_id=lead.id, account_used=account.email_address, step=step,
                subject=subject, sent_at=now, log_type='campaign', status='failed',
                lead_email=lead.email, lead_name=lead.full_name, campaign_id=cl.campaign_id,
            )
            db.session.add(log)
            db.session.commit()
            fail_count = EmailLog.query.filter_by(
                lead_id=lead.id, step=step, status='failed'
            ).count()
            if fail_count >= 3:
                cl.finished = True
                db.session.commit()
            return {'sent': 0, 'skipped': 1, 'errors': [f'{lead.email}: {str(e)}'], 'reason': 'send_failed'}

    # Fallback: legacy Lead.sequence_step path
    return {'sent': 0, 'skipped': 1, 'errors': [], 'reason': 'no_lead'}

    accounts = get_available_accounts(settings)
    account = pick_account(accounts, lead)
    if not account:
        return {
            'sent': 0, 'skipped': 1,
            'errors': ['No email accounts available or daily limit reached.'],
            'reason': 'no_account',
        }

    step = lead.sequence_step + 1
    campaign = Campaign.query.get(lead.campaign_id) if lead.campaign_id else None
    subject_raw, body_raw = _pick_campaign_content(campaign, step)
    if subject_raw is None:
        template = (_pick_legacy_template(campaign, step) if campaign else None) or pick_template(step)
        if not template:
            return {'sent': 0, 'skipped': 1, 'errors': [f'No template for step {step}'], 'reason': 'no_template'}
        subject_raw = template.subject
        body_raw = template.body

    tracking_on = campaign.tracking_enabled if campaign else True
    tracking_token = secrets.token_hex(16) if tracking_on else None
    subject, plain, html = prepare_content(subject_raw, body_raw, lead, settings, step, tracking_on, tracking_token)
    sender_name = (settings.sender_name or '').strip() or 'Your Name'

    try:
        _send_email(account, lead.email, subject, plain, html, sender_name)
        advance_sequence_step(lead, now)
        lead.assigned_account = account.email_address
        account.daily_sent_count += 1
        settings.next_allowed_send_at = now + timedelta(seconds=random.randint(60, 120))
        log = EmailLog(
            lead_id=lead.id, account_used=account.email_address, step=step,
            subject=subject, sent_at=now, log_type='campaign', status='sent',
            lead_email=lead.email, lead_name=lead.full_name,
            campaign_id=lead.campaign_id,
            tracking_token=tracking_token,
        )
        db.session.add(log)
        db.session.commit()
        return {'sent': 1, 'skipped': 0, 'errors': [], 'lead': lead.email, 'step': step, 'account': account.email_address, 'path': 'legacy'}
    except Exception as e:
        log = EmailLog(
            lead_id=lead.id, account_used=account.email_address, step=step,
            subject=subject, sent_at=now, log_type='campaign', status='failed',
            lead_email=lead.email, lead_name=lead.full_name, campaign_id=lead.campaign_id,
        )
        db.session.add(log)
        db.session.commit()
        return {'sent': 0, 'skipped': 1, 'errors': [f'{lead.email}: {str(e)}'], 'reason': 'send_failed'}


WARMUP_SUBJECTS = ['Quick question', 'Checking in', 'Hey', 'Following up', 'Hello there']
WARMUP_BODIES = [
    'Hope you are doing well!',
    'Just wanted to check in quickly.',
    'Let me know if you got my last message.',
    'Thanks for your time.',
    'Have a great day.',
]


def try_send_warmup_email() -> dict:
    settings = _get_fresh_settings()
    warmup_addrs = [a.strip() for a in (settings.warmup_addresses or '').split(',') if a.strip()]
    if not warmup_addrs:
        return {'sent': 0, 'errors': ['No warmup addresses configured.']}

    accounts = EmailAccount.query.filter_by(warmup_enabled=True).all()
    sent = 0
    errors = []

    for account in accounts:
        account.reset_daily_if_needed()
        cap = min(2 + (account.warmup_day - 1) * 2, 10)
        if account.daily_sent_count >= cap:
            continue
        to_addr = random.choice(warmup_addrs)
        subject = random.choice(WARMUP_SUBJECTS)
        body = random.choice(WARMUP_BODIES)
        try:
            _send_email(account, to_addr, subject, body, '', settings.sender_name or '')
            account.daily_sent_count += 1
            log = EmailLog(
                account_used=account.email_address,
                step=0, subject=subject,
                sent_at=datetime.utcnow(),
                log_type='warmup', status='sent',
                lead_email=to_addr, lead_name='Warmup',
            )
            db.session.add(log)
            sent += 1
        except Exception as e:
            errors.append(f'{account.email_address}: {str(e)}')

    db.session.commit()
    return {'sent': sent, 'errors': errors}


def preview_template(template_id: int) -> dict:
    settings = _get_fresh_settings()
    template = Template.query.get(template_id)
    if not template:
        return {'error': 'Template not found'}

    class SampleLead:
        first_name = 'John'
        last_name = 'Doe'
        company = 'Acme Corp'
        id = 0
        pitch_text = settings.pitch_text or ''

    subject, plain, html = prepare_content(
        template.subject, template.body, SampleLead(), settings, template.step, False
    )
    return {'subject': subject, 'body': html or plain, 'is_html': bool(html)}


def preview_step_content(subject_raw: str, body_raw: str, step: int = 1) -> dict:
    settings = _get_fresh_settings()

    class SampleLead:
        first_name = 'John'
        last_name = 'Doe'
        company = 'Acme Corp'
        id = 0
        pitch_text = settings.pitch_text or ''

    subject, plain, html = prepare_content(subject_raw, body_raw, SampleLead(), settings, step, False)
    return {'subject': subject, 'body': html or plain}
