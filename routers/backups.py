from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, FileResponse

router = APIRouter(prefix="/backups")


@router.get("")
def list_backups(request: Request):
    devices = request.app.state.devices_repo.list_all()
    recent = request.app.state.backups_repo.list_recent()
    return request.app.state.templates.TemplateResponse("backups.html", {
        "request": request, "devices": devices, "recent": recent, "active": "backups",
    })


@router.post("/api/run")
async def run_backup(request: Request):
    """Body: {"device_ids": [int, ...]}  (empty/omitted = all devices)"""
    body = await request.json()
    device_ids = body.get("device_ids") or []
    devices_repo = request.app.state.devices_repo
    devices = devices_repo.get_many(device_ids) if device_ids else devices_repo.list_all()

    results = await request.app.state.backup_service.backup_many(devices)
    return JSONResponse({"results": results})


@router.get("/download/{backup_id}")
def download_backup(backup_id: int, request: Request):
    backup = request.app.state.backups_repo.get(backup_id)
    if not backup:
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(backup.path, filename=backup.filename, media_type="text/plain")
