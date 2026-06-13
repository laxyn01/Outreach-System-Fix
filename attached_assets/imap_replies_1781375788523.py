import imaplib

from models import EmailAccount, Lead, db


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
            mail.login(account.email_address, account.app_password)
            mail.select('INBOX')
            for email_addr, lead in lead_map.items():
                try:
                    _, data = mail.search(None, f'(FROM "{email_addr}")')
                    if data and data[0]:
                        ids = data[0].split()
                        if ids:
                            lead.replied = True
                            replies_found += 1
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
