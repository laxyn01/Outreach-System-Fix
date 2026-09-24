from apscheduler.schedulers.background import BackgroundScheduler

scheduler = BackgroundScheduler()


def start_scheduler(app):
    from email_sender import try_send_next_email
    from imap_replies import check_replies
    from models import db
    from warmup import (
        process_warmup_opens,
        process_warmup_replies,
        scan_warmup_inboxes,
        scan_warmup_inboxes_outlook,
        send_warmup_round,
    )

    def send_job():
        # UNCHANGED real-campaign sender.
        # The only edit: the old try_send_warmup_email() call was removed from
        # here. That function incremented EmailAccount.daily_sent_count, so
        # warmup was eating real campaign sending capacity. Warmup now runs in
        # its own job below with its own counter. The old function is still
        # present in email_sender.py (untouched) but is no longer called.
        with app.app_context():
            try:
                result = try_send_next_email()
                print(f'[SCHEDULER] send_job result: {result}', flush=True)
            except Exception as e:
                print(f'[SCHEDULER] send_job ERROR: {e}', flush=True)

    def reply_job():
        with app.app_context():
            try:
                check_replies()
            except Exception:
                pass

    # ── Warmup jobs (independent of send_job / reply_job) ──────────────────

    def _run_warmup(name, fn):
        with app.app_context():
            try:
                result = fn()
                if result and (result.get('sent') or result.get('opened')
                               or result.get('replied') or result.get('scheduled')):
                    print(f'[WARMUP] {name}: {result}', flush=True)
                if result and result.get('errors'):
                    print(f'[WARMUP] {name} errors: {result["errors"]}', flush=True)
            except Exception as e:
                print(f'[WARMUP] {name} ERROR: {e}', flush=True)
                try:
                    db.session.rollback()
                except Exception:
                    pass
            finally:
                try:
                    db.session.remove()
                except Exception:
                    pass

    def warmup_send_job():
        _run_warmup('send', send_warmup_round)

    def warmup_open_job():
        _run_warmup('open', process_warmup_opens)

    def warmup_reply_job():
        _run_warmup('reply', process_warmup_replies)

    def warmup_inbox_job():
        _run_warmup('inbox_scan', scan_warmup_inboxes)

    # NEW (OutreachCommand Outlook/Graph warmup task) — sibling job for the
    # Outlook-only scan_warmup_inboxes_outlook(). Runs independently of
    # warmup_inbox_job above; the Gmail job and its schedule are untouched.
    def warmup_inbox_job_outlook():
        _run_warmup('inbox_scan_outlook', scan_warmup_inboxes_outlook)

    if not scheduler.running:
        scheduler.add_job(send_job, 'interval', minutes=1, id='send_job', replace_existing=True)
        scheduler.add_job(reply_job, 'interval', hours=4, id='reply_job', replace_existing=True)
        # NEW warmup jobs
        scheduler.add_job(
            warmup_send_job, 'interval', minutes=2, id='warmup_send_job',
            replace_existing=True, max_instances=1, misfire_grace_time=120,
        )
        scheduler.add_job(
            warmup_open_job, 'interval', minutes=10, jitter=120, id='warmup_open_job',
            replace_existing=True, max_instances=1, misfire_grace_time=300,
        )
        scheduler.add_job(
            warmup_reply_job, 'interval', minutes=10, jitter=120, id='warmup_reply_job',
            replace_existing=True, max_instances=1, misfire_grace_time=300,
        )
        scheduler.add_job(
            warmup_inbox_job, 'interval', minutes=30, jitter=180, id='warmup_inbox_job',
            replace_existing=True, max_instances=1, misfire_grace_time=600,
        )
        # NEW (OutreachCommand Outlook/Graph warmup task) — additive job,
        # same cadence as the Gmail inbox-scan job above, but a separate
        # scheduler id so it can be paused/removed independently if needed.
        scheduler.add_job(
            warmup_inbox_job_outlook, 'interval', minutes=30, jitter=180, id='warmup_inbox_job_outlook',
            replace_existing=True, max_instances=1, misfire_grace_time=600,
        )
        scheduler.start()
