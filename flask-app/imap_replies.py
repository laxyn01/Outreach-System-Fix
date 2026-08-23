import imaplib
import json
from models import CampaignLead, EmailAccount, Lead, db


def _imap_login(mail, account):
    """Login via app-password (SMTP accounts) or XOAUTH2 (OAuth accounts)."""
    if account.auth_type == 'oauth' and account.oauth_token:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request

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

        auth_string = f'user={account.email_address}\x01auth=Bearer {creds.token}\x01\x01'
        mail.authenticate('XOAUTH2', lambda x: auth_string.encode())
    else:
        mail.login(account.email_address, account.app_password)


def check_replies() -> dict:
    lead_map = {
        l.email.lower(): l
        for l in Lead.query.filter_by(replied=False, unsubscribed=False).all()
    }
    if not lead_map:
        return {'checked': 0, 'replies_found': 0, 'errors': []}
    replies_found = 0
    errors = []
    for account in EmailAccount.query.all():
        try:
            mail = imaplib.IMAP4_SSL('imap.gmail.com', 993)
            _imap_login(mail, account)
            mail.select('INBOX')
            for email_addr, lead in lead_map.items():
                try:
                    _, data = mail.search(None, f'(FROM "{email_addr}")')
                    if data and data[0]:
                        ids = data[0].split()
                        if ids:
                            lead.replied = True
                            replies_found += 1
                            for cl in CampaignLead.query.filter_by(lead_id=lead.id).all():
                                cl.replied = True
                                cl.finished = True
                except Exception:
                    pass
            mail.logout()
        except Exception as e:
            errors.append(f'{account.email_address}: {str(e)}')
    db.session.commit()
    return {
        'checked': len(lead_map),
        'replies_found': replies_found,
        'errors': errors,
    }
