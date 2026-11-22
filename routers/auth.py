from fastapi import APIRouter, Request, Form
from fastapi.responses import RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware

from services.auth_service import AuthService

router = APIRouter()

PUBLIC_PATHS = ("/login", "/static")


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if not any(path == p or path.startswith(p + "/") for p in PUBLIC_PATHS):
            if not request.session.get("authenticated"):
                return RedirectResponse("/login")
        return await call_next(request)


@router.get("/login")
def login_form(request: Request):
    templates = request.app.state.templates
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@router.post("/login")
def login_submit(request: Request, username: str = Form(...), password: str = Form(...)):
    templates = request.app.state.templates
    if AuthService.verify(username, password):
        request.session["authenticated"] = True
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        "login.html", {"request": request, "error": "Invalid username or password"}
    )


@router.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
