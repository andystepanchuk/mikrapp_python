from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

import config
from db.connection import Database
from db.sites_repo import SiteRepository
from db.devices_repo import DeviceRepository
from db.templates_repo import TemplateRepository
from db.backups_repo import BackupRepository
from db.topology_repo import TopologyRepository
from db.check_history_repo import CheckHistoryRepository
from db.logs_repo import LogRepository
from services.device_service import DeviceService
from services.backup_service import BackupService
from services.port_traffic_service import PortTrafficService
from services.device_detail_service import DeviceDetailService
from services.scheduler_service import Scheduler
from services.check_recorder import CheckRecorder
from services.log_service import LogService
from routers import auth, dashboard, sites, devices, commands, backups, topology, logs
from routers.auth import AuthMiddleware


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start the history writer, the syslog listener and the scheduler with the application;
    stop them cleanly on shutdown (scheduler first, so the last checks still reach the history)."""
    await app.state.recorder.start()
    await app.state.log_service.start()
    await app.state.scheduler.start()
    try:
        yield
    finally:
        await app.state.scheduler.stop()
        await app.state.log_service.stop()
        await app.state.recorder.stop()


def create_app() -> FastAPI:
    app = FastAPI(title="MikrApp", lifespan=lifespan)

    # Sessions first (outermost), then auth check on top of them.
    app.add_middleware(AuthMiddleware)
    app.add_middleware(SessionMiddleware, secret_key=config.SESSION_SECRET)

    app.mount("/static", StaticFiles(directory="static"), name="static")
    app.state.templates = Jinja2Templates(directory="templates")

    db = Database(config.DB_PATH)
    app.state.sites_repo = SiteRepository(db)
    app.state.devices_repo = DeviceRepository(db)
    app.state.templates_repo = TemplateRepository(db)
    app.state.backups_repo = BackupRepository(db)
    app.state.topo_repo = TopologyRepository(db)

    app.state.history_repo = CheckHistoryRepository(db)
    app.state.recorder = CheckRecorder(
        config.CHECK_LOG_DIR if config.CHECK_LOG_ENABLED else None, config.CHECK_LOG_DAYS,
        app.state.history_repo if config.CHECK_HISTORY_ENABLED else None, config.CHECK_HISTORY_DAYS)

    app.state.device_service = DeviceService(
        app.state.devices_repo, recorder=app.state.recorder, topology_repo=app.state.topo_repo,
        traffic=PortTrafficService(app.state.topo_repo))
    app.state.backup_service = BackupService(app.state.devices_repo, app.state.backups_repo)
    app.state.device_detail_service = DeviceDetailService(app.state.devices_repo, app.state.backups_repo)
    app.state.logs_repo = LogRepository(db)
    app.state.log_service = LogService(app.state.devices_repo, app.state.logs_repo,
                                       config.SYSLOG_HOST, config.SYSLOG_PORT, config.SYSLOG_DAYS,
                                       config.SYSLOG_ENABLED)
    app.state.scheduler = Scheduler(app.state.device_service, config.CHECK_INTERVAL,
                                    config.SCHEDULER_ENABLED)
    app.state.scheduler.add_job("delete old device logs", app.state.log_service.purge_job,
                                3600, run_now=False)

    app.include_router(auth.router)
    app.include_router(dashboard.router)
    app.include_router(sites.router)
    app.include_router(devices.router)
    app.include_router(commands.router)
    app.include_router(backups.router)
    app.include_router(topology.router)
    app.include_router(logs.router)

    return app


app = create_app()
