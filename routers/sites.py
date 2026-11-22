from fastapi import APIRouter, Request, Form
from fastapi.responses import RedirectResponse

router = APIRouter(prefix="/sites")


@router.get("")
def list_sites(request: Request):
    sites = request.app.state.sites_repo.list_all()
    return request.app.state.templates.TemplateResponse(
        "sites.html", {"request": request, "sites": sites, "active": "sites"}
    )


@router.post("/add")
def add_site(request: Request, name: str = Form(...), location: str = Form(""),
             description: str = Form("")):
    request.app.state.sites_repo.create(name, location, description)
    return RedirectResponse("/sites", status_code=303)


@router.post("/{site_id}/edit")
def edit_site(site_id: int, request: Request, name: str = Form(...),
              location: str = Form(""), description: str = Form("")):
    request.app.state.sites_repo.update(site_id, name, location, description)
    return RedirectResponse("/sites", status_code=303)


@router.post("/{site_id}/delete")
def delete_site(site_id: int, request: Request):
    request.app.state.sites_repo.delete(site_id)
    return RedirectResponse("/sites", status_code=303)
