import random
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import or_

from models import Campaign, CampaignLead, EmailAccount, Lead, Settings, db


def is_within_send_window(settings: Settings, now_utc: datetime) -> bool:
    try:
        tz = ZoneInfo(settings.timezone or 'UTC')
    except Exception:
        tz = ZoneInfo('UTC')
    local = now_utc.replace(tzinfo=timezone.utc).astimezone(tz)
    day_abbr = local.strftime('%a')
    active = [d.strip() for d in (settings.active_days or '').split(',') if d.strip()]
    if active and day_abbr not in active:
        return False
    try:
        start_parts = settings.send_window_start.split(':')
        end_parts = settings.send_window_end.split(':')
        start = local.replace(
            hour=int(start_parts[0]), minute=int(start_parts[1]), second=0, microsecond=0
        )
        end = local.replace(
            hour=int(end_parts[0]), minute=int(end_parts[1]), second=0, microsecond=0
        )
    except (ValueError, IndexError):
        return True
    return start <= local <= end


def pick_next_campaign_lead(now: datetime) -> 'CampaignLead | None':
    from models import EmailLog

    # Pehle non-failed leads try karo
    failed_lead_ids = db.session.query(EmailLog.lead_id).filter_by(
        status='failed'
    ).distinct().subquery()

    result = (
        CampaignLead.query
        .join(Campaign, CampaignLead.campaign_id == Campaign.id)
        .join(Lead, CampaignLead.lead_id == Lead.id)
        .filter(
            CampaignLead.finished.is_(False),
            CampaignLead.replied.is_(False),
            Campaign.status == 'active',
            Lead.unsubscribed.is_(False),
            Lead.replied.is_(False),
            Lead.paused.is_(False),
            or_(CampaignLead.next_send_at.is_(None), CampaignLead.next_send_at <= now),
            Lead.id.notin_(failed_lead_ids),
        )
        .order_by(CampaignLead.id)
        .first()
    )

    if result:
        return result

    # Saari normal leads khatam — ab failed leads retry karo
    return (
        CampaignLead.query
        .join(Campaign, CampaignLead.campaign_id == Campaign.id)
        .join(Lead, CampaignLead.lead_id == Lead.id)
        .filter(
            CampaignLead.finished.is_(False),
            CampaignLead.replied.is_(False),
            Campaign.status == 'active',
            Lead.unsubscribed.is_(False),
            Lead.replied.is_(False),
            Lead.paused.is_(False),
            or_(CampaignLead.next_send_at.is_(None), CampaignLead.next_send_at <= now),
            Lead.id.in_(failed_lead_ids),
        )
        .order_by(CampaignLead.id)
        .first()
    )


def advance_campaign_lead_step(cl: 'CampaignLead', now: datetime, steps: list):
    """Advance cl to the next step after a successful send.

    steps: parsed campaign.get_steps() list
    """
    total_steps = len(steps) if steps else 3
    cl.last_sent_at = now
    cl.sequence_step += 1

    if cl.sequence_step >= total_steps:
        cl.finished = True
        cl.next_send_at = None
    else:
        # wait_days from the NEXT step's definition
        next_step_data = steps[cl.sequence_step] if cl.sequence_step < len(steps) else {}
        wait_days = max(0, int(next_step_data.get('wait_days', 3)))
        cl.next_send_at = now + timedelta(days=wait_days)


def get_available_accounts(settings: Settings) -> list:
    limit = settings.daily_limit_per_account or 15
    accounts = EmailAccount.query.all()
    available = []
    for acc in accounts:
        acc.reset_daily_if_needed()
        if acc.daily_sent_count < limit:
            available.append(acc)
    if not available and accounts:
        for acc in accounts:
            acc.daily_sent_count = 0
        db.session.commit()
        available = list(accounts)
    return available


def pick_account(accounts: list, lead: Lead) -> 'EmailAccount | None':
    if not accounts:
        return None
    if lead.assigned_account:
        for acc in accounts:
            if acc.email_address == lead.assigned_account:
                return acc
    return min(accounts, key=lambda acc: acc.daily_sent_count)


# ── Legacy pick_next_lead kept for backwards compat (not used by scheduler) ──

def pick_next_lead(now: datetime) -> 'Lead | None':
    """Legacy — only used if CampaignLead table is empty or missing."""
    return (
        Lead.query.outerjoin(Campaign, Lead.campaign_id == Campaign.id)
        .filter(
            Lead.unsubscribed.is_(False),
            Lead.replied.is_(False),
            Lead.paused.is_(False),
            Lead.sequence_step < 3,
            or_(Lead.next_send_at.is_(None), Lead.next_send_at <= now),
            or_(Lead.campaign_id.is_(None), Campaign.status == 'active'),
        )
        .order_by(Lead.id)
        .first()
    )


def advance_sequence_step(lead: Lead, now: datetime):
    """Legacy — kept for backwards compat."""
    lead.last_sent_at = now
    if lead.sequence_step == 0:
        lead.sequence_step = 1
        lead.next_send_at = now + timedelta(days=3)
    elif lead.sequence_step == 1:
        lead.sequence_step = 2
        lead.next_send_at = now + timedelta(days=4)
    elif lead.sequence_step == 2:
        lead.sequence_step = 3
        lead.next_send_at = None


DEFAULT_TEMPLATES = {
    1: {
        'subject': 'Quick question about {company}',
        'body': (
            'Hi {first_name},\n\n'
            'I came across {company} and wanted to reach out personally.\n\n'
            '{pitch}\n\n'
            'Would it make sense to connect this week?\n\n'
            '{sender_name}'
        ),
        'is_html': False,
    },
    2: {
        'subject': 'Following up, {first_name}',
        'body': (
            'Hi {first_name},\n\n'
            'Just wanted to follow up on my last email.\n\n'
            'Here is a link I wanted to share: {video_link}\n\n'
            'Still think there could be a good fit here.\n\n'
            '{sender_name}'
        ),
        'is_html': True,
    },
    3: {
        'subject': 'Closing the loop',
        'body': (
            "Hi {first_name},\n\n"
            "Just closing the loop — no worries if the timing isn't right.\n\n"
            "Feel free to reach out whenever.\n\n"
            "{sender_name}"
        ),
        'is_html': False,
    },
}


def pick_template(step: int):
    from models import Template

    templates = Template.query.filter_by(step=step).all()
    if templates:
        return random.choice(templates)
    defaults = DEFAULT_TEMPLATES.get(step)
    if defaults:
        t = Template(
            name=f'Default Step {step}',
            step=step,
            subject=defaults['subject'],
            body=defaults['body'],
            is_html=defaults['is_html'],
        )
        return t
    return None
