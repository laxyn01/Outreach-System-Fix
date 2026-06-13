from apscheduler.schedulers.background import BackgroundScheduler

scheduler = BackgroundScheduler()


def start_scheduler(app):
    from email_sender import try_send_next_email, try_send_warmup_email
    from imap_replies import check_replies

    def send_job():
        with app.app_context():
            try_send_next_email()
            try_send_warmup_email()

    def reply_job():
        with app.app_context():
            check_replies()

    if not scheduler.running:
        scheduler.add_job(send_job, 'interval', minutes=1, id='send_job', replace_existing=True)
        scheduler.add_job(reply_job, 'cron', hour=9, id='reply_job', replace_existing=True)
        scheduler.start()
