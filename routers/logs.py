from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Request

router = APIRouter()

SEVERITY_CHOICES = [("", "any"), ("3", "error or worse"), ("4", "warning or worse"), ("6", "info or worse")]
HOURS_CHOICES = [(1, "last hour"), (24, "last 24 hours"), (168, "last 7 days"), (720, "last 30 days"), (0, "all")]


@router.get("/logs")
def logs_page(request: Request, device: int = 0, severity: str = "", topic: str = "",
              q: str = "", hours: int = 24, before: int = 0, limit: int = 200):
    state = request.app.state
    limit = max(10, min(limit, 1000))
    since = None
    if hours > 0:
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="milliseconds")
    entries = state.logs_repo.search(
        device_id=device or None,
        max_severity=int(severity) if severity.isdigit() else None,
        topic=topic.strip(), text=q.strip(), since_iso=since,
        before_id=before or None, limit=limit + 1)
    more = len(entries) > limit
    entries = entries[:limit]
    return state.templates.TemplateResponse("logs.html", {
        "request": request, "active": "logs", "entries": entries,
        "next_before": entries[-1].id if more and entries else 0,
        "devices": state.devices_repo.list_all(), "status": state.log_service.status(),
        "f": {"device": device, "severity": severity, "topic": topic, "q": q, "hours": hours, "limit": limit},
        "severity_choices": SEVERITY_CHOICES, "hours_choices": HOURS_CHOICES,
    })
