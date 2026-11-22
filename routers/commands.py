from fastapi import APIRouter, Request, Form
from fastapi.responses import RedirectResponse, JSONResponse

router = APIRouter(prefix="/commands")


@router.get("")
def list_templates(request: Request):
    templates = request.app.state.templates_repo.list_all()
    devices = request.app.state.devices_repo.list_all()
    return request.app.state.templates.TemplateResponse("commands.html", {
        "request": request, "cmd_templates": templates, "devices": devices, "active": "commands",
    })


@router.post("/add")
def add_template(request: Request, name: str = Form(...), category: str = Form("Custom"),
                  description: str = Form(""), commands: str = Form(...)):
    request.app.state.templates_repo.create(name, category, description, commands)
    return RedirectResponse("/commands", status_code=303)


@router.post("/{template_id}/edit")
def edit_template(template_id: int, request: Request, name: str = Form(...),
                   category: str = Form("Custom"), description: str = Form(""),
                   commands: str = Form(...)):
    request.app.state.templates_repo.update(template_id, name, category, description, commands)
    return RedirectResponse("/commands", status_code=303)


@router.post("/{template_id}/delete")
def delete_template(template_id: int, request: Request):
    request.app.state.templates_repo.delete(template_id)
    return RedirectResponse("/commands", status_code=303)


@router.post("/api/run")
async def run_template(request: Request):
    """Body: {"template_id": int, "device_ids": [int, ...]}"""
    body = await request.json()
    template = request.app.state.templates_repo.get(body.get("template_id"))
    if not template:
        return JSONResponse({"error": "template not found"}, status_code=404)

    device_ids = body.get("device_ids") or []
    devices = request.app.state.devices_repo.get_many(device_ids)
    commands = template.commands.splitlines()

    outputs = await request.app.state.device_service.run_commands(devices, commands)
    return JSONResponse({"outputs": outputs})
