from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse

import config
from services.topology_view import build_view

router = APIRouter(prefix="/topology")

MAX_PORT_NAME = 64


def _view(state, site: int):
    devices = state.devices_repo.list_all()
    shown = [d for d in devices if not site or d.site_id == site]
    view = build_view(shown, state.topo_repo.list_links(), state.topo_repo.positions(),
                      state.topo_repo.counters(), config.CHECK_INTERVAL)
    if view["new_positions"]:                       # a spot is fixed the first time a device is shown
        state.topo_repo.save_positions(view["new_positions"])
    view.pop("new_positions")
    view["config"]["site"] = site
    return view, devices


@router.get("")
def topology_page(request: Request, site: int = 0, error: str = ""):
    state = request.app.state
    view, devices = _view(state, site)
    return state.templates.TemplateResponse("topology.html", {
        "request": request, "active": "topology", "topo": view, "devices": devices,
        "sites": state.sites_repo.list_all(), "site_filter": site, "error": error[:200],
    })


@router.get("/api/state")
def api_state(request: Request, site: int = 0):
    view, _ = _view(request.app.state, site)
    return JSONResponse(view)


@router.post("/api/position")
async def api_position(request: Request):
    state = request.app.state
    try:
        body = await request.json()
        device_id, x, y = int(body["device_id"]), int(body["x"]), int(body["y"])
    except Exception:
        return JSONResponse({"error": "bad request"}, status_code=400)
    if state.devices_repo.get(device_id) is None:
        return JSONResponse({"error": "device not found"}, status_code=404)
    state.topo_repo.save_positions({device_id: (max(0, min(x, 5000)), max(0, min(y, 5000)))})
    return JSONResponse({"ok": True})


@router.post("/api/reset-layout")
def api_reset_layout(request: Request):
    request.app.state.topo_repo.clear_positions()      # devices get fresh automatic spots on the next view
    return JSONResponse({"ok": True})


def _checked(state, a_id: int, a_port: str, b_id: int, b_port: str):
    """(error message or None, cleaned ports)."""
    a_port, b_port = a_port.strip()[:MAX_PORT_NAME], b_port.strip()[:MAX_PORT_NAME]
    if not a_id or not b_id:
        return "Choose both devices.", a_port, b_port
    if a_id == b_id:
        return "A device cannot be linked to itself.", a_port, b_port
    if state.devices_repo.get(a_id) is None or state.devices_repo.get(b_id) is None:
        return "Device not found.", a_port, b_port
    return None, a_port, b_port


def _back(error: str = ""):
    return RedirectResponse("/topology" + (f"?error={quote(error)}" if error else ""), status_code=303)


@router.post("/links/add")
def add_link(request: Request, a_device_id: int = Form(0), a_port: str = Form(""),
             b_device_id: int = Form(0), b_port: str = Form("")):
    state = request.app.state
    error, a_port, b_port = _checked(state, a_device_id, a_port, b_device_id, b_port)
    if error:
        return _back(error)
    state.topo_repo.add_link(a_device_id, a_port, b_device_id, b_port)
    return _back()


@router.post("/links/{link_id}/edit")
def edit_link(link_id: int, request: Request, a_device_id: int = Form(0), a_port: str = Form(""),
              b_device_id: int = Form(0), b_port: str = Form("")):
    state = request.app.state
    if state.topo_repo.get_link(link_id) is None:
        return _back("Link not found.")
    error, a_port, b_port = _checked(state, a_device_id, a_port, b_device_id, b_port)
    if error:
        return _back(error)
    state.topo_repo.update_link(link_id, a_device_id, a_port, b_device_id, b_port)
    return _back()


@router.post("/links/{link_id}/delete")
def delete_link(link_id: int, request: Request):
    request.app.state.topo_repo.delete_link(link_id)
    return _back()
