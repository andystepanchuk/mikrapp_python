import asyncio
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import List

import config
from db.devices_repo import DeviceRepository
from db.backups_repo import BackupRepository
from models import Device
from services.client_factory import ClientFactory


def _safe_name(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", text) or "device"


class BackupService:
    def __init__(self, devices_repo: DeviceRepository, backups_repo: BackupRepository,
                 max_concurrency: int = config.MAX_CONCURRENT_CHECKS):
        self.devices_repo = devices_repo
        self.backups_repo = backups_repo
        self.clients = ClientFactory(devices_repo)
        self._semaphore = asyncio.Semaphore(max_concurrency)
        Path(config.BACKUP_DIR).mkdir(parents=True, exist_ok=True)

    async def backup_device(self, device: Device) -> dict:
        if device.kind == "generic":
            return {"device_id": device.id, "ok": False, "error": "generic device - nothing to back up"}
        async with self._semaphore:
            try:
                client = await self.clients.for_device(device)
                export_text = await client.export_config()
            except Exception as exc:
                return {"device_id": device.id, "ok": False, "error": str(exc)}

            timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            filename = f"{_safe_name(device.display_name)}_{timestamp}.rsc"
            device_dir = Path(config.BACKUP_DIR) / str(device.id)
            device_dir.mkdir(parents=True, exist_ok=True)
            path = device_dir / filename

            def _write():
                path.write_text(export_text, encoding="utf-8")
                return path.stat().st_size

            size = await asyncio.to_thread(_write)
            created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            await asyncio.to_thread(
                self.backups_repo.add, device.id, filename, str(path), size, created_at
            )
            return {"device_id": device.id, "ok": True, "filename": filename, "size": size}

    async def backup_many(self, devices: List[Device]) -> list:
        if not devices:
            return []
        return await asyncio.gather(*(self.backup_device(d) for d in devices))
