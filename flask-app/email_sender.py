import base64
import json
import os
import random
import smtplib
import secrets
import uuid
import re
import requests
from email.mime.image import MIMEImage
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

def _build_html_part_with_images(html: str):
    """Return a MIME part for the HTML alternative. If the HTML has Cloudinary
    images, wraps them properly in multipart/related so clients show real
    inline images. Falls back to plain MIMEText on any failure."""
    if not html:
        return MIMEText(html or '', 'html', 'utf-8')

    images = []

    def repl(match):
        url = match.group(1)
        try:
            resp = requests.get(url, timeout=8)
            resp.raise_for_status()
            cid = uuid.uuid4().hex
            content_type = resp.headers.get('Content-Type', 'image/jpeg').split(';')[0]
            subtype = content_type.split('/')[-1] or 'jpeg'
            ext = 'jpg' if subtype == 'jpeg' else subtype
            img_part = MIMEImage(resp.content, _subtype=subtype)
            img_part.add_header('Content-ID', f'<{cid}>')
            img_part.add_header('Content-Disposition', 'inline', filename=f'image.{ext}')
            images.append(img_part)
            return f'src="cid:{cid}"'
        except Exception:
            return match.group(0)

    new_html = re.sub(r'src="(https://res\.cloudinary\.com/[^"]+)"', repl, html)

    if not images:
        return MIMEText(new_html, 'html', 'utf-8')

    related = MIMEMultipart('related')
    related.attach(MIMEText(new_html, 'html', 'utf-8'))
    for img in images:
        related.attach(img)
    return related


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
        msg.attach(_build_html_part_with_images(html))

    with smtplib.SMTP(account.smtp_host, account.smtp_port) as server:
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.login(account.email_address, account.app_password)
        server.sendmail(account.email_address, to_email, msg.as_string())

    return new_message_id, None


# ── NEW: shared OAuth credential loader ───────────────────────────────────────

def _load_oauth_credentials(account: EmailAccount):
    """Build a google Credentials object from account.oauth_token, refreshing
    and persisting the access token (and its expiry) if it has expired or if
    no expiry was ever stored for it (legacy rows saved before this fix)."""
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from datetime import datetime

    token_data = json.loads(account.oauth_token)

    expiry = None
    if token_data.get('expiry'):
        try:
            expiry = datetime.fromisoformat(token_data['expiry'])
        except Exception:
            expiry = None

    creds = Credentials(
        token=token_data.get('token'),
        refresh_token=token_data.get('refresh_token'),
        token_uri='https://oauth2.googleapis.com/token',
        client_id=token_data.get('client_id'),
        client_secret=token_data.get('client_secret'),
        scopes=token_data.get('scopes'),
        expiry=expiry,
    )

    # expiry is None for every row saved before this fix — without a stored
    # expiry, creds.expired is permanently False and refresh() never fires.
    # Force a refresh in that case too, not just when actually expired.
    if creds.refresh_token and (expiry is None or creds.expired):
        creds.refresh(Request())
        token_data['token'] = creds.token
        token_data['expiry'] = creds.expiry.isoformat() if creds.expiry else None
        account.oauth_token = json.dumps(token_data)
        db.session.commit()

    return creds


# ── NEW (OutreachCommand Outlook/Graph task): MSAL constants ──────────────────
# Mirrors the same-named constants in app.py exactly (same env vars, same
# defaults, same "common" authority reasoning — see the comment above
# connect_outlook() in app.py for why "common" and not the GUID tenant or
# "consumers"). Deliberately duplicated here rather than imported from app.py:
# app.py does `from email_sender import ...` at module load time, so an
# `from app import ...` here would be a circular import. Since these are
# just constants (not state), duplication is simpler and safer than a lazy
# in-function import of app.py.
MS_CLIENT_ID = os.getenv('MS_CLIENT_ID', 'a5d4c56e-8e9c-4018-ba95-e6fe6020f791')
MS_CLIENT_SECRET = os.getenv('MS_CLIENT_SECRET')
MS_AUTHORITY = 'https://login.microsoftonline.com/common'
MS_SCOPES = ['Mail.Send', 'Mail.ReadWrite', 'User.Read']

# NEW: in-memory cache for Outlook/Graph access tokens, keyed by account.id.
# Microsoft's abuse-detection (AADSTS70000 "service abuse mode") can trigger
# on accounts that get their token refreshed too frequently — warmup calls
# _load_outlook_credentials() on the same account multiple times within a
# few minutes (open-marking, spam-rescue, sending), and without this cache
# every one of those calls was a fresh MSAL refresh-token call to Microsoft.
# This cache holds the token for up to ~55 minutes (Graph tokens are valid
# ~60 minutes; 5-minute safety buffer) so repeat calls within that window
# reuse the same token instead of hitting Microsoft again. Process-local
# only (resets on restart/redeploy) — that's fine, a cache miss just falls
# through to a normal refresh.
_outlook_token_cache = {}
_OUTLOOK_TOKEN_CACHE_SECONDS = 55 * 60


def _load_outlook_credentials(account: EmailAccount) -> str:
    """Return a valid Graph API access token for an Outlook/Microsoft 365
    personal account, refreshing via MSAL and persisting the new token set
    if the cached one is expired or about to expire.

    This is the Outlook-specific analogue of _load_oauth_credentials() above.
    It is intentionally a SEPARATE function rather than a modification of
    that one: the two providers store differently-shaped JSON in the same
    oauth_token column (Google: token/refresh_token/client_id/client_secret/
    scopes: MSAL result dict: access_token/refresh_token/expires_in/id_token
    etc.), and google.oauth2.credentials.Credentials has no idea how to parse
    an MSAL token dict. Callers MUST already know account.provider == 'outlook'
    before calling this (see the dispatcher in _send_email below).

    Returns the bearer access_token string (not a Credentials object) since
    that's all send_outlook_graph() needs to call Graph's REST API directly.
    """
    import msal
    import time

    # NEW: serve from the in-memory cache if we refreshed this account's
    # token recently and it hasn't hit the cache TTL yet — avoids hammering
    # Microsoft's refresh endpoint on every call.
    cached = _outlook_token_cache.get(account.id)
    if cached and (time.monotonic() - cached['cached_at']) < _OUTLOOK_TOKEN_CACHE_SECONDS:
        return cached['access_token']

    token_data = json.loads(account.oauth_token)

    msal_app = msal.ConfidentialClientApplication(
        MS_CLIENT_ID,
        authority=MS_AUTHORITY,
        client_credential=MS_CLIENT_SECRET,
    )

    refresh_token = token_data.get('refresh_token')
    result = None
    if refresh_token:
        # acquire_token_by_refresh_token handles the "still valid, just return
        # a cached-equivalent token" case internally via MSAL's token cache
        # semantics for a ConfidentialClientApplication built fresh each call,
        # so we always go through it rather than hand-rolling an expires_on
        # check — MSAL's own refresh endpoint call is the source of truth for
        # whether Microsoft still considers the access token good.
        result = msal_app.acquire_token_by_refresh_token(
            refresh_token, scopes=MS_SCOPES,
        )

    if not result or 'access_token' not in result:
        raise RuntimeError(
            f'Failed to refresh Outlook/Graph token for {account.email_address}: '
            f'{(result or {}).get("error_description") or (result or {}).get("error") or "no refresh_token stored"}'
        )

    # MSAL's refresh result may omit a new refresh_token (Microsoft doesn't
    # always rotate it) — keep the old one in that case instead of losing it.
    merged = dict(token_data)
    merged.update(result)
    if 'refresh_token' not in result and refresh_token:
        merged['refresh_token'] = refresh_token

    account.oauth_token = json.dumps(merged)
    db.session.commit()

    # NEW: populate the cache so the next call within the TTL window skips
    # Microsoft entirely.
    _outlook_token_cache[account.id] = {
        'access_token': merged['access_token'],
        'cached_at': time.monotonic(),
    }

    return merged['access_token']


# ── NEW: Gmail API sender ─────────────────────────────────────────────────────

def send_gmail_api(account: EmailAccount, to_email: str, subject: str, plain: str, html: str, sender_name: str = '', in_reply_to: str = None, references: str = None, thread_id: str = None):
    """Send email via Gmail API using stored OAuth token."""
    from googleapiclient.discovery import build

    creds = _load_oauth_credentials(account)

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
        msg.attach(_build_html_part_with_images(html))
        
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    body = {'raw': raw}
    if thread_id:
        body['threadId'] = thread_id

    sent = service.users().messages().send(userId='me', body=body).execute()
    sent_id = sent.get('id')
    gmail_thread_id = sent.get('threadId')

    # Gmail overrides our Message-ID header — fetch the real one it assigned
    actual_message_id = new_message_id
    try:
        full_msg = service.users().messages().get(
            userId='me', id=sent_id, format='metadata', metadataHeaders=['Message-ID']
        ).execute()
        headers = full_msg.get('payload', {}).get('headers', [])
        for h in headers:
            if h.get('name', '').lower() == 'message-id':
                actual_message_id = h.get('value')
                break
        print(f'[GMAIL] Fetched real Message-ID: {actual_message_id}', flush=True)
    except Exception as e:
        print(f'[GMAIL] Failed to fetch real Message-ID: {e}', flush=True)

    return actual_message_id, gmail_thread_id


# ── NEW (OutreachCommand Outlook/Graph task): Outlook sender ─────────────────

def _build_outlook_attachments(html: str):
    """Outlook/Graph analogue of _build_html_part_with_images() above — NOT a
    modification of it, a separate helper, since Graph's JSON attachment
    model (fileAttachment objects + cid: references in the html) is a
    completely different shape from that function's MIME multipart/related
    output. Graph's MIME-import parsing does not reliably preserve nested
    multipart/alternative > multipart/related > inline-cid structures, so
    images sent that way arrive as regular attachments instead of inline
    (confirmed in production) — the JSON attachment model with isInline=True
    is what actually renders inline in Outlook/OWA.

    Mirrors _build_html_part_with_images()'s URL-detection/download logic
    exactly (same Cloudinary regex, same requests.get + Content-Type
    handling, same graceful fallback of leaving the original src="..." in
    place on any download failure) — only the output shape differs: a
    (new_html, attachments) tuple instead of a MIME part object.

    Returns (html, []) unchanged if html is falsy or has no Cloudinary images.
    """
    if not html:
        return html, []

    attachments = []

    def repl(match):
        url = match.group(1)
        try:
            resp = requests.get(url, timeout=8)
            resp.raise_for_status()
            cid = uuid.uuid4().hex
            content_type = resp.headers.get('Content-Type', 'image/jpeg').split(';')[0]
            subtype = content_type.split('/')[-1] or 'jpeg'
            ext = 'jpg' if subtype == 'jpeg' else subtype
            attachments.append({
                '@odata.type': '#microsoft.graph.fileAttachment',
                'name': f'image.{ext}',
                'contentType': content_type,
                'contentBytes': base64.b64encode(resp.content).decode('ascii'),
                'contentId': cid,
                'isInline': True,
            })
            return f'src="cid:{cid}"'
        except Exception:
            return match.group(0)

    new_html = re.sub(r'src="(https://res\.cloudinary\.com/[^"]+)"', repl, html)
    return new_html, attachments


def send_outlook_graph(account: EmailAccount, to_email: str, subject: str, plain: str, html: str, sender_name: str = '', in_reply_to: str = None, references: str = None, thread_id: str = None):
    """Send email via Microsoft Graph using the account's OAuth access token.
    Mirrors send_gmail_api()'s signature exactly, so _send_email()'s caller
    (try_send_next_email in this file) doesn't need to know which provider
    actually sent it.

    RETURN SHAPE (repurposed, read this before touching the caller):
    Returns (internet_message_id, graph_internal_id) — same 2-tuple position
    as send_gmail_api()'s (message_id, thread_id), but the second element is
    NOT a Gmail-style thread id (Graph's REST API has no equivalent). It is
    Graph's own internal message `id` for the message THIS call just sent.
    The caller must persist it as EmailLog.outlook_message_id (a new column,
    NOT gmail_thread_id) so a later follow-up in the same lead's sequence can
    look this message back up via createReply() (see below) — that internal
    `id` is the only thing createReply() accepts; the RFC internetMessageId
    is useless for that lookup.

    `thread_id` (an INPUT param here) is likewise repurposed on the way IN:
    the caller passes the PREVIOUS step's EmailLog.outlook_message_id through
    this parameter (the same way it already passes the previous step's Gmail
    threadId to send_gmail_api() via this same parameter) so this function
    knows which existing Graph message to reply to. If in_reply_to is set
    but thread_id is not (e.g. an old row from before this column existed),
    this function falls back to a fresh (non-threaded) message rather than
    failing, since createReply() cannot work without Graph's internal id.

    WHY THIS VERSION (previous attempts and their failures):
    v1 sent a JSON body to /me/sendMail with custom "Message-ID"/"In-Reply-To"
    /"References" entries in internetMessageHeaders. Per Graph's own docs,
    custom headers are only honored when their name starts with "x-" — plain
    RFC header names there are silently dropped, so neither the id we stored
    nor the threading ever actually worked.
    v2 switched to building a real MIME message and POSTing it as base64 to
    /me/sendMail with Content-Type: text/plain, to get real headers through.
    That fixed the header-naming issue, but production testing surfaced a
    deeper problem: Exchange Online's transport pipeline commonly REPLACES a
    client-supplied Message-ID with its own server-generated one while
    parsing arbitrary inbound MIME content — so the id we generated and
    stored still never matched the real delivered message, and a real
    follow-up test landed as a separate thread instead of nesting. The same
    MIME-import path also doesn't reliably preserve nested
    multipart/alternative > multipart/related > inline-cid structures, so
    inline Cloudinary images arrived as plain attachments instead.

    Both problems trace back to the same thing: routing content through
    Graph's raw-MIME-import parser instead of its native message model. This
    version (v3) avoids that entirely by building the message as Graph's own
    JSON Message resource. Creating a message via POST /me/messages returns
    the full Message resource, including `id` and `internetMessageId` set by
    Graph itself at creation time — that IS the real id Exchange delivers
    with, not something transport can silently rewrite afterwards. Threading
    uses Graph's own createReply() action, which sets References/In-Reply-To
    /conversationId correctly itself (we still cannot set those directly —
    the same "x-" prefix restriction on custom headers applies to the JSON
    model too).

    Flow:
      - If in_reply_to AND thread_id (the previous message's Graph internal
        id) are both present: POST .../messages/{thread_id}/createReply to
        get a correctly-threaded draft, PATCH its subject/body/toRecipients
        with our actual follow-up content (createReply's draft starts out
        with quoted-original boilerplate we don't want), then attach any
        inline images one-by-one via POST .../messages/{id}/attachments —
        Graph does NOT accept an attachments array in a PATCH to an existing
        message, only in the create call for a brand-new message.
      - Otherwise (first email in a sequence, or no stored Graph id to reply
        to): POST .../messages with the full message body AND inline
        attachments array in one call — Graph's docs confirm attachments can
        be embedded directly in a message-create call ("you can add an
        attachment to a message that is being created and sent on the fly").
      - Either way, finish with POST .../messages/{id}/send (empty body) to
        send the now-fully-built draft.
    """
    access_token = _load_outlook_credentials(account)
    headers_json = {
        'Authorization': f'Bearer {access_token}',
        'Content-Type': 'application/json',
        'Prefer': 'IdType="ImmutableId"',
    }
    headers_bearer = {
         'Authorization': f'Bearer {access_token}',
         'Prefer': 'IdType="ImmutableId"',
    }

    rendered_html, attachments = (html, [])
    if rendered_html is not None:
        body_content = rendered_html
        content_type = 'HTML'
    else:
        body_content = plain or ''
        content_type = 'Text'

    message_body = {
        'subject': subject,
        'body': {'contentType': content_type, 'content': body_content},
        'toRecipients': [{'emailAddress': {'address': to_email}}],
    }

    is_reply = bool(in_reply_to and thread_id)

    if is_reply:
        reply_resp = requests.post(
            f'https://graph.microsoft.com/v1.0/me/messages/{thread_id}/createReply',
            headers=headers_json, json={}, timeout=30,
        )
        if reply_resp.status_code not in (200, 201):
            raise RuntimeError(
                f'Graph createReply failed ({reply_resp.status_code}): {reply_resp.text[:500]}'
            )
        draft = reply_resp.json()
        draft_id = draft.get('id')

        # createReply's draft starts pre-filled with quoted-original
        # boilerplate and the original recipients — overwrite with our own
        # follow-up subject/body/toRecipients. (No attachments here: Graph
        # rejects an attachments array on PATCH to an existing message.)
        patch_resp = requests.patch(
            f'https://graph.microsoft.com/v1.0/me/messages/{draft_id}',
            headers=headers_json, json=message_body, timeout=30,
        )
        if patch_resp.status_code not in (200, 201):
            raise RuntimeError(
                f'Graph reply-draft update failed ({patch_resp.status_code}): {patch_resp.text[:500]}'
            )
        if patch_resp.text:
            draft = patch_resp.json()

        for att in attachments:
            att_resp = requests.post(
                f'https://graph.microsoft.com/v1.0/me/messages/{draft_id}/attachments',
                headers=headers_json, json=att, timeout=30,
            )
            if att_resp.status_code not in (200, 201):
                raise RuntimeError(
                    f'Graph add-attachment failed ({att_resp.status_code}): {att_resp.text[:500]}'
                )
    else:
        if attachments:
            message_body['attachments'] = attachments
        create_resp = requests.post(
            'https://graph.microsoft.com/v1.0/me/messages',
            headers=headers_json, json=message_body, timeout=30,
        )
        if create_resp.status_code not in (200, 201):
            raise RuntimeError(
                f'Graph create-message failed ({create_resp.status_code}): {create_resp.text[:500]}'
            )
        draft = create_resp.json()
        draft_id = draft.get('id')

    real_internet_message_id = draft.get('internetMessageId')
    if not draft_id or not real_internet_message_id:
        raise RuntimeError(
            f'Graph did not return an id/internetMessageId for the new message: {draft}'
        )

    send_resp = requests.post(
        f'https://graph.microsoft.com/v1.0/me/messages/{draft_id}/send',
        headers=headers_bearer, timeout=30,
    )
    if send_resp.status_code not in (200, 202):
        raise RuntimeError(
            f'Graph message-send failed ({send_resp.status_code}): {send_resp.text[:500]}'
        )

    return real_internet_message_id, draft_id


# ─────────────────────────────────────────────────────────────────────────────
def _send_email(account: EmailAccount, to_email: str, subject: str, plain: str, html: str, sender_name: str = '', in_reply_to: str = None, references: str = None, thread_id: str = None):
    """Smart dispatcher — uses OAuth if available, falls back to SMTP.

    NEW (OutreachCommand Outlook/Graph task): when an OAuth account's
    provider is 'outlook', route to send_outlook_graph() instead of
    send_gmail_api(). getattr() with a 'gmail' default keeps this safe for
    every pre-existing row, which has no provider column value set until the
    migration backfills the default — those rows behave exactly as before.
    """
    if account.auth_type == 'oauth' and account.oauth_token:
        if getattr(account, 'provider', 'gmail') == 'outlook':
            return send_outlook_graph(account, to_email, subject, plain, html, sender_name, in_reply_to, references, thread_id)
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
        return None, None, 0
    steps = campaign.get_steps()
    if not steps:
        return None, None, 0
    step_idx = step - 1
    if step_idx < 0 or step_idx >= len(steps):
        return None, None, 0
    step_data = steps[step_idx]
    variants = step_data.get('variants', [])
    if not variants:
        return None, None, 0
    variant_idx = random.randrange(len(variants))
    variant = variants[variant_idx]
    return variant.get('subject', ''), variant.get('body', ''), variant_idx


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

        subject_raw, body_raw, variant_idx = _pick_campaign_content(campaign, step)
        if subject_raw is None:
            template = _pick_legacy_template(campaign, step) or pick_template(step)
            if not template:
                return {'sent': 0, 'skipped': 1, 'errors': [f'No template for step {step}'], 'reason': 'no_template'}
            subject_raw = template.subject
            body_raw = template.body
            variant_idx = 0

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
                # NEW (Outlook/Graph threading fix): Outlook accounts need
                # the PREVIOUS message's Graph-internal id (createReply()
                # only accepts that, not the RFC message_id/internetMessageId
                # above) — reuse this same thread_id slot to carry it through
                # to send_outlook_graph(), exactly the way it already carries
                # Gmail's threadId through to send_gmail_api().
                if getattr(account, 'provider', 'gmail') == 'outlook':
                    thread_id = prev_log.outlook_message_id
                else:
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
            account.consecutive_failures = 0
            account.is_paused_auto = False
            settings.next_allowed_send_at = now + timedelta(seconds=random.randint(60, 120))
            # NEW (Outlook/Graph threading fix): send_outlook_graph() returns
            # Graph's internal message id in the same tuple slot Gmail uses
            # for its threadId — route it to the new outlook_message_id
            # column instead of gmail_thread_id so a later follow-up's
            # createReply() lookup (above) finds the right column.
            is_outlook = getattr(account, 'provider', 'gmail') == 'outlook'
            log = EmailLog(
                lead_id=lead.id, account_used=account.email_address, step=step,
                subject=subject, sent_at=now, log_type='campaign', status='sent',
                lead_email=lead.email, lead_name=lead.full_name, campaign_id=cl.campaign_id,
                tracking_token=tracking_token, message_id=new_message_id,
                gmail_thread_id=(None if is_outlook else new_thread_id),
                outlook_message_id=(new_thread_id if is_outlook else None),
                variant_index=variant_idx,
            )
            db.session.add(log)
            db.session.commit()
            return {'sent': 1, 'skipped': 0, 'errors': [], 'lead': lead.email, 'step': step, 'account': account.email_address}
        except Exception as e:
            error_str = str(e)
            log = EmailLog(
                lead_id=lead.id, account_used=account.email_address, step=step,
                subject=subject, sent_at=now, log_type='campaign', status='failed',
                lead_email=lead.email, lead_name=lead.full_name, campaign_id=cl.campaign_id,
                error_message=error_str,
            )
            db.session.add(log)

            account.last_error = error_str
            account.last_error_at = now
            account.consecutive_failures = (account.consecutive_failures or 0) + 1
            hard_block_signals = ['limit', 'quota', 'suspend', 'disabled', 'blocked']
            if any(sig in error_str.lower() for sig in hard_block_signals):
                account.is_paused_auto = True

            db.session.commit()
            fail_count = EmailLog.query.filter_by(
                lead_id=lead.id, step=step, status='failed'
            ).count()
            if fail_count >= 4:
                cl.finished = True
                db.session.commit()
            return {'sent': 0, 'skipped': 1, 'errors': [f'{lead.email}: {error_str}'], 'reason': 'send_failed'}

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
    subject_raw, body_raw, variant_idx = _pick_campaign_content(campaign, step)
    if subject_raw is None:
        template = (_pick_legacy_template(campaign, step) if campaign else None) or pick_template(step)
        if not template:
            return {'sent': 0, 'skipped': 1, 'errors': [f'No template for step {step}'], 'reason': 'no_template'}
        subject_raw = template.subject
        body_raw = template.body
        variant_idx = 0

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
            variant_index=variant_idx,
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
