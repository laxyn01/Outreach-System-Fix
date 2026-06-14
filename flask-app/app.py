import base64
import csv
import io
import json
import os
from datetime import datetime, timedelta

from flask import (
    Flask,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    Response,
    url_for,
)
from dotenv import load_dotenv
from sqlalchemy import func

from models import Campaign, EmailAccount, EmailLog, Lead, Settings, Template, db, init_db
from email_sender import (
    clean_email,
    preview_template,
    send_test_email,
    try_send_next_email,
)
from imap_replies import check_replies
from scheduler import start_scheduler
from sequence import is_within_send_window

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv('FLASK_SECRET_KEY', 'dev-secret-change-me')

init_db(app)


def cleanup_lead_emails():
    leads = Lead.query.all()
    for lead in leads:
        cleaned = clean_email(lead.email)
        if not cleaned:
            lead.unsubscribed = True
        else:
            lead.email = cleaned
    db.session.commit()


with app.app_context():
    cleanup_lead_emails()
    start_scheduler(app)


@app.route('/ping')
def ping():
    return 'OK'


@app.context_processor
def inject_globals():
    return {'now': datetime.utcnow()}


def get_dashboard_stats():
    settings = Settings.get_singleton()
    now = datetime.utcnow()
    total = Lead.query.count()
    active = Lead.query.filter(
        Lead.sequence_step.in_([1, 2]),
        Lead.unsubscribed.is_(False),
        Lead.replied.is_(False),
    ).count()
    sent = Lead.query.filter(Lead.sequence_step > 0).count()
    opened = Lead.query.filter_by(opened=True).count()
    clicked = Lead.query.filter_by(clicked=True).count()
    replies = Lead.query.filter_by(replied=True).count()
    unsubbed = Lead.query.filter_by(unsubscribed=True).count()
    open_rate = round((opened / sent * 100) if sent else 0, 1)
    click_rate = round((clicked / sent * 100) if sent else 0, 1)
    reply_rate = round((replies / sent * 100) if sent else 0, 1)

    last_log = EmailLog.query.filter_by(log_type='campaign', status='sent').order_by(
        EmailLog.sent_at.desc()
    ).first()
    last_sent_mins = None
    if last_log and last_log.sent_at:
        last_sent_mins = int((now - last_log.sent_at).total_seconds() / 60)

    next_send_mins = None
    schedule_status = 'active'
    if not is_within_send_window(settings, now):
        schedule_status = 'outside_window'
    elif settings.next_allowed_send_at and now < settings.next_allowed_send_at:
        next_send_mins = int((settings.next_allowed_send_at - now).total_seconds() / 60)
        schedule_status = 'rate_limit'

    return {
        'total': total, 'active': active, 'sent': sent,
        'opened': opened, 'clicked': clicked, 'replies': replies,
        'unsubbed': unsubbed, 'open_rate': open_rate,
        'click_rate': click_rate, 'reply_rate': reply_rate,
        'last_sent_mins': last_sent_mins,
        'next_send_mins': next_send_mins,
        'schedule_status': schedule_status,
    }


# ─── Dashboard ──────────────────────────────────────────────────────────────

@app.route('/')
def dashboard():
    stats = get_dashboard_stats()
    recent_logs = EmailLog.query.filter_by(log_type='campaign').order_by(
        EmailLog.sent_at.desc()
    ).limit(10).all()
    campaigns = Campaign.query.order_by(Campaign.created_at.desc()).limit(5).all()
    return render_template('dashboard.html', stats=stats, recent_logs=recent_logs, campaigns=campaigns)


@app.route('/trigger-sequence', methods=['POST'])
def trigger_sequence():
    result = try_send_next_email()
    return jsonify(result)


@app.route('/check/replies', methods=['POST'])
def check_replies_route():
    result = check_replies()
    flash(f'Checked {result["checked"]} leads. Found {result["replies_found"]} replies.', 'success')
    if result['errors']:
        flash('Errors: ' + '; '.join(result['errors']), 'error')
    return redirect(url_for('dashboard'))


# ─── Campaigns ──────────────────────────────────────────────────────────────

@app.route('/campaigns')
def campaigns_page():
    campaigns = Campaign.query.order_by(Campaign.created_at.desc()).all()
    return render_template('campaigns.html', campaigns=campaigns)


@app.route('/campaigns/new', methods=['GET', 'POST'])
def campaign_new():
    templates = Template.query.order_by(Template.step, Template.id).all()
    step1_templates = [t for t in templates if t.step == 1]
    step2_templates = [t for t in templates if t.step == 2]
    step3_templates = [t for t in templates if t.step == 3]

    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        if not name:
            flash('Campaign name is required.', 'error')
            return redirect(url_for('campaign_new'))

        # Step 3 template assignments
        t1 = request.form.get('template_step1', type=int)
        t2 = request.form.get('template_step2', type=int)
        t3 = request.form.get('template_step3', type=int)

        campaign = Campaign(
            name=name,
            status='draft',
            template_step1_id=t1 or None,
            template_step2_id=t2 or None,
            template_step3_id=t3 or None,
        )
        db.session.add(campaign)
        db.session.flush()

        # Step 2: CSV upload for this campaign
        imported = 0
        if 'file' in request.files and request.files['file'].filename:
            file = request.files['file']
            rows = _parse_upload_file(file)
            for row in rows:
                email = clean_email(row.get('email', ''))
                if not email:
                    continue
                existing = Lead.query.filter_by(email=email).first()
                if existing:
                    if not existing.campaign_id:
                        existing.campaign_id = campaign.id
                else:
                    lead = Lead(
                        first_name=row.get('first_name', ''),
                        last_name=row.get('last_name', ''),
                        email=email,
                        company=row.get('company', ''),
                        campaign_id=campaign.id,
                    )
                    db.session.add(lead)
                    imported += 1

        # Step 4: launch immediately if requested
        launch = request.form.get('launch') == '1'
        if launch:
            campaign.status = 'active'

        db.session.commit()
        flash(f'Campaign "{name}" created with {imported} leads.', 'success')
        return redirect(url_for('campaigns_page'))

    return render_template(
        'campaign_new.html',
        step1_templates=step1_templates,
        step2_templates=step2_templates,
        step3_templates=step3_templates,
    )


@app.route('/campaigns/<int:campaign_id>/pause', methods=['POST'])
def campaign_pause(campaign_id):
    c = Campaign.query.get_or_404(campaign_id)
    c.status = 'paused'
    db.session.commit()
    return redirect(url_for('campaigns_page'))


@app.route('/campaigns/<int:campaign_id>/resume', methods=['POST'])
def campaign_resume(campaign_id):
    c = Campaign.query.get_or_404(campaign_id)
    c.status = 'active'
    db.session.commit()
    return redirect(url_for('campaigns_page'))


@app.route('/campaigns/<int:campaign_id>/delete', methods=['POST'])
def campaign_delete(campaign_id):
    c = Campaign.query.get_or_404(campaign_id)
    db.session.delete(c)
    db.session.commit()
    flash('Campaign deleted.', 'success')
    return redirect(url_for('campaigns_page'))


# ─── Analytics ──────────────────────────────────────────────────────────────

@app.route('/analytics')
def analytics_page():
    period = request.args.get('period', '30')
    try:
        days = int(period)
    except ValueError:
        days = 30

    now = datetime.utcnow()
    since = now - timedelta(days=days) if days > 0 else None

    q = EmailLog.query.filter_by(log_type='campaign')
    if since:
        q = q.filter(EmailLog.sent_at >= since)

    logs = q.all()
    total_sent = len(logs)
    total_opened = sum(1 for l in logs if l.opened_at)
    total_clicked = sum(1 for l in logs if l.clicked)
    total_replied = 0
    if logs:
        lead_ids = [l.lead_id for l in logs if l.lead_id]
        if lead_ids:
            total_replied = Lead.query.filter(Lead.id.in_(lead_ids), Lead.replied.is_(True)).count()

    open_rate = round(total_opened / total_sent * 100, 1) if total_sent else 0
    click_rate = round(total_clicked / total_sent * 100, 1) if total_sent else 0
    reply_rate = round(total_replied / total_sent * 100, 1) if total_sent else 0

    # Daily chart data
    daily_q = db.session.query(
        func.date(EmailLog.sent_at).label('day'),
        func.count(EmailLog.id).label('sent'),
        func.sum(db.case((EmailLog.opened_at.isnot(None), 1), else_=0)).label('opened'),
        func.sum(db.case((EmailLog.clicked.is_(True), 1), else_=0)).label('clicked'),
    ).filter(EmailLog.log_type == 'campaign')
    if since:
        daily_q = daily_q.filter(EmailLog.sent_at >= since)
    daily_q = daily_q.group_by(func.date(EmailLog.sent_at)).order_by(func.date(EmailLog.sent_at))

    chart_labels = []
    chart_sent = []
    chart_opened = []
    chart_clicked = []
    for row in daily_q.all():
        chart_labels.append(str(row.day))
        chart_sent.append(int(row.sent or 0))
        chart_opened.append(int(row.opened or 0))
        chart_clicked.append(int(row.clicked or 0))

    return render_template(
        'analytics.html',
        period=period,
        total_sent=total_sent,
        total_opened=total_opened,
        total_clicked=total_clicked,
        total_replied=total_replied,
        open_rate=open_rate,
        click_rate=click_rate,
        reply_rate=reply_rate,
        chart_labels=json.dumps(chart_labels),
        chart_sent=json.dumps(chart_sent),
        chart_opened=json.dumps(chart_opened),
        chart_clicked=json.dumps(chart_clicked),
    )


# ─── Leads ──────────────────────────────────────────────────────────────────

@app.route('/leads')
def leads_page():
    q = request.args.get('q', '').strip()
    status = request.args.get('status', 'all')
    page = request.args.get('page', 1, type=int)
    per_page = 25

    query = Lead.query
    if q:
        like = f'%{q}%'
        query = query.filter(
            db.or_(
                Lead.first_name.ilike(like),
                Lead.last_name.ilike(like),
                Lead.email.ilike(like),
                Lead.company.ilike(like),
            )
        )
    if status == 'pending':
        query = query.filter(Lead.sequence_step == 0, Lead.unsubscribed.is_(False), Lead.replied.is_(False))
    elif status == 'active':
        query = query.filter(Lead.sequence_step.in_([1, 2]), Lead.unsubscribed.is_(False), Lead.replied.is_(False))
    elif status == 'complete':
        query = query.filter(Lead.sequence_step >= 3)
    elif status == 'unsubscribed':
        query = query.filter_by(unsubscribed=True)
    elif status == 'replied':
        query = query.filter_by(replied=True)
    elif status == 'paused':
        query = query.filter_by(paused=True)

    total = query.count()
    leads = query.order_by(Lead.id.desc()).offset((page - 1) * per_page).limit(per_page).all()
    start = (page - 1) * per_page + 1 if total else 0
    end = min(page * per_page, total)
    pages = (total + per_page - 1) // per_page if total else 1

    return render_template(
        'leads.html',
        leads=leads, q=q, status=status, page=page,
        per_page=per_page, total=total, start=start, end=end, pages=pages,
    )


@app.route('/leads/bulk-delete', methods=['POST'])
def leads_bulk_delete():
    ids = request.form.getlist('lead_ids')
    if ids:
        Lead.query.filter(Lead.id.in_(ids)).delete(synchronize_session=False)
        db.session.commit()
        flash(f'Deleted {len(ids)} leads.', 'success')
    return redirect(url_for('leads_page'))


@app.route('/leads/<int:lead_id>/delete', methods=['POST'])
def lead_delete(lead_id):
    lead = Lead.query.get_or_404(lead_id)
    db.session.delete(lead)
    db.session.commit()
    flash('Lead deleted.', 'success')
    return redirect(request.referrer or url_for('leads_page'))


@app.route('/leads/<int:lead_id>/pause', methods=['POST'])
def lead_pause(lead_id):
    lead = Lead.query.get_or_404(lead_id)
    lead.paused = True
    db.session.commit()
    return redirect(request.referrer or url_for('leads_page'))


@app.route('/leads/<int:lead_id>/resume', methods=['POST'])
def lead_resume(lead_id):
    lead = Lead.query.get_or_404(lead_id)
    lead.paused = False
    db.session.commit()
    return redirect(request.referrer or url_for('leads_page'))


# ─── Upload ──────────────────────────────────────────────────────────────────

def _parse_upload_file(file):
    """Bug fix #6: handle Name / Full Name columns, split into first/last."""
    filename = file.filename.lower()
    if filename.endswith('.xlsx') or filename.endswith('.xls'):
        import pandas as pd
        df = pd.read_excel(file)
        raw_rows = df.to_dict('records')
    else:
        content = file.read().decode('utf-8-sig')
        reader = csv.DictReader(io.StringIO(content))
        raw_rows = list(reader)

    result = []
    for row in raw_rows:
        # Normalize keys to lowercase stripped
        row = {str(k).strip().lower(): str(v).strip() if v is not None else '' for k, v in row.items()}

        # Email
        email = row.get('email', '')

        # Bug fix #6: handle "name" or "full name" columns
        first = row.get('first_name', '') or row.get('first name', '')
        last = row.get('last_name', '') or row.get('last name', '')
        if not first and not last:
            full = row.get('name', '') or row.get('full name', '') or row.get('full_name', '')
            if full:
                parts = full.split(' ', 1)
                first = parts[0]
                last = parts[1] if len(parts) > 1 else ''

        result.append({
            'first_name': first.strip(),
            'last_name': last.strip(),
            'email': email,
            'company': row.get('company', '').strip(),
        })
    return result


@app.route('/upload', methods=['GET', 'POST'])
def upload():
    if request.method == 'POST':
        action = request.form.get('action', 'preview')

        if action == 'preview' and 'file' in request.files:
            file = request.files['file']
            if file.filename:
                rows = _parse_upload_file(file)
                seen = set()
                preview_rows = []
                skipped = 0
                for row in rows:
                    email = clean_email(row.get('email', ''))
                    if not email:
                        skipped += 1
                        continue
                    if email in seen:
                        continue
                    seen.add(email)
                    preview_rows.append({
                        'first_name': row.get('first_name', ''),
                        'last_name': row.get('last_name', ''),
                        'email': email,
                        'company': row.get('company', ''),
                    })
                # Bug fix #1 for upload: json.dumps with default=str
                rows_json = json.dumps(preview_rows, default=str)
                return render_template(
                    'upload.html',
                    preview_rows=preview_rows,
                    rows_json=rows_json,
                    skipped=skipped,
                    show_preview=True,
                )

        elif action == 'confirm':
            rows_json = request.form.get('rows_data', '[]').strip() or '[]'
            try:
                rows = json.loads(rows_json)
            except (json.JSONDecodeError, ValueError):
                rows = []
            imported = 0
            skipped = 0
            for row in rows:
                email = clean_email(row.get('email', ''))
                if not email:
                    skipped += 1
                    continue
                if Lead.query.filter_by(email=email).first():
                    continue
                lead = Lead(
                    first_name=row.get('first_name', ''),
                    last_name=row.get('last_name', ''),
                    email=email,
                    company=row.get('company', ''),
                )
                db.session.add(lead)
                imported += 1
            db.session.commit()
            flash(f'Imported {imported} leads. Skipped {skipped} invalid/duplicate emails.', 'success')
            return redirect(url_for('upload'))

    return render_template('upload.html', preview_rows=[], rows_json='[]', skipped=0, show_preview=False)


# ─── Accounts ────────────────────────────────────────────────────────────────

@app.route('/accounts', methods=['GET', 'POST'])
def accounts():
    if request.method == 'POST':
        email = request.form.get('email_address', '').strip()
        password = request.form.get('app_password', '').strip()
        host = request.form.get('smtp_host', 'smtp.gmail.com').strip()
        port = request.form.get('smtp_port', 587, type=int)
        if email and password:
            existing = EmailAccount.query.filter_by(email_address=email).first()
            if existing:
                existing.app_password = password
                existing.smtp_host = host
                existing.smtp_port = port
            else:
                acc = EmailAccount(email_address=email, app_password=password, smtp_host=host, smtp_port=port)
                db.session.add(acc)
            db.session.commit()
            flash('Account saved.', 'success')
        return redirect(url_for('accounts'))

    accounts_list = EmailAccount.query.order_by(EmailAccount.id).all()
    settings = Settings.get_singleton()
    return render_template('accounts.html', accounts=accounts_list, settings=settings)


@app.route('/accounts/<int:account_id>/delete', methods=['POST'])
def account_delete(account_id):
    acc = EmailAccount.query.get_or_404(account_id)
    db.session.delete(acc)
    db.session.commit()
    flash('Account deleted.', 'success')
    return redirect(url_for('accounts'))


@app.route('/accounts/<int:account_id>/test', methods=['POST'])
def account_test(account_id):
    acc = EmailAccount.query.get_or_404(account_id)
    result = send_test_email(acc)
    return jsonify(result)


@app.route('/accounts/<int:account_id>/warmup', methods=['POST'])
def account_warmup(account_id):
    acc = EmailAccount.query.get_or_404(account_id)
    acc.warmup_enabled = request.form.get('warmup_enabled') == 'on'
    db.session.commit()
    return redirect(url_for('accounts'))


# ─── Templates ───────────────────────────────────────────────────────────────

@app.route('/templates', methods=['GET', 'POST'])
def templates_page():
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        step = request.form.get('step', 1, type=int)
        subject = request.form.get('subject', '').strip()
        body = request.form.get('body', '').strip()
        is_html = request.form.get('is_html') == 'on'
        if name and subject and body:
            t = Template(name=name, step=step, subject=subject, body=body, is_html=is_html)
            db.session.add(t)
            db.session.commit()
            flash('Template created.', 'success')
        return redirect(url_for('templates_page'))

    templates = Template.query.order_by(Template.step, Template.id).all()
    return render_template('templates.html', templates=templates)


@app.route('/templates/<int:template_id>/delete', methods=['POST'])
def template_delete(template_id):
    t = Template.query.get_or_404(template_id)
    db.session.delete(t)
    db.session.commit()
    flash('Template deleted.', 'success')
    return redirect(url_for('templates_page'))


@app.route('/templates/<int:template_id>/preview')
def template_preview(template_id):
    return jsonify(preview_template(template_id))


# ─── Logs ────────────────────────────────────────────────────────────────────

@app.route('/logs')
def logs_page():
    account = request.args.get('account', '')
    step = request.args.get('step', '')
    date_from = request.args.get('date_from', '')
    date_to = request.args.get('date_to', '')

    query = EmailLog.query.filter_by(log_type='campaign')
    if account:
        query = query.filter(EmailLog.account_used == account)
    if step:
        try:
            query = query.filter(EmailLog.step == int(step))
        except ValueError:
            pass
    if date_from:
        try:
            query = query.filter(EmailLog.sent_at >= datetime.strptime(date_from, '%Y-%m-%d'))
        except ValueError:
            pass
    if date_to:
        try:
            query = query.filter(EmailLog.sent_at < datetime.strptime(date_to, '%Y-%m-%d') + timedelta(days=1))
        except ValueError:
            pass

    logs = query.order_by(EmailLog.sent_at.desc()).limit(500).all()
    accounts_list = EmailAccount.query.all()
    return render_template(
        'logs.html',
        logs=logs,
        accounts=accounts_list,
        filters={'account': account, 'step': step, 'date_from': date_from, 'date_to': date_to},
    )


@app.route('/logs/export')
def logs_export():
    account = request.args.get('account', '')
    step = request.args.get('step', '')
    date_from = request.args.get('date_from', '')
    date_to = request.args.get('date_to', '')

    query = EmailLog.query.filter_by(log_type='campaign')
    if account:
        query = query.filter(EmailLog.account_used == account)
    if step:
        try:
            query = query.filter(EmailLog.step == int(step))
        except ValueError:
            pass
    if date_from:
        try:
            query = query.filter(EmailLog.sent_at >= datetime.strptime(date_from, '%Y-%m-%d'))
        except ValueError:
            pass
    if date_to:
        try:
            query = query.filter(EmailLog.sent_at < datetime.strptime(date_to, '%Y-%m-%d') + timedelta(days=1))
        except ValueError:
            pass

    logs = query.order_by(EmailLog.sent_at.desc()).all()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['Lead Name', 'Email', 'Subject', 'Step', 'Sent At', 'Opened', 'Opened At', 'Clicked', 'Clicked At', 'Account', 'Status'])
    for log in logs:
        writer.writerow([
            log.lead_name or '', log.lead_email or '', log.subject or '', log.step,
            log.sent_at.isoformat() if log.sent_at else '',
            'Yes' if log.opened_at else 'No',
            log.opened_at.isoformat() if log.opened_at else '',
            'Yes' if log.clicked else 'No',
            log.clicked_at.isoformat() if log.clicked_at else '',
            log.account_used or '', log.status or '',
        ])
    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': 'attachment; filename=email_logs.csv'},
    )


# ─── Settings ────────────────────────────────────────────────────────────────

@app.route('/settings', methods=['GET', 'POST'])
def settings_page():
    settings = Settings.get_singleton()
    if request.method == 'POST':
        settings.sender_name = request.form.get('sender_name', '').strip()
        settings.video_link_url = request.form.get('video_link_url', '').strip()
        settings.warmup_addresses = request.form.get('warmup_addresses', '').strip()
        settings.tracking_base_url = request.form.get('tracking_base_url', '').strip()
        settings.pitch_text = request.form.get('pitch_text', '').strip()
        settings.send_window_start = request.form.get('send_window_start', '09:00')
        settings.send_window_end = request.form.get('send_window_end', '18:00')
        settings.timezone = request.form.get('timezone', 'Asia/Kolkata')
        days = request.form.getlist('active_days')
        settings.active_days = ','.join(days) if days else 'Mon,Tue,Wed,Thu,Fri'
        settings.daily_limit_per_account = request.form.get('daily_limit_per_account', 15, type=int)
        db.session.commit()
        flash('Settings saved.', 'success')
        return redirect(url_for('settings_page'))

    timezones = [
        'Asia/Kolkata', 'America/New_York', 'America/Los_Angeles',
        'America/Chicago', 'America/Denver', 'Europe/London',
        'Europe/Berlin', 'Europe/Paris', 'Asia/Dubai', 'Asia/Singapore',
        'Asia/Tokyo', 'Australia/Sydney', 'UTC',
    ]
    weekdays = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
    active_days = [d.strip() for d in (settings.active_days or '').split(',') if d.strip()]
    return render_template(
        'settings.html',
        settings=settings,
        timezones=timezones,
        weekdays=weekdays,
        active_days=active_days,
    )


# ─── Tracking ────────────────────────────────────────────────────────────────

TRACKING_GIF = base64.b64decode('R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7')


@app.route('/track/open/<int:lead_id>/<int:step>')
def track_open(lead_id, step):
    lead = Lead.query.get(lead_id)
    if lead:
        lead.opened = True
        if not lead.opened_at:
            lead.opened_at = datetime.utcnow()
        log = EmailLog.query.filter_by(lead_id=lead_id, step=step).order_by(EmailLog.sent_at.desc()).first()
        if log:
            log.opened_at = log.opened_at or datetime.utcnow()
            log.open_count = (log.open_count or 0) + 1
        db.session.commit()
    # Bug fix #8: Cache-Control headers so pixel fires every open
    response = Response(TRACKING_GIF, mimetype='image/gif')
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response


@app.route('/track/click/<int:lead_id>/<int:step>')
def track_click(lead_id, step):
    url = request.args.get('url', '/')
    lead = Lead.query.get(lead_id)
    if lead:
        lead.clicked = True
        if not lead.clicked_at:
            lead.clicked_at = datetime.utcnow()
        log = EmailLog.query.filter_by(lead_id=lead_id, step=step).order_by(EmailLog.sent_at.desc()).first()
        if log:
            log.clicked = True
            log.clicked_at = log.clicked_at or datetime.utcnow()
        db.session.commit()
    return redirect(url)


@app.route('/unsubscribe/<int:lead_id>')
def unsubscribe(lead_id):
    lead = Lead.query.get(lead_id)
    if lead:
        lead.unsubscribed = True
        db.session.commit()
        name = lead.first_name or 'there'
        return render_template('unsubscribe.html', name=name)
    return render_template('unsubscribe.html', name='there')


if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
