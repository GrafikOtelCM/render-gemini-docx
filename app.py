import os
import sqlite3
import secrets
import time
from pathlib import Path
from typing import Optional

from fastapi import (
    FastAPI, Request, Form, Depends, UploadFile, File, HTTPException
)
from fastapi.responses import HTMLResponse, RedirectResponse, Response, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.datastructures import URL
import bcrypt

# ────────────────────────────────────────────────────────────────────────────────
# ENV / CONFIG
# ────────────────────────────────────────────────────────────────────────────────
APP_NAME = os.getenv("APP_NAME", "Otel Planlama Stüdyosu")
SECRET_KEY = os.getenv("SECRET_KEY", secrets.token_urlsafe(48))

# Render free plan: kalıcı disk yok -> /tmp
USAGE_DB_PATH = os.getenv("USAGE_DB_PATH", "/tmp/usage.db")

# Admin tohumlama (opsiyonel)
ADMIN_CODE_USER = os.getenv("ADMIN_CODE_USER", "admin")
ADMIN_CODE_PASS = os.getenv("ADMIN_CODE_PASS", "admin123")

BASE_DIR = Path(__file__).parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

# Klasörler mevcut mu?
TEMPLATES_DIR.mkdir(exist_ok=True)
STATIC_DIR.mkdir(exist_ok=True)

# ────────────────────────────────────────────────────────────────────────────────
# APP
# ────────────────────────────────────────────────────────────────────────────────
app = FastAPI(title=APP_NAME)

# Session cookie (mutlaka ilk eklensin)
app.add_middleware(
    SessionMiddleware,
    secret_key=SECRET_KEY,
    session_cookie="ops_session",
    same_site="lax",
    https_only=False,  # istersen True yapabilirsin
    max_age=60 * 60 * 24 * 7,  # 7 gün
)

# Basit güvenlik header’ları
class HardenHeaders(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

app.add_middleware(HardenHeaders)

# Statik & Jinja
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Jinja ortak değişkenleri
def common_ctx(request: Request):
    user = request.session.get("user")
    is_admin = bool(user and user.get("role") == "admin")
    csrf_token = ensure_csrf(request)
    return {"request": request, "app_name": APP_NAME, "user": user, "is_admin": is_admin, "csrf_token": csrf_token}

# ────────────────────────────────────────────────────────────────────────────────
# DB
# ────────────────────────────────────────────────────────────────────────────────
def get_db():
    # check_same_thread False: aynı process’te farklı thread’ler kullanabilir
    return sqlite3.connect(USAGE_DB_PATH, timeout=30, check_same_thread=False)

def ensure_db():
    conn = get_db()
    cur = conn.cursor()
    # kullanıcılar
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        pw_hash TEXT NOT NULL,
        role TEXT NOT NULL CHECK(role IN ('admin','user')),
        created_at INTEGER NOT NULL
    )""")
    # basit log (opsiyonel)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT,
        action TEXT,
        at INTEGER NOT NULL
    )""")
    conn.commit()
    conn.close()

def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

def check_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        return False

def create_user_if_missing(username: str, password: str, role: str = "user"):
    conn = get_db()
    cur = conn.cursor()
    # varsa dokunma
    cur.execute("SELECT id FROM users WHERE username = ?", (username,))
    row = cur.fetchone()
    if row:
        conn.close()
        return
    pw_hash = hash_password(password)
    cur.execute(
        "INSERT INTO users (username, pw_hash, role, created_at) VALUES (?,?,?,?)",
        (username, pw_hash, role, int(time.time()))
    )
    conn.commit()
    conn.close()

def seed_admin_from_env():
    # admin’i bir kere tohumla
    create_user_if_missing(ADMIN_CODE_USER, ADMIN_CODE_PASS, "admin")

def audit(username: Optional[str], action: str):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("INSERT INTO audit_log (username, action, at) VALUES (?,?,?)", (username, action, int(time.time())))
    conn.commit()
    conn.close()

# ────────────────────────────────────────────────────────────────────────────────
# CSRF
# ────────────────────────────────────────────────────────────────────────────────
def ensure_csrf(request: Request) -> str:
    token = request.session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf_token"] = token
    return token

def verify_csrf(request: Request, token_from_form: str):
    token_in_session = request.session.get("csrf_token")
    if not token_in_session or not token_from_form or token_from_form != token_in_session:
        raise HTTPException(status_code=400, detail="CSRF doğrulaması başarısız")

# ────────────────────────────────────────────────────────────────────────────────
# AUTH HELPERS
# ────────────────────────────────────────────────────────────────────────────────
def current_user(request: Request) -> Optional[dict]:
    return request.session.get("user")

def require_user(request: Request) -> dict:
    u = current_user(request)
    if not u:
        # login’e yönlendir
        next_url = str(request.url)
        r = RedirectResponse(url=f"/login?next={URL(next_url).path}", status_code=303)
        return r  # FastAPI dependency içinde redirect return edemiyoruz; bu yüzden route içinde kullanacağız
    return u

def is_admin_user(u: Optional[dict]) -> bool:
    return bool(u and u.get("role") == "admin")

# ────────────────────────────────────────────────────────────────────────────────
# STARTUP
# ────────────────────────────────────────────────────────────────────────────────
@app.on_event("startup")
def on_startup():
    # /tmp yolunu bilgilendir
    print(f"[INFO] Using DB at: {USAGE_DB_PATH}")
    ensure_db()
    seed_admin_from_env()

# ────────────────────────────────────────────────────────────────────────────────
# HEALTH
# ────────────────────────────────────────────────────────────────────────────────
@app.get("/health")
def health():
    return {"ok": True}

@app.get("/healthz")
def healthz():
    return {"ok": True}

@app.head("/")
def head_root():
    # Render port scan HEAD / atıyor -> 204 ver, auth kontrol etmeyelim
    return Response(status_code=204)

# ────────────────────────────────────────────────────────────────────────────────
# AUTH ROUTES
# ────────────────────────────────────────────────────────────────────────────────
@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = "/"):
    ctx = common_ctx(request) | {"next": next, "error": None}
    return templates.TemplateResponse("login.html", ctx)

@app.post("/login", response_class=HTMLResponse)
def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    csrf_token: str = Form(...),
    next: str = Form("/")
):
    verify_csrf(request, csrf_token)

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT username, pw_hash, role FROM users WHERE username = ?", (username,))
    row = cur.fetchone()
    conn.close()

    if not row or not check_password(password, row[1]):
        ctx = common_ctx(request) | {"next": next, "error": "Geçersiz kullanıcı adı veya şifre"}
        return templates.TemplateResponse("login.html", ctx, status_code=400)

    # Login success
    request.session["user"] = {"username": row[0], "role": row[2]}
    audit(row[0], "login")
    return RedirectResponse(next or "/", status_code=303)

@app.post("/logout")
def logout(request: Request, csrf_token: str = Form(...)):
    verify_csrf(request, csrf_token)
    u = request.session.get("user")
    if u:
        audit(u.get("username"), "logout")
    request.session.clear()
    return RedirectResponse("/login", status_code=303)

# ────────────────────────────────────────────────────────────────────────────────
# HOME / PLANLAMA (eski küçük önizlemeli sürükle-bırak)
# ────────────────────────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    u = current_user(request)
    if not u:
        return RedirectResponse("/login?next=/", status_code=303)
    ctx = common_ctx(request)
    return templates.TemplateResponse("index.html", ctx)

@app.post("/upload")
async def upload_images(
    request: Request,
    csrf_token: str = Form(...),
    files: list[UploadFile] = File(default=[])
):
    verify_csrf(request, csrf_token)
    u = current_user(request)
    if not u:
        return RedirectResponse("/login?next=/", status_code=303)

    saved = []
    upload_dir = Path("/tmp/uploads")
    upload_dir.mkdir(parents=True, exist_ok=True)

    for f in files:
        if not f.filename:
            continue
        # sadece temel güvenlik filtresi
        name = f.filename.replace("/", "_").replace("\\", "_")
        dst = upload_dir / f"{int(time.time()*1000)}_{name}"
        content = await f.read()
        dst.write_bytes(content)
        saved.append(dst.name)

    audit(u.get("username"), f"uploaded {len(saved)} file(s)")
    return JSONResponse({"ok": True, "saved": saved})

# ────────────────────────────────────────────────────────────────────────────────
# ADMIN: kullanıcı yönetimi (yalnız admin)
# ────────────────────────────────────────────────────────────────────────────────
@app.get("/admin", response_class=HTMLResponse)
def admin_home(request: Request):
    u = current_user(request)
    if not u:
        return RedirectResponse("/login?next=/admin", status_code=303)
    if not is_admin_user(u):
        raise HTTPException(status_code=403, detail="Yasak")
    # kullanıcıları listele
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, username, role, created_at FROM users ORDER BY id DESC")
    users = [{"id": r[0], "username": r[1], "role": r[2], "created_at": r[3]}] = [*map(lambda r: {"id": r[0], "username": r[1], "role": r[2], "created_at": r[3]}, cur.fetchall())]  # noqa: E731
    conn.close()
    ctx = common_ctx(request) | {"users": users}
    return templates.TemplateResponse("admin.html", ctx)

@app.post("/admin/users/create")
def admin_create_user(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    role: str = Form("user"),
    csrf_token: str = Form(...)
):
    u = current_user(request)
    if not u:
        return RedirectResponse("/login?next=/admin", status_code=303)
    if not is_admin_user(u):
        raise HTTPException(status_code=403, detail="Yasak")
    verify_csrf(request, csrf_token)

    if role not in ("admin", "user"):
        role = "user"

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id FROM users WHERE username = ?", (username,))
    if cur.fetchone():
        conn.close()
        return RedirectResponse("/admin?err=exists", status_code=303)

    pw_hash = hash_password(password)
    cur.execute(
        "INSERT INTO users (username, pw_hash, role, created_at) VALUES (?,?,?,?)",
        (username, pw_hash, role, int(time.time()))
    )
    conn.commit()
    conn.close()

    audit(u.get("username"), f"create_user:{username}:{role}")
    return RedirectResponse("/admin?ok=created", status_code=303)

@app.post("/admin/users/delete")
def admin_delete_user(
    request: Request,
    user_id: int = Form(...),
    csrf_token: str = Form(...)
):
    u = current_user(request)
    if not u:
        return RedirectResponse("/login?next=/admin", status_code=303)
    if not is_admin_user(u):
        raise HTTPException(status_code=403, detail="Yasak")
    verify_csrf(request, csrf_token)

    conn = get_db()
    cur = conn.cursor()
    # admin kendini silemesin
    cur.execute("SELECT username, role FROM users WHERE id = ?", (user_id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return RedirectResponse("/admin?err=notfound", status_code=303)
    target_username, target_role = row
    if target_username == u.get("username"):
        conn.close()
        return RedirectResponse("/admin?err=cant_delete_self", status_code=303)

    cur.execute("DELETE FROM users WHERE id = ?", (user_id,))
    conn.commit()
    conn.close()

    audit(u.get("username"), f"delete_user:{target_username}")
    return RedirectResponse("/admin?ok=deleted", status_code=303)
