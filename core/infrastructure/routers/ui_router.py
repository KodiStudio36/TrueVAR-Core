import os
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse

# Absolute, so it works whatever the working directory is (the core used as
# a library). TRUEVAR_TEMPLATES_DIR overrides it, e.g. for a frozen app.
TEMPLATES_DIR = Path(os.environ.get("TRUEVAR_TEMPLATES_DIR")
                     or Path(__file__).resolve().parents[3] / "ui" / "core" / "templates")

router = APIRouter(tags=["UI Views"])
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


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