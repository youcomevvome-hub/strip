import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from .config import get_settings
from .pipeline import run_pipeline_all

logger = logging.getLogger(__name__)
_scheduler: BackgroundScheduler | None = None


def start_scheduler() -> None:
    global _scheduler
    if _scheduler:
        return
    s = get_settings()
    _scheduler = BackgroundScheduler(timezone="UTC")
    _scheduler.add_job(
        run_pipeline_all,
        CronTrigger(hour=s.SCRAPE_CRON_HOUR, minute=s.SCRAPE_CRON_MINUTE),
        id="daily-scrape",
        replace_existing=True,
        max_instances=1,
    )
    _scheduler.start()
    logger.info("Scheduler started: daily at %02d:%02d UTC", s.SCRAPE_CRON_HOUR, s.SCRAPE_CRON_MINUTE)


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        _scheduler = None
