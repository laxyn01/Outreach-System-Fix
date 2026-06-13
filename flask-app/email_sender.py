import random
import smtplib
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from models import EmailAccount, EmailLog, Lead, Settings, Template, db
from sequence import (
    advance_sequence_step,
    get_available_accounts,
    is_within_send_window,
    pick_account,
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


def prepare_content(template, lead, settings, step: int):
    sender_name = settings.sender_name or 'Your Name'
    video_link = settings.video_link_url or ''
    base_url = settings.tracking_base_url or 'http://localhost:5000'

    lead.pitch_text = settings.pitch_text or ''

    # Fix #2: parse_spintax FIRST on subject and body, then replace placeholders
    subject = parse_spintax(template.subject or '')
    body = parse_spintax(template.body or '')

    subject = replace_placeholders(subject, lead, sender_name, video_link)
    body = replace_placeholders(body, lead, sender_name, video_link)

    is_html = template.is_html or step == 2
    if step == 2 and video_link:
        link_html = f'<a href="{video_link}">{video_link}</a>'
        body = body.replace(video_link, link_html)

    plain, html = ensure_html_wrapper(body, is_html)
    if html:
        html = wrap_links(html, lead.id, step, base_url)
        html = inject_tracking_pixel(html, lead.id, step, base_url)

    plain, html = append_unsubscribe(plain, html, lead.id, base_url)
    return subject, plain, html, is_html


def send_smtp(account: EmailAccount, to_email: str, subject: str, plain: str, html: str):
    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    msg['From'] = account.email_address
    msg['To'] = to_email
    if plain:
        msg.attach(MIMEText(plain, 'plain'))
    if html:
        msg.attach(MIMEText(html, 'html'))

    # Fix #4: ehlo → starttls → ehlo pattern
    with smtplib.SMTP(account.smtp_host, account.smtp_port) as server:
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.login(account.email_address, account.app_password)
        server.sendmail(account.email_address, to_email, msg.as_string())


def send_test_email(account: EmailAccount) -> dict:
    try:
        subject = 'SMTP Test — OutreachCommand'
        plain = f'This is a test email from OutreachCommand sent at {datetime.utcnow().isoformat()} UTC'
        send_smtp(account, account.email_address, subject, plain, '')
        return {'ok': True, 'message': 'Test email sent successfully.'}
    except Exception as e:
        return {'ok': False, 'message': str(e)}


def try_send_next_email() -> dict:
    settings = Settings.get_singleton()
    now = datetime.utcnow()

    # Fix #3: rate limit check
    if settings.next_allowed_send_at and now < settings.next_allowed_send_at:
        wait_secs = int((settings.next_allowed_send_at - now).total_seconds())
        return {
            'sent': 0,
            'skipped': 1,
            'errors': [],
            'reason': 'rate_limit',
            'wait_seconds': wait_secs,
        }

    if not is_within_send_window(settings, now):
        return {
            'sent': 0,
            'skipped': 1,
            'errors': [],
            'reason': 'outside_window',
        }

    # Fix #3: pick ONE lead
    lead = pick_next_lead(now)
    if not lead:
        return {'sent': 0, 'skipped': 1, 'errors': [], 'reason': 'no_lead'}

    # Fix #3: pick account with daily_sent_count < limit
    accounts = get_available_accounts(settings)
    account = pick_account(accounts, lead)
    if not account:
        return {
            'sent': 0,
            'skipped': 1,
            'errors': ['No email accounts available or daily limit reached.'],
            'reason': 'no_account',
        }

    step = lead.sequence_step + 1
    template = pick_template(step)
    if not template:
        return {
            'sent': 0,
            'skipped': 1,
            'errors': [f'No template for step {step}'],
            'reason': 'no_template',
        }

    subject, plain, html, _ = prepare_content(template, lead, settings, step)
    errors = []

    try:
        send_smtp(account, lead.email, subject, plain, html)
        # Fix #3: update sequence_step, next_send_at, next_allowed_send_at
        advance_sequence_step(lead, now)
        lead.assigned_account = account.email_address
        account.daily_sent_count += 1
        # Fix #3: next_allowed_send_at = now + random 60-120 seconds
        settings.next_allowed_send_at = now + timedelta(seconds=random.randint(60, 120))

        log = EmailLog(
            lead_id=lead.id,
            account_used=account.email_address,
            step=step,
            subject=subject,
            sent_at=now,
            log_type='campaign',
            status='sent',
            lead_email=lead.email,
            lead_name=lead.full_name,
        )
        db.session.add(log)
        db.session.commit()
        return {
            'sent': 1,
            'skipped': 0,
            'errors': [],
            'lead': lead.email,
            'step': step,
            'account': account.email_address,
        }
    except Exception as e:
        errors.append(f'{lead.email}: {str(e)}')
        log = EmailLog(
            lead_id=lead.id,
            account_used=account.email_address,
            step=step,
            subject=subject,
            sent_at=now,
            log_type='campaign',
            status='failed',
            lead_email=lead.email,
            lead_name=lead.full_name,
        )
        db.session.add(log)
        db.session.commit()
        return {'sent': 0, 'skipped': 1, 'errors': errors, 'reason': 'send_failed'}


WARMUP_SUBJECTS = ['Quick question', 'Checking in', 'Hey', 'Following up', 'Hello']
WARMUP_BODIES = [
    'Hope you are doing well!',
    'Just wanted to check in quickly.',
    'Let me know if you got my last message.',
    'Thanks!',
    'Have a great day.',
]


def try_send_warmup_email() -> dict:
    settings = Settings.get_singleton()
    warmup_addrs = [
        a.strip()
        for a in (settings.warmup_addresses or '').split(',')
        if a.strip()
    ]
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
            send_smtp(account, to_addr, subject, body, '')
            account.daily_sent_count += 1
            log = EmailLog(
                account_used=account.email_address,
                step=0,
                subject=subject,
                sent_at=datetime.utcnow(),
                log_type='warmup',
                status='sent',
                lead_email=to_addr,
                lead_name='Warmup',
            )
            db.session.add(log)
            sent += 1
        except Exception as e:
            errors.append(f'{account.email_address}: {str(e)}')

    db.session.commit()
    return {'sent': sent, 'errors': errors}


def preview_template(template_id: int) -> dict:
    settings = Settings.get_singleton()
    template = Template.query.get(template_id)
    if not template:
        return {'error': 'Template not found'}

    class SampleLead:
        first_name = 'John'
        last_name = 'Doe'
        company = 'Acme Corp'
        id = 0
        pitch_text = settings.pitch_text or ''

    lead = SampleLead()
    subject, plain, html, is_html = prepare_content(template, lead, settings, template.step)
    return {
        'subject': subject,
        'body': html if is_html else plain,
        'is_html': is_html,
    }
