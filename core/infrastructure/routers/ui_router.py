from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse

router = APIRouter(tags=["UI Views"])
templates = Jinja2Templates(directory="ui/templates")


@router.get("/", response_class=HTMLResponse)
async def auth_page(request: Request):
    return templates.TemplateResponse(request, "auth.html", {"request": request})


@router.get("/tournaments", response_class=HTMLResponse)
async def tournaments_page(request: Request):
    return templates.TemplateResponse(request, "tournaments.html", {"request": request})


@router.get("/court-selection", response_class=HTMLResponse)   # ← new
async def court_selection_page(request: Request):
    return templates.TemplateResponse(request, "court_selection.html", {"request": request})


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page(request: Request):
    return templates.TemplateResponse(request, "dashboard.html", {"request": request})