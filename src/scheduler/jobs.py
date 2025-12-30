from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
import asyncio
from sqlalchemy.future import select
from sqlalchemy import func
from database.db import AsyncSessionLocal
from database.models import Detection, Track

scheduler = AsyncIOScheduler()

async def prepare_human_review():
    """
    Periodic task to check for uncertain detections and flag them for review.
    In a real system, this might move data to a separate 'review_queue' table
    or send notifications. For now, we just log the count.
    """
    async with AsyncSessionLocal() as session:
        # Count uncertain detections
        stmt = select(func.count()).select_from(Detection).where(Detection.is_uncertain == True)
        result = await session.execute(stmt)
        count = result.scalar()
        
        print(f"[Scheduler] Found {count} uncertain detections pending review.")
        
        # If count > threshold, we could trigger alerts here.

async def log_ready_datasets():
    """
    Check if we have enough labeled data to trigger a fine-tuning run (manual).
    """
    async with AsyncSessionLocal() as session:
        # Count confirmed detections
        stmt = select(func.count()).select_from(Detection).where(Detection.human_label != None)
        result = await session.execute(stmt)
        count = result.scalar()
        
        if count > 500:
            print(f"[Scheduler] DATASET READY: {count} labeled examples available for fine-tuning.")

def start_scheduler():
    scheduler.add_job(prepare_human_review, IntervalTrigger(minutes=60)) # Check every hour
    scheduler.add_job(log_ready_datasets, IntervalTrigger(minutes=60))
    scheduler.start()
    print("Scheduler started.")

if __name__ == "__main__":
    # Test run
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    start_scheduler()
    try:
        loop.run_forever()
    except (KeyboardInterrupt, SystemExit):
        pass
