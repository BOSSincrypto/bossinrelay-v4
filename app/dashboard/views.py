"""HTML-страницы админки (Jinja). Данные подтягивают через /admin/* API."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .auth import optional_admin
from .store import MODEL_REGISTRY, STORE, VERDICT_LABELS

router = APIRouter()
templates = Jinja2Templates(directory="app/dashboard/templates")

NAV = [
    ("overview", "Обзор", "/"),
    ("providers", "Провайдеры и ключи", "/providers"),
    ("pools", "Пулы и модели", "/pools"),
    ("cache", "Кэш", "/cache"),
    ("sessions", "Сессии", "/sessions"),
    ("quotas", "Квоты", "/quotas"),
    ("settings", "Настройки", "/settings"),
]


def page(request: Request, name: str, tab: str, **ctx):
    ctx.update({"nav": NAV, "active": tab,
                "authed": bool(optional_admin(request))})
    return templates.TemplateResponse(request, f"{name}.html", ctx)


@router.get("/login", response_class=HTMLResponse)
def login(request: Request):
    return templates.TemplateResponse(request, "login.html",
                                      {"nav": NAV, "active": ""})


@router.get("/", response_class=HTMLResponse)
def overview(request: Request):
    if not optional_admin(request=request):
        return RedirectResponse("/login", status_code=302)
    return page(request, "overview", "overview")


# все страницы кроме главной отдают контент напрямую:
# Jinja подставляет seed из STORE, формы шлют fetch в /admin/*
@router.get("/providers", response_class=HTMLResponse)
def providers(request: Request):
    if not optional_admin(request=request):
        return RedirectResponse("/login", status_code=302)
    return page(request, "providers", "providers", providers=STORE["providers"],
                registry=MODEL_REGISTRY, verdict_labels=VERDICT_LABELS)


@router.get("/pools", response_class=HTMLResponse)
def pools(request: Request):
    if not optional_admin(request=request):
        return RedirectResponse("/login", status_code=302)
    prov_models = [f"{p['id']}:{m}" for p in STORE["providers"] for m in p["models"]]
    return page(request, "pools", "pools",
                pools=STORE["pools"], prov_models=prov_models)


@router.get("/cache", response_class=HTMLResponse)
def cache(request: Request):
    if not optional_admin(request=request):
        return RedirectResponse("/login", status_code=302)
    return page(request, "cache", "cache", cache=STORE["cache"])


@router.get("/sessions", response_class=HTMLResponse)
def sessions(request: Request):
    if not optional_admin(request=request):
        return RedirectResponse("/login", status_code=302)
    return page(request, "sessions", "sessions", sessions=STORE["sessions"])


@router.get("/quotas", response_class=HTMLResponse)
def quotas(request: Request):
    if not optional_admin(request=request):
        return RedirectResponse("/login", status_code=302)
    return page(request, "quotas", "quotas", quotas=STORE["quotas"])


@router.get("/settings", response_class=HTMLResponse)
def settings(request: Request):
    if not optional_admin(request=request):
        return RedirectResponse("/login", status_code=302)
    return page(request, "settings", "settings",
                settings=STORE["settings"], pools=STORE["pools"])
