import config
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter()


@router.get("/")
def dashboard(request: Request):
    devices_repo = request.app.state.devices_repo
    devices = devices_repo.list_all()
    stats = devices_repo.stats()
    return request.app.state.templates.TemplateResponse("dashboard.html", {
        "request": request, "devices": devices, "stats": stats, "active": "dashboard",
        "stamp": devices_repo.last_check_stamp(), "interval": config.CHECK_INTERVAL,
    })


@router.get("/api/stamp")
def api_stamp(request: Request):
    """Cheap question the dashboard asks every interval: has any check finished since I loaded?"""
    return JSONResponse({
        "stamp": request.app.state.devices_repo.last_check_stamp(),
        "interval": config.CHECK_INTERVAL,
    })


@router.post("/api/check-all")
async def api_check_all(request: Request):
    service = request.app.state.device_service
    results = await service.check_all(full=True)
    return JSONResponse({
        "results": [
            {"device_id": r.device_id, "status": r.status, "error": r.error}
            for r in results
        ]
    })


@router.post("/api/check/{device_id}")
async def api_check_one(device_id: int, request: Request):
    devices_repo = request.app.state.devices_repo
    device = devices_repo.get(device_id)
    if not device:
        return JSONResponse({"error": "not found"}, status_code=404)
    result = await request.app.state.device_service.check_device(device, full=True)
    return JSONResponse({"device_id": result.device_id, "status": result.status, "error": result.error})
