import os
import sqlite3
import secrets
import io
from datetime import datetime
from typing import Optional, List

from fastapi import FastAPI, Request, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from passlib.hash import bcrypt

from docx import Document
from docx.shared import Inches

# ========= Config =========
APP_NAME = os.getenv("APP_NAME", "Otel Planlama Stüdyosu")
SECRET_KEY = os.getenv("SECRET_KEY", "change-me-please")
USAGE_DB_PATH = os.getenv("USAGE_DB_PATH", "/tmp/usage.db")

ADMIN_SEED_USER = os.getenv("ADMIN_CODE_USER", "admin")
ADMIN_SEED_PASS = os.getenv("ADMIN_CODE_PASS", "admin123")

# ========= Paths =========
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")
STATIC_DIR = os.path.join(BASE_DIR, "static")

os.makedirs(os.path.dirname(USAGE_DB_PATH), exist_ok=True)
os.makedirs(TEMPLATES_DIR, exist_ok=True)
os.makedirs(STATIC_DIR, exist_ok=True)

# ========= App =========
app = FastAPI(title=APP_NAME)
# Session middleware
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, same_site="lax", https_only=False)
templates = Jinja2Templates(directory=TEMPLATES_DIR)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ========= DB =========
def get_db():
    conn = sqlite3.connect(USAGE_DB_PATH, timeout=30, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def ensure_db():
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'user',
            created_at TEXT NOT NULL
        )
        """
    )
    conn.commit()

    # Seed admin if not exists
    cur.execute("SELECT id FROM users WHERE username = ?", (ADMIN_SEED_USER,))
    if not cur.fetchone():
        cur.execute(
            "INSERT INTO users (username, password_hash, role, created_at) VALUES (?, ?, ?, ?)",
            (ADMIN_SEED_USER, bcrypt.using(rounds=12).hash(ADMIN_SEED_PASS), "admin", datetime.utcnow().isoformat()),
        )
        conn.commit()
    conn.close()


# ========= CSRF =========
def get_csrf_token(request: Request) -> str:
    token = request.session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf_token"] = token
    return token


def verify_csrf(request: Request, token_from_form: str):
    token = request.session.get("csrf_token")
    if not token or not token_from_form or token_from_form != token:
        raise ValueError("CSRF doğrulaması başarısız")


# ========= Auth Helpers =========
def current_user(request: Request) -> Optional[dict]:
    return request.session.get("user")


def require_login_redirect(request: Request) -> Optional[RedirectResponse]:
    if not current_user(request):
        return RedirectResponse(url="/login", status_code=302)
    return None


def require_admin_redirect(request: Request) -> Optional[RedirectResponse]:
    user = current_user(request)
    if not user or user.get("role") != "admin":
        return RedirectResponse(url="/", status_code=302)
    return None


# ========= Routes =========
@app.on_event("startup")
def on_startup():
    ensure_db()
    print(f"[INFO] Using DB at: {USAGE_DB_PATH}")


@app.get("/health", response_class=PlainTextResponse)
def health():
    return "ok"


@app.get("/healthz", response_class=PlainTextResponse)
def healthz():
    return "ok"


# Render/ELB vs HEAD kontrolü 405 olmasın
@app.head("/", response_class=PlainTextResponse)
def head_root():
    return PlainTextResponse("ok")


# ---------- Auth ----------
@app.get("/login", response_class=HTMLResponse)
def login_get(request: Request):
    if current_user(request):
        return RedirectResponse(url="/", status_code=302)
    return templates.TemplateResponse(
        "login.html",
        {"request": request, "csrf_token": get_csrf_token(request), "app_name": APP_NAME, "user": None},
    )


@app.post("/login")
async def login_post(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    csrf_token: str = Form(...),
):
    try:
        verify_csrf(request, csrf_token)
    except Exception:
        return RedirectResponse(url="/login?err=csrf", status_code=302)

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, username, password_hash, role FROM users WHERE username = ?", (username,))
    row = cur.fetchone()
    conn.close()
    if not row or not bcrypt.verify(password, row[2]):
        return RedirectResponse(url="/login?err=auth", status_code=302)

    request.session["user"] = {"id": row[0], "username": row[1], "role": row[3]}
    request.session["csrf_token"] = secrets.token_urlsafe(32)
    return RedirectResponse(url="/", status_code=302)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=302)


# ---------- Home / Plan ----------
@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    redir = require_login_redirect(request)
    if redir:
        return redir

    user = current_user(request)
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "user": user,
            "csrf_token": get_csrf_token(request),
            "app_name": APP_NAME,
        },
    )


@app.post("/create-plan")
async def create_plan(
    request: Request,
    hotel_name: str = Form(...),
    month: str = Form(...),  # YYYY-MM
    file_name: str = Form("otel-plani"),
    frequency_days: int = Form(1),
    contact_info: str = Form(""),
    images: List[UploadFile] = File([]),
    csrf_token: str = Form(...),
):
    redir = require_login_redirect(request)
    if redir:
        return redir

    verify_csrf(request, csrf_token)

    doc = Document()
    doc.add_heading(f"{hotel_name} - Aylık Plan", level=0)

    month_text = month
    try:
        month_text = datetime.strptime(month, "%Y-%m").strftime("%B %Y")
    except Exception:
        pass

    doc.add_paragraph(f"Ay: {month_text}")
    doc.add_paragraph(f"Frekans: {frequency_days} günde 1")
    if contact_info.strip():
        doc.add_paragraph(f"İletişim: {contact_info}")

    doc.add_paragraph("")
    doc.add_paragraph("Görseller:")
    for img in images or []:
        try:
            img_bytes = await img.read()
            if img_bytes:
                stream = io.BytesIO(img_bytes)
                doc.add_picture(stream, width=Inches(1.6))
        except Exception:
            continue

    doc.add_paragraph("")
    doc.add_paragraph("Örnek Görev Planı:")
    table = doc.add_table(rows=1, cols=3)
    hdr = table.rows[0].cells
    hdr[0].text = "Tarih"
    hdr[1].text = "Görev"
    hdr[2].text = "Not"

    try:
        start_dt = datetime.strptime(month + "-01", "%Y-%m-%d")
        day = 1
        while True:
            try:
                cur_dt = datetime.strptime(f"{start_dt.year}-{start_dt.month:02d}-{day:02d}", "%Y-%m-%d")
            except ValueError:
                break
            row = table.add_row().cells
            row[0].text = cur_dt.strftime("%d.%m.%Y")
            row[1].text = "İçerik / Görsel Hazırlığı"
            row[2].text = "-"
            day += frequency_days
    except Exception:
        pass

    buf = io.BytesIO()
    doc.save(buf)
    buf.seek(0)

    safe_name = (file_name or "otel-plani").strip().replace(" ", "-")
    if not safe_name.lower().endswith(".docx"):
        safe_name += ".docx"

    headers = {"Content-Disposition": f'attachment; filename="{safe_name}"'}
    return StreamingResponse(
        buf,
        headers=headers,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )


# ---------- Admin ----------
@app.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request):
    redir = require_admin_redirect(request)
    if redir:
        return redir

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, username, role, created_at FROM users ORDER BY id ASC")
    rows = cur.fetchall()
    conn.close()
    users = [{"id": r[0], "username": r[1], "role": r[2], "created_at": r[3]} for r in rows]

    return templates.TemplateResponse(
        "admin.html",
        {
            "request": request,
            "user": current_user(request),
            "users": users,
            "csrf_token": get_csrf_token(request),
            "app_name": APP_NAME,
        },
    )


@app.post("/admin/users/create")
async def admin_create_user(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    role: str = Form("user"),
    csrf_token: str = Form(...),
):
    redir = require_admin_redirect(request)
    if redir:
        return redir
    try:
        verify_csrf(request, csrf_token)
    except Exception:
        return RedirectResponse(url="/admin?err=csrf", status_code=302)

    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO users (username, password_hash, role, created_at) VALUES (?, ?, ?, ?)",
            (username, bcrypt.using(rounds=12).hash(password), role, datetime.utcnow().isoformat()),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        return RedirectResponse(url="/admin?err=exists", status_code=302)
    conn.close()
    return RedirectResponse(url="/admin?ok=1", status_code=302)


@app.post("/admin/users/delete")
async def admin_delete_user(
    request: Request,
    user_id: int = Form(...),
    csrf_token: str = Form(...),
):
    redir = require_admin_redirect(request)
    if redir:
        return redir
    try:
        verify_csrf(request, csrf_token)
    except Exception:
        return RedirectResponse(url="/admin?err=csrf", status_code=302)

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT username FROM users WHERE id = ?", (user_id,))
    row = cur.fetchone()
    if row and row[0] == ADMIN_SEED_USER:
        conn.close()
        return RedirectResponse(url="/admin?err=cannot_delete_admin", status_code=302)

    cur.execute("DELETE FROM users WHERE id = ?", (user_id,))
    conn.commit()
    conn.close()
    return RedirectResponse(url="/admin?ok=1", status_code=302)
