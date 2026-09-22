"""
warmup.py — self-contained email warmup system for OutreachCommand.

Design rules (do not break these):
  * Warmup NEVER emails a real Lead. Recipients are always other rows in
    email_accounts that have warmup_enabled = True.
  * Warmup NEVER touches EmailAccount.daily_sent_count. Warmup volume is
    counted in EmailAccount.warmup_sent_today.
  * Warmup NEVER touches Settings.next_allowed_send_at (the real campaign's
    global rate limiter). It uses per-account
    EmailAccount.warmup_next_allowed_send_at instead.
  * Warmup reuses email_sender._send_email() (OAuth + SMTP dispatcher) and
    imap_replies._imap_login() (XOAUTH2 + app-password, auto token refresh).
  * Every warmup EmailLog row is written with log_type='warmup' and
    lead_id=None so it can never appear in campaign analytics / leads / logs.
  * Failed warmup sends are logged with status='warmup_failed' (NOT 'failed').
    See the note on pick_next_campaign_lead() at the bottom of this file.
"""

import imaplib
import email as email_lib
import random
import time
import uuid
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from zoneinfo import ZoneInfo

from models import EmailAccount, EmailLog, Settings, WarmupEmail, db

# Reuse existing infrastructure — never reimplement send / login logic.
# _load_oauth_credentials is the SAME credential construction + refresh block
# that send_gmail_api() uses; it was extracted into email_sender.py so this
# module can share it rather than diverge from it.
from email_sender import _load_oauth_credentials, _send_email
from imap_replies import _imap_login


# ─── Tunables ────────────────────────────────────────────────────────────────

WARMUP_START_VOLUME = 2        # emails/day on warmup_day 1
WARMUP_DEFAULT_TARGET = 18     # emails/day once fully ramped
WARMUP_RAMP_DAYS = 21          # ~3 weeks from start volume to target

WARMUP_WINDOW_START_HOUR = 7   # local hour (Settings.timezone) warmup may start
WARMUP_WINDOW_END_HOUR = 21    # local hour warmup stops

MIN_GAP_SECONDS = 180          # never two sends from one account inside 3 min
MAX_GAP_SECONDS = 2700         # never wait more than 45 min between sends
FAILURE_BACKOFF_SECONDS = (900, 2700)   # 15–45 min after a failed warmup send

OPEN_DELAY_SECONDS = (30 * 60, 4 * 60 * 60)      # 30 min – 4 h
REPLY_DELAY_SECONDS = (60 * 60, 8 * 60 * 60)     # 1 h – 8 h

REPLY_PROBABILITY = 0.55       # not every warmup email gets a reply

# Open-simulation behaviour
OPEN_BATCH_LIMIT = 25          # rows considered per process_warmup_opens() run
READ_EMULATION_DELAY = (2, 8)  # seconds paused after each simulated read
OPEN_JOB_TIME_BUDGET = 120     # hard ceiling per run; job interval is 300s
IMPORTANT_PROBABILITY = 0.5    # ~40-60% of warmup mail gets flagged Important
IMAP_HOST = 'imap.gmail.com'
IMAP_PORT = 993
SPAM_FOLDERS = ('[Gmail]/Spam', '[Google Mail]/Spam', 'Spam')

WARMUP_REF_PREFIX = 'WU-'      # body marker, also lets you filter in Gmail


# ─── Content pool (14 openers, 12 replies) ───────────────────────────────────

WARMUP_TEMPLATES = [
    ("Quick note",
     "Hey {name},\n\nJust a quick note before I forget — nothing urgent on my "
     "end, I'll catch you later this week.\n\nThanks,\n{sender}"),
    ("Following up on that doc",
     "Hi {name},\n\nDid the version I sent over make sense? Happy to walk "
     "through it whenever you have ten minutes.\n\nBest,\n{sender}"),
    ("Notes from earlier",
     "Hi {name},\n\nWrote up the notes from earlier while they were still "
     "fresh. Let me know if I missed anything obvious.\n\nCheers,\n{sender}"),
    ("Are you around Thursday?",
     "Hey {name},\n\nAre you around Thursday afternoon? Would be good to sort "
     "out the last couple of bits then.\n\n{sender}"),
    ("Small update",
     "Hi {name},\n\nSmall update from my side: the second batch is done, so "
     "we're a bit ahead of where I thought we'd be.\n\nTalk soon,\n{sender}"),
    ("About the schedule",
     "Hi {name},\n\nI moved a couple of things around on the schedule. Nothing "
     "dramatic, but wanted you to hear it from me first.\n\nThanks,\n{sender}"),
    ("One thing I forgot",
     "Hey {name},\n\nOne thing I forgot to mention — the older file is out of "
     "date now, so ignore that one.\n\nBest,\n{sender}"),
    ("Checking something",
     "Hi {name},\n\nChecking something on my end and your name came up. Nothing "
     "you need to do, just keeping you in the loop.\n\n{sender}"),
    ("Sending this over",
     "Hi {name},\n\nSending this over now so it isn't sitting in my drafts all "
     "week. Read it whenever suits you.\n\nThanks,\n{sender}"),
    ("Thoughts when you get a sec",
     "Hey {name},\n\nWould value your thoughts on this when you get a second. "
     "No rush at all.\n\n{sender}"),
    ("Wrapping up this week",
     "Hi {name},\n\nWrapping up this week a little earlier than planned. If "
     "anything needs me before Friday, just say.\n\nBest,\n{sender}"),
    ("That thing we discussed",
     "Hi {name},\n\nAbout that thing we discussed — I had another look and I "
     "think the simpler option is the right call.\n\nCheers,\n{sender}"),
    ("Morning",
     "Morning {name},\n\nStarting on this today. Should have something worth "
     "showing you by tomorrow afternoon.\n\n{sender}"),
    ("Just so it's written down",
     "Hi {name},\n\nPutting this in writing so neither of us has to remember "
     "it: we agreed to keep the current setup for now.\n\nThanks,\n{sender}"),
]

WARMUP_REPLIES = [
    "Thanks {name}, this is helpful. I'll take a proper look tomorrow.",
    "Got it — makes sense to me. Nothing to add from my side.",
    "Appreciate the update. Let's pick it up later in the week.",
    "Perfect, thanks for flagging. I'll keep an eye on it.",
    "That works. I'll get back to you once I've read it properly.",
    "Thanks for sending this over — much clearer now.",
    "Noted, and agreed. No changes needed as far as I can tell.",
    "Good to know. I'll adjust things on my end accordingly.",
    "Thanks {name}. Nothing urgent here, so take your time.",
    "Sounds right to me. Happy to go with that.",
    "Cheers for the heads up. I'll follow up if anything changes.",
    "All clear on this side. Talk soon.",
]

SUBJECT_TWEAKS = ['', '', '', ' — quick one', ' (no rush)', ' 🙂']


# ─── Small helpers ───────────────────────────────────────────────────────────

def _display_name(email_address: str) -> str:
    """Turn user.name123@gmail.com into 'User'."""
    local = (email_address or '').split('@')[0]
    for sep in ('.', '_', '-', '+'):
        local = local.split(sep)[0]
    local = ''.join(ch for ch in local if ch.isalpha())
    return local.capitalize() or 'there'


def _local_now(settings, now_utc: datetime):
    try:
        tz = ZoneInfo(settings.timezone or 'UTC')
    except Exception:
        tz = ZoneInfo('UTC')
    return now_utc.replace(tzinfo=timezone.utc).astimezone(tz)


def _in_warmup_window(settings, now_utc: datetime) -> bool:
    """Warmup has its OWN window (7am–9pm local, every day). It deliberately
    does not reuse the campaign send window / active days."""
    local = _local_now(settings, now_utc)
    return WARMUP_WINDOW_START_HOUR <= local.hour < WARMUP_WINDOW_END_HOUR


def _seconds_left_in_window(settings, now_utc: datetime) -> int:
    local = _local_now(settings, now_utc)
    end = local.replace(hour=WARMUP_WINDOW_END_HOUR, minute=0, second=0, microsecond=0)
    if end <= local:
        return 0
    return int((end - local).total_seconds())


def _warmup_pool():
    """Accounts participating in warmup, in a stable order."""
    return (
        EmailAccount.query
        .filter(EmailAccount.warmup_enabled.is_(True))
        .order_by(EmailAccount.id)
        .all()
    )


def _ramp_volume(account: EmailAccount) -> int:
    """Linear ramp from WARMUP_START_VOLUME to the account's target."""
    day = int(account.warmup_day or 1)
    day = max(1, min(day, 60))
    target = int(account.warmup_target_daily or WARMUP_DEFAULT_TARGET)
    target = max(WARMUP_START_VOLUME, target)
    if day >= WARMUP_RAMP_DAYS:
        volume = float(target)
    else:
        span = float(WARMUP_RAMP_DAYS - 1) or 1.0
        volume = WARMUP_START_VOLUME + (target - WARMUP_START_VOLUME) * ((day - 1) / span)
    volume = int(round(volume))
    if volume > 3:
        volume += random.randint(-1, 1)      # day-to-day jitter, never robotic
    return max(1, volume)


def _todays_goal(account: EmailAccount) -> int:
    """Pick (once per day) how many warmup emails this account will send."""
    if not account.warmup_daily_goal:
        account.warmup_daily_goal = _ramp_volume(account)
    return int(account.warmup_daily_goal)


def _schedule_next_send(account: EmailAccount, settings, now: datetime):
    """Spread the remaining sends over the rest of the warmup window, with
    heavy jitter, so sends never arrive as a burst."""
    goal = _todays_goal(account)
    remaining = max(1, goal - int(account.warmup_sent_today or 0))
    secs_left = _seconds_left_in_window(settings, now)
    if secs_left <= 0:
        interval = MAX_GAP_SECONDS
    else:
        interval = secs_left / float(remaining)
    interval = max(MIN_GAP_SECONDS, min(interval, MAX_GAP_SECONDS))
    interval = interval * random.uniform(0.5, 1.25)   # mean < 1 so the day finishes in-window
    account.warmup_next_allowed_send_at = now + timedelta(seconds=int(interval))


def _ref_line(token: str) -> str:
    return f'\n\n--\nref: {WARMUP_REF_PREFIX}{token}'


def _compose_opener(sender: EmailAccount, recipient: EmailAccount, token: str):
    subject, body = random.choice(WARMUP_TEMPLATES)
    subject = subject + random.choice(SUBJECT_TWEAKS)
    body = body.format(
        name=_display_name(recipient.email_address),
        sender=_display_name(sender.email_address),
    )
    return subject, body + _ref_line(token)


def _compose_reply(replier: EmailAccount, original_sender: EmailAccount, token: str):
    body = random.choice(WARMUP_REPLIES).format(
        name=_display_name(original_sender.email_address)
    )
    body = f'{body}\n\n{_display_name(replier.email_address)}'
    return body + _ref_line(token)


def _log_warmup(account_email: str, to_email: str, subject: str, now: datetime,
                status: str, message_id=None, thread_id=None, error=None):
    """Mirror row in email_logs. log_type='warmup' + lead_id=None keeps it out
    of every existing page (they all filter log_type='campaign')."""
    log = EmailLog(
        lead_id=None,
        account_used=account_email,
        step=0,
        subject=subject,
        sent_at=now,
        log_type='warmup',
        status=status,
        lead_email=to_email,
        lead_name='Warmup',
        campaign_id=None,
        tracking_token=None,
        message_id=message_id,
        gmail_thread_id=thread_id,
        error_message=error,
    )
    db.session.add(log)
    return log


# ─── 1. Sending warmup emails ────────────────────────────────────────────────

def _pick_recipient(sender: EmailAccount, pool: list, now: datetime):
    """Randomized pairing, biased towards whoever has received least today."""
    candidates = [a for a in pool if a.id != sender.id]
    if not candidates:
        return None
    since = now - timedelta(hours=24)
    counts = {}
    for acc in candidates:
        counts[acc.id] = WarmupEmail.query.filter(
            WarmupEmail.recipient_account_id == acc.id,
            WarmupEmail.sent_at >= since,
        ).count()
    candidates.sort(key=lambda a: counts.get(a.id, 0))
    half = max(1, len(candidates) // 2)
    return random.choice(candidates[:half])


def _send_one_warmup(sender: EmailAccount, recipient: EmailAccount, settings, now: datetime):
    token = uuid.uuid4().hex[:12]
    subject, body = _compose_opener(sender, recipient, token)
    sender_name = _display_name(sender.email_address)

    try:
        message_id, thread_id = _send_email(
            sender,
            recipient.email_address,
            subject,
            body,
            '',                      # plain text only — no tracking pixel, no unsubscribe
            sender_name,
        )
    except Exception as e:
        err = str(e)
        db.session.add(WarmupEmail(
            sender_account_id=sender.id,
            recipient_account_id=recipient.id,
            sender_email=sender.email_address,
            recipient_email=recipient.email_address,
            subject=subject,
            token=token,
            sent_at=now,
            status='failed',
            error_message=err,
            is_reply=False,
        ))
        # status='warmup_failed', NOT 'failed' — see note at bottom of file.
        _log_warmup(sender.email_address, recipient.email_address, subject, now,
                    'warmup_failed', error=err)
        sender.warmup_next_allowed_send_at = now + timedelta(
            seconds=random.randint(*FAILURE_BACKOFF_SECONDS)
        )
        db.session.commit()
        return False, err

    wu = WarmupEmail(
        sender_account_id=sender.id,
        recipient_account_id=recipient.id,
        sender_email=sender.email_address,
        recipient_email=recipient.email_address,
        subject=subject,
        token=token,
        message_id=message_id,
        gmail_thread_id=thread_id,
        sent_at=now,
        status='sent',
        is_reply=False,
        open_due_at=now + timedelta(seconds=random.randint(*OPEN_DELAY_SECONDS)),
        reply_scheduled=False,
        reply_sent=False,
    )
    db.session.add(wu)

    # WARMUP COUNTER ONLY — daily_sent_count is untouched here on purpose.
    sender.warmup_sent_today = int(sender.warmup_sent_today or 0) + 1
    _schedule_next_send(sender, settings, now)

    _log_warmup(sender.email_address, recipient.email_address, subject, now,
                'sent', message_id=message_id, thread_id=thread_id)
    db.session.commit()
    return True, None


def send_warmup_round(max_sends: int = 1) -> dict:
    """Scheduled job #1 — send at most `max_sends` warmup emails per run."""
    now = datetime.utcnow()
    settings = Settings.get_singleton()

    pool = _warmup_pool()
    if pool:
        for acc in pool:
            acc.reset_warmup_daily_if_needed()
        db.session.commit()

    if len(pool) < 2:
        return {'sent': 0, 'skipped': 1, 'reason': 'need_at_least_two_warmup_accounts', 'errors': []}

    if not _in_warmup_window(settings, now):
        return {'sent': 0, 'skipped': 1, 'reason': 'outside_warmup_window', 'errors': []}

    ready = []
    for acc in pool:
        if acc.is_paused_auto:
            continue          # account health paused it — still a valid recipient
        if int(acc.warmup_sent_today or 0) >= _todays_goal(acc):
            continue
        if acc.warmup_next_allowed_send_at and now < acc.warmup_next_allowed_send_at:
            continue
        ready.append(acc)
    db.session.commit()   # persist any goals assigned by _todays_goal()

    if not ready:
        return {'sent': 0, 'skipped': 1, 'reason': 'no_account_due', 'errors': []}

    random.shuffle(ready)
    sent = 0
    errors = []
    for sender in ready:
        if sent >= max_sends:
            break
        recipient = _pick_recipient(sender, pool, now)
        if not recipient:
            continue
        ok, err = _send_one_warmup(sender, recipient, settings, now)
        if ok:
            sent += 1
        else:
            errors.append(f'{sender.email_address}: {err}')
    return {'sent': sent, 'skipped': 0, 'reason': 'ok', 'errors': errors}


# ─── 1b. Gmail API helpers (OAuth accounts only) ─────────────────────────────
#
# For auth_type == 'oauth' we drive Gmail directly instead of IMAP. This is
# closer to the real user actions we are imitating:
#   "Report not spam"  -> messages.modify removeLabelIds SPAM, addLabelIds INBOX
#   "read the message" -> messages.modify removeLabelIds UNREAD
#   "mark important"   -> messages.modify addLabelIds IMPORTANT
# SMTP / app-password accounts keep the original IMAP paths untouched.

_SCOPE_WARNED = set()


def _gmail_service(account: EmailAccount):
    """Build a Gmail API client using the SHARED credential loader."""
    from googleapiclient.discovery import build
    creds = _load_oauth_credentials(account)
    return build('gmail', 'v1', credentials=creds)


def _is_scope_error(exc) -> bool:
    text = str(exc).lower()
    return (
        'insufficientpermissions' in text
        or 'insufficient authentication scopes' in text
        or 'access_token_scope_insufficient' in text
        or 'request had insufficient authentication scopes' in text
    )


def _warn_scope_once(account: EmailAccount, exc):
    """Gmail write operations need the gmail.modify scope. Accounts connected
    with only gmail.readonly cannot modify labels (and cannot write over IMAP
    either). Warn once per account per process instead of spamming the log."""
    if account.id in _SCOPE_WARNED:
        return
    _SCOPE_WARNED.add(account.id)
    print(
        f'[WARMUP][SCOPE] {account.email_address}: Gmail rejected a label change '
        f'because the stored OAuth token lacks '
        f'https://www.googleapis.com/auth/gmail.modify. Reconnect this account '
        f'after adding that scope in app.py; until then spam-rescue, mark-read '
        f'and mark-important are skipped for it. ({exc})',
        flush=True,
    )


def _gmail_find_ids(service, rfc_message_id: str):
    """Map an RFC822 Message-ID header to Gmail's internal message id(s)."""
    if not rfc_message_id:
        return []
    clean = rfc_message_id.strip().strip('<>')
    if not clean:
        return []
    resp = service.users().messages().list(
        userId='me', q=f'rfc822msgid:{clean}', maxResults=5,
    ).execute()
    return [m['id'] for m in resp.get('messages', []) if m.get('id')]


def _gmail_modify(service, gmail_id: str, add=None, remove=None) -> bool:
    body = {}
    if add:
        body['addLabelIds'] = list(add)
    if remove:
        body['removeLabelIds'] = list(remove)
    if not body:
        return False
    service.users().messages().modify(userId='me', id=gmail_id, body=body).execute()
    return True


def _rescue_from_spam_gmail_api(account: EmailAccount, peer_addresses) -> int:
    """OAuth equivalent of _rescue_from_spam(): the real 'Report not spam'.

    IMPORTANT: errors are NOT swallowed here. Any exception (a missing-scope
    error or anything else) is warned-about-if-relevant and then RE-RAISED so
    it propagates to scan_warmup_inboxes(), whose except block is what runs
    the original IMAP copy+delete fallback for this account. An earlier
    version of this function caught scope errors internally and returned the
    partial count instead of raising, which meant that except block never
    fired and the fallback silently never ran. Do not reintroduce a bare
    `except Exception: ... return` here.
    """
    service = _gmail_service(account)
    rescued = 0
    try:
        for addr in peer_addresses:
            resp = service.users().messages().list(
                userId='me', q=f'in:spam from:{addr} newer_than:2d', maxResults=25,
            ).execute()
            for m in resp.get('messages', []):
                _gmail_modify(service, m['id'], add=['INBOX'], remove=['SPAM'])
                rescued += 1
    except Exception as e:
        if _is_scope_error(e):
            _warn_scope_once(account, e)
        raise
    return rescued


def _open_via_gmail_api(service, account: EmailAccount, message_id: str,
                        mark_important: bool):
    """Mark read (and optionally Important) through the Gmail API.

    Returns (seen_ok, important_applied).
    """
    try:
        ids = _gmail_find_ids(service, message_id)
    except Exception as e:
        if _is_scope_error(e):
            _warn_scope_once(account, e)
        return False, False
    if not ids:
        return False, False

    add = ['IMPORTANT'] if mark_important else None
    seen_ok = False
    important_ok = False
    for gmail_id in ids:
        try:
            _gmail_modify(service, gmail_id, add=add, remove=['UNREAD'])
            seen_ok = True
            important_ok = bool(mark_important)
        except Exception as e:
            if _is_scope_error(e):
                _warn_scope_once(account, e)
                return False, False
    return seen_ok, important_ok


# ─── 2. Deferred open simulation ─────────────────────────────────────────────
#
# NOTE ON "MARK AS IMPORTANT" FOR SMTP / APP-PASSWORD ACCOUNTS:
# There is no reliable way to set Gmail's Important marker over plain IMAP.
# IMAP's \\Flagged maps to Gmail's STAR, which is a different signal, and the
# Important marker is not exposed as a settable IMAP flag. Copying into
# "[Gmail]/Important" is undocumented and depends on that label being enabled
# for IMAP in the account's settings, so it is not dependable. Rather than ship
# no-op or fake code, Important is simply SKIPPED for auth_type == 'smtp'
# accounts and counted in the 'important_skipped_smtp' return value.

def _mark_seen_by_message_id(mail, message_id: str) -> bool:
    if not message_id:
        return False
    try:
        mail.select('INBOX')
        typ, data = mail.search(None, f'(HEADER Message-ID "{message_id}")')
        if typ != 'OK' or not data or not data[0]:
            return False
        for num in data[0].split():
            try:
                mail.store(num, '+FLAGS', '\\Seen')
            except Exception:
                pass
        return True
    except Exception:
        return False


def process_warmup_opens() -> dict:
    """Scheduled job #2 — open warmup emails that became due.

    OAuth accounts are driven through the Gmail API (remove UNREAD, and add
    IMPORTANT for a random subset). SMTP accounts keep the original IMAP
    \\Seen path. Each row is followed by a short randomized pause so a batch
    never fires as identical back-to-back actions. The whole run is capped by
    OPEN_JOB_TIME_BUDGET; anything left over stays due for the next run.
    """
    now = datetime.utcnow()
    due = (
        WarmupEmail.query
        .filter(
            WarmupEmail.status == 'sent',
            WarmupEmail.opened_at.is_(None),
            WarmupEmail.open_due_at.isnot(None),
            WarmupEmail.open_due_at <= now,
            WarmupEmail.sent_at >= now - timedelta(days=3),
        )
        .order_by(WarmupEmail.open_due_at)
        .limit(OPEN_BATCH_LIMIT)
        .all()
    )
    if not due:
        return {'opened': 0, 'errors': []}

    grouped = {}
    for row in due:
        grouped.setdefault(row.recipient_account_id, []).append(row)

    opened = 0
    important = 0
    important_skipped_smtp = 0
    deferred = 0
    errors = []
    job_started = time.monotonic()
    out_of_time = False

    for account_id, rows in grouped.items():
        if out_of_time:
            deferred += len(rows)
            continue

        account = EmailAccount.query.get(account_id) if account_id else None
        use_api = bool(account and account.auth_type == 'oauth' and account.oauth_token
                       and getattr(account, 'provider', 'gmail') == 'gmail')
        service = None
        mail = None

        if use_api:
            try:
                service = _gmail_service(account)
            except Exception as e:
                use_api = False
                errors.append(f'{account.email_address}: gmail api unavailable: {e}')

        if account is not None and not use_api:
            try:
                mail = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
                _imap_login(mail, account)
            except Exception as e:
                mail = None
                errors.append(f'{account.email_address}: {e}')

        for row in rows:
            # Hard time ceiling so this job can never overrun its interval.
            if time.monotonic() - job_started > OPEN_JOB_TIME_BUDGET:
                out_of_time = True
                deferred += 1
                continue

            mark_important = random.random() < IMPORTANT_PROBABILITY
            
            marked_seen = True   # SMTP/IMAP path assumed successful unless api path fails

            if use_api:
                seen_ok, important_ok = _open_via_gmail_api(
                    service, account, row.message_id, mark_important
                )
                marked_seen = seen_ok
                if important_ok:
                    important += 1
            elif mail is not None:
                _mark_seen_by_message_id(mail, row.message_id)
                if mark_important:
                    # No dependable IMAP equivalent — see the note above.
                    important_skipped_smtp += 1

            if not marked_seen:
                continue   # scope/API failure — don't mark opened, retry next run

            row.opened_at = now

            row.opened_at = now
            log = EmailLog.query.filter_by(
                log_type='warmup', message_id=row.message_id
            ).first() if row.message_id else None
            if log:
                if not log.opened_at:
                    log.opened_at = now
                log.open_count = (log.open_count or 0) + 1
            opened += 1

            # Read-emulation pause: a human does not clear an inbox in 0 ms.
            if account is not None:
                time.sleep(random.uniform(*READ_EMULATION_DELAY))

        if mail is not None:
            try:
                mail.logout()
            except Exception:
                pass

    db.session.commit()
    return {
        'opened': opened,
        'important': important,
        'important_skipped_smtp': important_skipped_smtp,
        'deferred': deferred,
        'errors': errors,
    }


# ─── 3. IMAP scan — detect delivery and schedule replies ─────────────────────

def _header_value(msg, name: str) -> str:
    raw = msg.get(name)
    if not raw:
        return ''
    try:
        return str(make_header(decode_header(raw))).strip()
    except Exception:
        return str(raw).strip()


def _rescue_from_spam(mail, peer_addresses, since_str: str):
    """If a warmup email landed in Spam, move it to the inbox. This is the
    single highest-value thing a warmup system does for reputation."""
    for folder in SPAM_FOLDERS:
        try:
            typ, _ = mail.select(folder)
        except Exception:
            continue
        if typ != 'OK':
            continue
        for addr in peer_addresses:
            try:
                typ, data = mail.search(None, f'(FROM "{addr}" SINCE {since_str})')
                if typ != 'OK' or not data or not data[0]:
                    continue
                for num in data[0].split():
                    try:
                        mail.copy(num, 'INBOX')
                        mail.store(num, '+FLAGS', '\\Deleted')
                    except Exception:
                        pass
                try:
                    mail.expunge()
                except Exception:
                    pass
            except Exception:
                continue
        break


def _match_warmup_row(account: EmailAccount, message_id: str, subject: str,
                      from_addr: str, now: datetime):
    """Find the WarmupEmail row this received message corresponds to."""
    if message_id:
        row = WarmupEmail.query.filter_by(message_id=message_id).first()
        if row:
            return row
    # Fallback: Message-ID rewritten in transit — match on sender + subject.
    return (
        WarmupEmail.query
        .filter(
            WarmupEmail.recipient_account_id == account.id,
            WarmupEmail.sender_email == (from_addr or '').lower(),
            WarmupEmail.subject == subject,
            WarmupEmail.is_reply.is_(False),
            WarmupEmail.reply_scheduled.is_(False),
            WarmupEmail.sent_at >= now - timedelta(days=3),
        )
        .order_by(WarmupEmail.sent_at.desc())
        .first()
    )


def scan_warmup_inboxes() -> dict:
    """Scheduled job #3 — read each warmup inbox over IMAP, rescue anything
    from spam, and schedule a delayed reply for ORIGINAL warmup emails only."""
    now = datetime.utcnow()
    pool = _warmup_pool()
    if len(pool) < 2:
        return {'scanned': 0, 'scheduled': 0, 'errors': []}

    since_str = (now - timedelta(days=2)).strftime('%d-%b-%Y')
    scanned = 0
    scheduled = 0
    errors = []

    for account in pool:
        peers = [p for p in pool if p.id != account.id]
        peer_addresses = [p.email_address for p in peers]
        mail = None
        try:
            mail = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
            _imap_login(mail, account)
            # OAuth accounts get Gmail's real "Report not spam" action via the
            # API; SMTP / app-password accounts keep the original IMAP
            # copy+delete move, unchanged.
            if account.auth_type == 'oauth' and account.oauth_token:
                try:
                    _rescue_from_spam_gmail_api(account, peer_addresses)
                except Exception as e:
                    errors.append(
                        f'{account.email_address}: gmail spam-rescue failed: {e}'
                    )
                    _rescue_from_spam(mail, peer_addresses, since_str)
            else:
                _rescue_from_spam(mail, peer_addresses, since_str)
            mail.select('INBOX')
            for peer in peers:
                try:
                    typ, data = mail.search(
                        None, f'(FROM "{peer.email_address}" SINCE {since_str})'
                    )
                except Exception:
                    continue
                if typ != 'OK' or not data or not data[0]:
                    continue
                for num in data[0].split()[-25:]:
                    try:
                        typ, msg_data = mail.fetch(
                            num,
                            '(BODY.PEEK[HEADER.FIELDS '
                            '(MESSAGE-ID SUBJECT IN-REPLY-TO REFERENCES FROM)])',
                        )
                    except Exception:
                        continue
                    if typ != 'OK' or not msg_data:
                        continue
                    raw = None
                    for part in msg_data:
                        if isinstance(part, tuple) and len(part) > 1:
                            raw = part[1]
                            break
                    if not raw:
                        continue
                    try:
                        msg = email_lib.message_from_bytes(raw)
                    except Exception:
                        continue

                    scanned += 1
                    subject = _header_value(msg, 'Subject')
                    message_id = _header_value(msg, 'Message-ID')
                    in_reply_to = _header_value(msg, 'In-Reply-To')

                    # ── LOOP GUARD 1: never reply to something that is itself
                    # a reply (Re: subject, or carries In-Reply-To).
                    if in_reply_to:
                        continue
                    if subject.strip().lower().startswith('re:'):
                        continue

                    row = _match_warmup_row(
                        account, message_id, subject, peer.email_address.lower(), now
                    )
                    if not row:
                        continue
                    # ── LOOP GUARD 2: row must be an original, not a reply.
                    if row.is_reply:
                        continue
                    # ── LOOP GUARD 3: one reply per thread, ever.
                    if row.reply_scheduled or row.reply_sent:
                        continue
                    if WarmupEmail.query.filter_by(parent_id=row.id).first():
                        continue

                    row.delivered_detected_at = now
                    if not row.message_id and message_id:
                        row.message_id = message_id
                    if random.random() <= REPLY_PROBABILITY:
                        row.reply_scheduled = True
                        row.reply_due_at = now + timedelta(
                            seconds=random.randint(*REPLY_DELAY_SECONDS)
                        )
                        scheduled += 1
                    else:
                        # Decided not to reply — close the thread so it is
                        # never picked up again.
                        row.reply_sent = True
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            errors.append(f'{account.email_address}: {e}')
        finally:
            if mail is not None:
                try:
                    mail.logout()
                except Exception:
                    pass

    return {'scanned': scanned, 'scheduled': scheduled, 'errors': errors}


# ─── 4. Deferred auto-reply ──────────────────────────────────────────────────

def process_warmup_replies(max_replies: int = 5) -> dict:
    """Scheduled job #4 — send the delayed replies that became due."""
    now = datetime.utcnow()
    due = (
        WarmupEmail.query
        .filter(
            WarmupEmail.status == 'sent',
            WarmupEmail.is_reply.is_(False),
            WarmupEmail.reply_scheduled.is_(True),
            WarmupEmail.reply_sent.is_(False),
            WarmupEmail.reply_due_at.isnot(None),
            WarmupEmail.reply_due_at <= now,
        )
        .order_by(WarmupEmail.reply_due_at)
        .limit(max_replies)
        .all()
    )
    if not due:
        return {'replied': 0, 'errors': []}

    settings = Settings.get_singleton()
    replied = 0
    errors = []

    for row in due:
        # ── LOOP GUARD: re-check everything at send time.
        if (row.subject or '').strip().lower().startswith('re:'):
            row.reply_sent = True
            db.session.commit()
            continue
        if WarmupEmail.query.filter_by(parent_id=row.id).first():
            row.reply_sent = True
            db.session.commit()
            continue

        replier = EmailAccount.query.get(row.recipient_account_id) if row.recipient_account_id else None
        original_sender = EmailAccount.query.get(row.sender_account_id) if row.sender_account_id else None
        if not replier or not original_sender or not replier.warmup_enabled:
            row.reply_sent = True
            db.session.commit()
            continue

        replier.reset_warmup_daily_if_needed()

        token = uuid.uuid4().hex[:12]
        subject = f'Re: {row.subject}'
        body = _compose_reply(replier, original_sender, token)

        try:
            # thread_id is deliberately NOT passed: row.gmail_thread_id belongs
            # to the ORIGINAL SENDER's mailbox and is meaningless (and invalid)
            # for the replying account's Gmail API. In-Reply-To/References are
            # what actually thread the conversation.
            message_id, thread_id = _send_email(
                replier,
                original_sender.email_address,
                subject,
                body,
                '',
                _display_name(replier.email_address),
                in_reply_to=row.message_id,
                references=row.message_id,
            )
        except Exception as e:
            err = str(e)
            errors.append(f'{replier.email_address}: {err}')
            _log_warmup(replier.email_address, original_sender.email_address,
                        subject, now, 'warmup_failed', error=err)
            # Back off, retry on a later run.
            row.reply_due_at = now + timedelta(
                seconds=random.randint(*FAILURE_BACKOFF_SECONDS)
            )
            db.session.commit()
            continue

        row.reply_sent = True
        row.replied_at = now

        child = WarmupEmail(
            sender_account_id=replier.id,
            recipient_account_id=original_sender.id,
            sender_email=replier.email_address,
            recipient_email=original_sender.email_address,
            subject=subject,
            token=token,
            message_id=message_id,
            gmail_thread_id=thread_id,
            sent_at=now,
            status='sent',
            is_reply=True,                 # never scanned for a further reply
            parent_id=row.id,
            open_due_at=now + timedelta(seconds=random.randint(*OPEN_DELAY_SECONDS)),
            reply_scheduled=False,
            reply_sent=True,               # terminal — hard stop on the thread
        )
        db.session.add(child)

        # Replies count towards the replier's WARMUP volume only.
        replier.warmup_sent_today = int(replier.warmup_sent_today or 0) + 1

        _log_warmup(replier.email_address, original_sender.email_address, subject,
                    now, 'sent', message_id=message_id, thread_id=thread_id)
        db.session.commit()
        replied += 1

    return {'replied': replied, 'errors': errors}


# ─── 5. Status helper (for an optional dashboard route) ──────────────────────

def warmup_status() -> dict:
    now = datetime.utcnow()
    settings = Settings.get_singleton()
    pool = _warmup_pool()
    accounts = []
    for acc in pool:
        accounts.append({
            'id': acc.id,
            'email': acc.email_address,
            'auth_type': acc.auth_type,
            'warmup_day': acc.warmup_day,
            'sent_today': int(acc.warmup_sent_today or 0),
            'goal_today': int(acc.warmup_daily_goal or 0) or _ramp_volume(acc),
            'target_daily': int(acc.warmup_target_daily or WARMUP_DEFAULT_TARGET),
            'next_send_at': acc.warmup_next_allowed_send_at.isoformat()
                            if acc.warmup_next_allowed_send_at else None,
            'daily_sent_count_campaign': acc.daily_sent_count,   # untouched by warmup
        })
    return {
        'in_window': _in_warmup_window(settings, now),
        'window_local': f'{WARMUP_WINDOW_START_HOUR:02d}:00–{WARMUP_WINDOW_END_HOUR:02d}:00 '
                        f'{settings.timezone or "UTC"}',
        'accounts': accounts,
        'sent_24h': WarmupEmail.query.filter(
            WarmupEmail.sent_at >= now - timedelta(hours=24),
            WarmupEmail.status == 'sent',
        ).count(),
        'pending_opens': WarmupEmail.query.filter(
            WarmupEmail.opened_at.is_(None),
            WarmupEmail.open_due_at.isnot(None),
            WarmupEmail.status == 'sent',
        ).count(),
        'pending_replies': WarmupEmail.query.filter(
            WarmupEmail.reply_scheduled.is_(True),
            WarmupEmail.reply_sent.is_(False),
        ).count(),
    }


# ─────────────────────────────────────────────────────────────────────────────
# WHY warmup failures use status='warmup_failed' and never 'failed':
#
# sequence.pick_next_campaign_lead() builds:
#     failed_lead_ids = session.query(EmailLog.lead_id)
#                              .filter_by(status='failed').distinct().subquery()
#     ... .filter(Lead.id.notin_(failed_lead_ids))
#
# It does NOT filter on log_type. A warmup EmailLog row has lead_id = NULL, so
# a row with status='failed' would put a NULL into that subquery, and in SQL
# `x NOT IN (…, NULL)` is never true — the first query would return ZERO leads
# and real campaign sending would silently stop. Using 'warmup_failed' keeps
# warmup completely out of that subquery.
# ─────────────────────────────────────────────────────────────────────────────
