from apscheduler.schedulers.asyncio import AsyncIOScheduler
from core.fechamento import fechar_painel

def iniciar_scheduler(bot):
    scheduler = AsyncIOScheduler()

    for hora in [8, 15, 22]:
        scheduler.add_job(fechar_painel, 'cron', hour=hora, args=[bot, "T1"])
        scheduler.add_job(fechar_painel, 'cron', hour=hora, args=[bot, "T2"])
        scheduler.add_job(fechar_painel, 'cron', hour=hora, args=[bot, "T3"])

    scheduler.start()
