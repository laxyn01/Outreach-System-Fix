from apscheduler.schedulers.background import BackgroundScheduler

scheduler = BackgroundScheduler()


def start_scheduler(app):
    from email_sender import try_send_next_email, try_send_warmup_email
    from imap_replies import check_replies

    def send_job():
        with app.app_context():
            try:
                result = try_send_next_email()
                print(f'[SCHEDULER] send_job result: {result}', flush=True)
            except Exception as e:
                print(f'[SCHEDULER] send_job ERROR: {e}', flush=True)
            try:
                try_send_warmup_email()
            except Exception as e:
                print(f'[SCHEDULER] warmup_job ERROR: {e}', flush=True)

    def reply_job():
        with app.app_context():
            try:
                check_replies()
            except Exception:
                pass

    if not scheduler.running:
        scheduler.add_job(send_job, 'interval', minutes=1, id='send_job', replace_existing=True)
        scheduler.add_job(reply_job, 'cron', hour=9, id='reply_job', replace_existing=True)
        scheduler.start()
