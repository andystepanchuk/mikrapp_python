import math
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter(prefix="/topology")


def _layout_devices(devices) -> dict:
    n = max(len(devices), 1)
    cx, cy, rx, ry = 500, 310, 360, 230
    positions = {}
    for i, d in enumerate(devices):
        angle = (2 * math.pi * i / n) - math.pi / 2
        jitter_x = ((d.id * 37) % 40) - 20
        jitter_y = ((d.id * 17) % 30) - 15
        positions[d.id] = {
            "x": round(cx + math.cos(angle) * rx + jitter_x),
            "y": round(cy + math.sin(angle) * ry + jitter_y),
        }
    return positions


def _layout_unmanaged(unmanaged_list: list) -> list:
    return [
        {"x": 60 + (i % 7) * 150, "y": 620 + (i // 7) * 52, "item": u}
        for i, u in enumerate(unmanaged_list)
    ]


@router.get("")
def topology_page(request: Request, site: int = 0, unmanaged: int = 1):
    devices_repo = request.app.state.devices_repo
    devices = devices_repo.list_all(site_id=site or None)
    dev_ids = {d.id for d in devices}
    sites = request.app.state.sites_repo.list_all()

    cache = request.app.state.topo_repo.get_cache() or {
        "links": [], "unmanaged": [], "scanned_at": None
    }
    links = [l for l in cache["links"] if l["from"] in dev_ids and l["to"] in dev_ids]
    unmanaged_list = cache["unmanaged"] if unmanaged else []
    if site:
        unmanaged_list = [u for u in unmanaged_list if u.get("from_device") in dev_ids]

    return request.app.state.templates.TemplateResponse("topology.html", {
        "request": request, "active": "topology",
        "devices": devices, "sites": sites, "site_filter": site,
        "show_unmanaged": bool(unmanaged),
        "links": links, "unmanaged": unmanaged_list,
        "positions": _layout_devices(devices),
        "unmanaged_positions": _layout_unmanaged(unmanaged_list),
        "scanned_at": cache["scanned_at"],
    })


@router.post("/api/scan")
async def scan_topology(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    site_id = body.get("site_id") or None
    result = await request.app.state.topology_service.scan(site_id=site_id)
    return JSONResponse({"links": len(result["links"]), "unmanaged": len(result["unmanaged"])})
