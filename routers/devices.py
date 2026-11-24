from datetime import datetime, timezone
from fastapi import APIRouter, Request, Form
from fastapi.responses import RedirectResponse

router = APIRouter(prefix="/devices")


def _clean_agent(agent_id: int, own_id: int = 0):
    """0 / empty means 'direct'; a device can never be its own agent."""
    return agent_id if agent_id and agent_id != own_id else None


def _clean_kind(kind: str) -> str:
    return "generic" if kind == "generic" else "mikrotik"


def _format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    if d:
        return f"{d}d {h}h {m}m"
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def _offline_duration(device) -> str:
    raw = device.offline_since or device.last_seen
    if device.status == "online" or not raw:
        return ""
    try:
        ts = datetime.fromisoformat(raw)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
    except ValueError:
        return ""
    return _format_duration((datetime.now(timezone.utc) - ts).total_seconds())


@router.get("")
def list_devices(request: Request):
    devices = request.app.state.devices_repo.list_all()
    sites = request.app.state.sites_repo.list_all()
    agents = request.app.state.devices_repo.list_agent_candidates()
    return request.app.state.templates.TemplateResponse("devices.html", {
        "request": request, "devices": devices, "sites": sites, "agents": agents,
        "active": "devices",
    })


@router.post("/add")
def add_device(request: Request, site_id: int = Form(...), ip: str = Form(...),
                username: str = Form("admin"), password: str = Form(""),
                protocol: str = Form("auto"), port: int = Form(22),
                api_port: int = Form(8728), kind: str = Form("mikrotik"),
                agent_id: int = Form(0), name: str = Form("")):
    kind = _clean_kind(kind)
    if kind == "generic":
        username, password = "", ""      # ping-only hosts have no credentials
    request.app.state.devices_repo.create(
        site_id, ip.strip(), username, password, protocol, port, api_port,
        kind=kind, agent_id=_clean_agent(agent_id), custom_identity=name.strip())
    return RedirectResponse("/devices", status_code=303)


@router.post("/mass-add")
def mass_add_devices(request: Request, site_id: int = Form(...),
                      lines: str = Form(...), username: str = Form("admin"),
                      password: str = Form(""), protocol: str = Form("auto"),
                      port: int = Form(22), api_port: int = Form(8728),
                      kind: str = Form("mikrotik"), agent_id: int = Form(0)):
    """
    One device per line.
      MikroTik: `ip` or `ip,username,password`
      Generic (ping-only, needs an agent): `ip` or `ip,display name`
    """
    repo = request.app.state.devices_repo
    kind = _clean_kind(kind)
    agent = _clean_agent(agent_id)
    for raw_line in lines.splitlines():
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        parts = [p.strip() for p in raw_line.split(",")]
        ip = parts[0]
        if kind == "generic":
            repo.create(site_id, ip, "", "", protocol, port, api_port,
                        kind="generic", agent_id=agent,
                        custom_identity=parts[1] if len(parts) > 1 else "")
            continue
        user = parts[1] if len(parts) > 1 and parts[1] else username
        pwd = parts[2] if len(parts) > 2 and parts[2] else password
        repo.create(site_id, ip, user, pwd, protocol, port, api_port,
                    kind="mikrotik", agent_id=agent)
    return RedirectResponse("/devices", status_code=303)


@router.post("/{device_id}/edit")
def edit_device(device_id: int, request: Request, site_id: int = Form(...),
                 ip: str = Form(...), username: str = Form(""),
                 password: str = Form(""), protocol: str = Form("auto"),
                 port: int = Form(22), api_port: int = Form(8728),
                 custom_identity: str = Form(""), kind: str = Form("mikrotik"),
                 agent_id: int = Form(0)):
    repo = request.app.state.devices_repo
    kind = _clean_kind(kind)
    if kind == "generic":
        username, password = "", ""
    elif not password:
        # blank means "keep current" -- otherwise every edit would wipe it
        existing = repo.get(device_id)
        password = existing.password if existing else ""
    repo.update_connection(
        device_id, site_id, ip.strip(), username, password, protocol,
        port, api_port, custom_identity.strip(), kind=kind,
        agent_id=_clean_agent(agent_id, device_id),
    )
    return RedirectResponse(f"/devices/{device_id}", status_code=303)


@router.post("/{device_id}/delete")
def delete_device(device_id: int, request: Request):
    request.app.state.devices_repo.delete(device_id)
    return RedirectResponse("/devices", status_code=303)


@router.get("/{device_id}")
async def device_detail(device_id: int, request: Request):
    devices_repo = request.app.state.devices_repo
    sites = request.app.state.sites_repo.list_all()
    device = devices_repo.get(device_id)
    if not device:
        return RedirectResponse("/devices", status_code=303)

    detail = await request.app.state.device_detail_service.get_detail(device)
    return request.app.state.templates.TemplateResponse("device_detail.html", {
        "request": request, "active": "devices", "device": device, "detail": detail,
        "sites": sites,
        "history": request.app.state.history_repo.latest_for_device(device_id, 50),
        "logs": request.app.state.logs_repo.latest_for_device(device_id, 50),
        "agents": [a for a in devices_repo.list_agent_candidates() if a.id != device.id], "offline_for": _offline_duration(device),
    })
