import os
import sqlite3
import secrets
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, Request, Form, Depends, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.base import BaseHTTPMiddleware

# ---------------------------
# Config
# ---------------------------
APP_NAME = os.getenv("APP_NAME", "Otel Planlama Stüdyosu")
SECRET_KEY = os.getenv(
    "SECRET_KEY",
    "dev-" + secrets.token_urlsafe(48)
)
USAGE_DB_PATH = os.getenv("USAGE_DB_PATH", "/tmp/usage.db")

# Gemini ayarları (opsiyonel burada kullanılmıyor ama env'de dursun)
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

# ---------------------------
# App / static / templates
# ---------------------------
app = FastAPI(title=APP_NAME)

app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, same_site="lax")
templates = Jinja2Templates(directory="templates")

if not os.path.exists("static"):
    os.makedirs("static", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")

# ---------------------------
# DB helpers
# ---------------------------
def get_db():
    # /tmp Render free planda yazılabilir
    conn = sqlite3.connect(USAGE_DB_PATH, timeout=30, check_same_thread=False)
    return conn

def ensure_db():
    os.makedirs(os.path.dirname(USAGE_DB_PATH), exist_ok=True) if os.path.dirname(USAGE_DB_PATH) else None
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            pw_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'user',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

def hash_pw(pw: str) -> str:
    # hızlı ve basit—bcrypt kurulu, onu kullanalım
    import bcrypt
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()

def check_pw(pw: str, pw_hash: str) -> bool:
    import bcrypt
    try:
        return bcrypt.checkpw(pw.encode(), pw_hash.encode())
    except Exception:
        return False

def get_user_by_username(username: str) -> Optional[dict]:
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, username, pw_hash, role, created_at FROM users WHERE username = ?", (username,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    return {"id": row[0], "username": row[1], "pw_hash": row[2], "role": row[3], "created_at": row[4]}

def create_user(username: str, password: str, role: str = "user") -> None:
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO users (username, pw_hash, role) VALUES (?, ?, ?)",
        (username, hash_pw(password), role)
    )
    conn.commit()
    conn.close()

def delete_user(user_id: int) -> None:
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM users WHERE id = ?", (user_id,))
    conn.commit()
    conn.close()

# ---------------------------
# CSRF helper
# ---------------------------
def get_or_set_csrf(request: Request) -> str:
    token = request.session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf_token"] = token
    return token

def require_csrf(request: Request, token: str):
    if not token or token != request.session.get("csrf_token"):
        raise ValueError("CSRF token mismatch")

# ---------------------------
# Auth dependencies
# ---------------------------
def current_user(request: Request) -> Optional[dict]:
    return request.session.get("user")

def require_login(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse(url="/login?next=/", status_code=303)
    return user

def require_admin(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse(url="/login?next=/admin", status_code=303)
    if user.get("role") != "admin":
        return RedirectResponse(url="/", status_code=303)
    return user

# ---------------------------
# Middleware: template context
# ---------------------------
class InjectGlobalsMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # sadece session erişimi için SessionMiddleware zaten var
        response = await call_next(request)
        return response

app.add_middleware(InjectGlobalsMiddleware)

# ---------------------------
# Startup: DB ve ilk admin
# ---------------------------
@app.on_event("startup")
def on_startup():
    ensure_db()
    # İlk admin—env'den veya hazır değer
    admin_user = os.getenv("ADMIN_CODE_USER", "admin")
    admin_pass = os.getenv("ADMIN_CODE_PASS", "admin123")
    existing = get_user_by_username(admin_user)
    if not existing:
        try:
            create_user(admin_user, admin_pass, role="admin")
        except sqlite3.IntegrityError:
            pass
    print(f"[INFO] Using DB at: {USAGE_DB_PATH}")

# ---------------------------
# Health
# ---------------------------
@app.get("/health", include_in_schema=False)
@app.get("/healthz", include_in_schema=False)
def health():
    return {"ok": True, "ts": datetime.utcnow().isoformat()}

# ---------------------------
# Auth routes
# ---------------------------
@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/"):
    csrf = get_or_set_csrf(request)
    return templates.TemplateResponse("login.html", {
        "request": request,
        "title": "Giriş Yap",
        "csrf_token": csrf,
        "next": next
    })

@app.post("/login", response_class=HTMLResponse)
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    csrf_token: str = Form(...),
    next: str = Form("/")
):
    try:
        require_csrf(request, csrf_token)
    except Exception:
        return templates.TemplateResponse("login.html", {
            "request": request,
            "title": "Giriş Yap",
            "error": "Oturum süresi doldu ya da CSRF hatası. Tekrar deneyin.",
            "csrf_token": get_or_set_csrf(request),
            "next": next
        }, status_code=400)

    u = get_user_by_username(username.strip())
    if not u or not check_pw(password, u["pw_hash"]):
        return templates.TemplateResponse("login.html", {
            "request": request,
            "title": "Giriş Yap",
            "error": "Kullanıcı adı veya şifre hatalı.",
            "csrf_token": get_or_set_csrf(request),
            "next": next
        }, status_code=400)

    request.session["user"] = {"id": u["id"], "username": u["username"], "role": u["role"]}
    return RedirectResponse(url=next or "/", status_code=303)

@app.post("/logout")
def logout(request: Request, csrf_token: str = Form(...)):
    try:
        require_csrf(request, csrf_token)
    except Exception:
        pass
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)

# ---------------------------
# Root: Planlama sayfası (giriş gerekli)
# ---------------------------
@app.get("/", response_class=HTMLResponse)
def index(request: Request, user=Depends(require_login)):
    csrf = get_or_set_csrf(request)
    months = [
        ("01", "Ocak"), ("02", "Şubat"), ("03", "Mart"), ("04", "Nisan"),
        ("05", "Mayıs"), ("06", "Haziran"), ("07", "Temmuz"), ("08", "Ağustos"),
        ("09", "Eylül"), ("10", "Ekim"), ("11", "Kasım"), ("12", "Aralık"),
    ]
    return templates.TemplateResponse("index.html", {
        "request": request,
        "title": APP_NAME,
        "csrf_token": csrf,
        "months": months,
        "user": user,
    })

@app.post("/planla", response_class=JSONResponse)
async def planla(
    request: Request,
    csrf_token: str = Form(...),
    file_name: str = Form("otel_plani.docx"),
    month: str = Form(...),
    every_days: int = Form(1),
    hotel_contact: str = Form(""),
    images: list[UploadFile] = File(default_factory=list)
):
    # CSRF
    try:
        require_csrf(request, csrf_token)
    except Exception:
        return JSONResponse({"ok": False, "error": "CSRF"}, status_code=400)

    # basit validasyonlar
    if every_days < 1:
        return JSONResponse({"ok": False, "error": "'Kaç günde bir' en az 1 olmalı."}, status_code=400)
    if not month or len(month) != 2:
        return JSONResponse({"ok": False, "error": "Ay seçimi zorunlu."}, status_code=400)

    # Burada gerçek docx üretim mantığınız vardıysa ona entegrasyon yapılır.
    # Şimdilik sadece parametreleri geri döndürelim ve front'ta indirilebilir dosya
    # endpointine yönlendirelim (placeholder).
    return {
        "ok": True,
        "received": {
            "file_name": file_name.strip() or "otel_plani.docx",
            "month": month,
            "every_days": every_days,
            "hotel_contact": hotel_contact.strip(),
            "images_count": len(images or []),
        }
    }

# İsteğe bağlı: bir örnek indirme endpoint’i (dummy)
@app.get("/download-example")
def download_example():
    from io import BytesIO
    content = BytesIO(b"Bu bir ornek dosyadir.")
    return StreamingResponse(content, media_type="application/octet-stream", headers={
        "Content-Disposition": f'attachment; filename="ornek.txt"'
    })

# ---------------------------
# Admin
# ---------------------------
@app.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request, user=Depends(require_admin)):
    csrf = get_or_set_csrf(request)
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, username, role, created_at FROM users ORDER BY id DESC")
    rows = cur.fetchall()
    conn.close()
    users = [{"id": r[0], "username": r[1], "role": r[2], "created_at": r[3]} for r in rows]
    return templates.TemplateResponse("admin.html", {
        "request": request,
        "title": "Admin",
        "csrf_token": csrf,
        "users": users,
        "user": user
    })

@app.post("/admin/users/create")
def admin_create_user(
    request: Request,
    user=Depends(require_admin),
    csrf_token: str = Form(...),
    username: str = Form(...),
    password: str = Form(...),
    role: str = Form("user")
):
    try:
        require_csrf(request, csrf_token)
    except Exception:
        return RedirectResponse(url="/admin?e=csrf", status_code=303)

    username = username.strip()
    role = role if role in ("user", "admin") else "user"
    if not username or not password:
        return RedirectResponse(url="/admin?e=validation", status_code=303)

    try:
        create_user(username, password, role)
    except sqlite3.IntegrityError:
        return RedirectResponse(url="/admin?e=exists", status_code=303)

    return RedirectResponse(url="/admin?ok=1", status_code=303)

@app.post("/admin/users/delete")
def admin_delete_user(
    request: Request,
    user=Depends(require_admin),
    csrf_token: str = Form(...),
    user_id: int = Form(...)
):
    try:
        require_csrf(request, csrf_token)
    except Exception:
        return RedirectResponse(url="/admin?e=csrf", status_code=303)

    delete_user(user_id)
    return RedirectResponse(url="/admin?deleted=1", status_code=303)
