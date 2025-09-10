import os, io, sqlite3, secrets, base64, json, datetime
from typing import List, Optional
from fastapi import FastAPI, Request, Form, UploadFile, File, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.datastructures import URL
from jinja2 import Environment, FileSystemLoader, select_autoescape
from itsdangerous import URLSafeSerializer
import bcrypt
from docx import Document
from docx.shared import Inches
from PIL import Image

APP_NAME = os.getenv("APP_NAME", "Otel Planlama Stüdyosu")
SECRET_KEY = os.getenv("SECRET_KEY", secrets.token_urlsafe(48))
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

USAGE_DB_PATH = os.getenv("USAGE_DB_PATH", "/tmp/usage.db")

app = FastAPI()
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, same_site="lax")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# ---------- CSRF ----------
def get_csrf_token(request: Request) -> str:
    token = request.session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf_token"] = token
    return token

def require_csrf(request: Request, token_from_body: str):
    real = request.session.get("csrf_token")
    if not real or not token_from_body or token_from_body != real:
        raise HTTPException(status_code=400, detail="Invalid CSRF token")

templates.env.globals["get_csrf_token"] = get_csrf_token
templates.env.globals["APP_NAME"] = APP_NAME

# ---------- DB ----------
def get_db():
    conn = sqlite3.connect(USAGE_DB_PATH, timeout=30, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn

def ensure_db():
    os.makedirs(os.path.dirname(USAGE_DB_PATH), exist_ok=True)
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE,
            pw_hash BLOB,
            role TEXT CHECK(role IN ('admin','staff')) NOT NULL DEFAULT 'staff'
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT,
            hotel TEXT,
            month TEXT,
            created_at TEXT
        )
    """)
    conn.commit()
    conn.close()

def create_user_if_missing(username: str, password: str, role: str = "admin"):
    if not username or not password:
        return
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id FROM users WHERE username=?", (username,))
    row = cur.fetchone()
    if not row:
        pw_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt())
        cur.execute("INSERT INTO users (username, pw_hash, role) VALUES (?,?,?)", (username, pw_hash, role))
        conn.commit()
    conn.close()

@app.on_event("startup")
def on_startup():
    ensure_db()
    # Admin seed (varsa tekrar eklemeye çalışmaz, SELECT ile kontrol ediyoruz)
    admin_user = os.getenv("ADMIN_CODE_USER", "admin")
    admin_pass = os.getenv("ADMIN_CODE_PASS", "admin123")
    create_user_if_missing(admin_user, admin_pass, "admin")

# ---------- Auth helpers ----------
def get_user_from_session(request: Request) -> Optional[dict]:
    u = request.session.get("user")
    return u

def require_login(request: Request) -> dict:
    u = get_user_from_session(request)
    if not u:
        # redirect to login with next
        next_url = str(request.url)
        resp = RedirectResponse(url=f"/login?next={next_url}", status_code=303)
        raise HTTPException(status_code=303, detail="redirect", headers={"Location": resp.headers["location"]})
    return u

def require_admin(request: Request) -> dict:
    u = require_login(request)
    if u.get("role") != "admin":
        raise HTTPException(403, "Admin required")
    return u

# ---------- Routes ----------
@app.get("/login", response_class=HTMLResponse)
def login_get(request: Request, next: Optional[str] = "/"):
    return templates.TemplateResponse("login.html", {"request": request, "next": next})

@app.post("/login")
def login_post(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    csrf_token: str = Form(...)
):
    require_csrf(request, csrf_token)
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, pw_hash, role FROM users WHERE username=?", (username,))
    row = cur.fetchone()
    conn.close()
    if not row:
        # kullanıcı yoksa basit mesajla geri dön
        return templates.TemplateResponse("login.html", {
            "request": request,
            "error": "Kullanıcı bulunamadı",
            "next": "/"
        })
    _, pw_hash, role = row
    if not bcrypt.checkpw(password.encode("utf-8"), pw_hash):
        return templates.TemplateResponse("login.html", {
            "request": request,
            "error": "Şifre hatalı",
            "next": "/"
        })
    request.session["user"] = {"username": username, "role": role}
    return RedirectResponse(url="/", status_code=303)

@app.post("/logout")
def logout_post(request: Request, csrf_token: str = Form(...)):
    require_csrf(request, csrf_token)
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)

@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    u = get_user_from_session(request)
    if not u:
        return RedirectResponse(url="/login?next=/", status_code=303)
    return templates.TemplateResponse("index.html", {"request": request, "user": u})

# --- Admin Dashboard (Yeni) ---
@app.get("/admin/dashboard", response_class=HTMLResponse)
def admin_dashboard(request: Request):
    u = get_user_from_session(request)
    if not u or u.get("role") != "admin":
        return RedirectResponse(url="/login?next=/admin/dashboard", status_code=303)
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, username, role FROM users ORDER BY role DESC, username ASC")
    users = [{"id": r[0], "username": r[1], "role": r[2]} for r in cur.fetchall()]
    conn.close()
    return templates.TemplateResponse("admin_dashboard.html", {"request": request, "user": u, "users": users})

@app.post("/admin/users/create")
def admin_users_create(
    request: Request,
    new_username: str = Form(...),
    new_password: str = Form(...),
    new_role: str = Form(...),
    csrf_token: str = Form(...)
):
    u = require_admin(request)
    require_csrf(request, csrf_token)
    if new_role not in ("admin","staff"):
        new_role = "staff"
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id FROM users WHERE username=?", (new_username,))
    row = cur.fetchone()
    if row:
        conn.close()
        # kullanıcı zaten var
        return RedirectResponse(url="/admin/dashboard?msg=exists", status_code=303)
    pw_hash = bcrypt.hashpw(new_password.encode("utf-8"), bcrypt.gensalt())
    cur.execute("INSERT INTO users (username, pw_hash, role) VALUES (?,?,?)", (new_username, pw_hash, new_role))
    conn.commit()
    conn.close()
    return RedirectResponse(url="/admin/dashboard?msg=created", status_code=303)

@app.post("/admin/users/delete")
def admin_users_delete(
    request: Request,
    user_id: int = Form(...),
    csrf_token: str = Form(...)
):
    u = require_admin(request)
    require_csrf(request, csrf_token)
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM users WHERE id=?", (user_id,))
    conn.commit()
    conn.close()
    return RedirectResponse(url="/admin/dashboard?msg=deleted", status_code=303)

# --- DOCX oluşturma (özet; var olan mantık korunarak) ---
def month_to_range(year: int, month: int):
    start = datetime.date(year, month, 1)
    if month == 12:
        end = datetime.date(year + 1, 1, 1) - datetime.timedelta(days=1)
    else:
        end = datetime.date(year, month + 1, 1) - datetime.timedelta(days=1)
    return start, end

@app.post("/plan")
async def create_plan(
    request: Request,
    hotel_name: str = Form(...),
    plan_month: str = Form(...),  # "2025-10" gibi
    interval_days: int = Form(...),
    tone: str = Form(...),
    csrf_token: str = Form(...),
    images: List[UploadFile] = File([])
):
    u = require_login(request)
    require_csrf(request, csrf_token)

    # Tarih planı
    y, m = map(int, plan_month.split("-"))
    start, end = month_to_range(y, m)
    dates = []
    d = start
    while d <= end:
        dates.append(d)
        d += datetime.timedelta(days=interval_days)
    if not dates:
        dates = [start]

    # DOCX derle
    doc = Document()
    doc.add_heading(f"{hotel_name} – {start.strftime('%B %Y')} Sosyal Medya Planı", level=1)

    # Görseller + açıklama/hashtag (basit örnek)
    # Not: Burada Gemini entegrasyonun varsa, her görsel için çağırıp captions üretebilirsin.
    for idx, up in enumerate(images):
        img_bytes = await up.read()
        # küçük görsel başlığı + tarih
        dt = dates[min(idx, len(dates)-1)]
        doc.add_paragraph(f"Paylaşım Tarihi: {dt.strftime('%d.%m.%Y')} – ({tone})")
        # görseli ekle
        try:
            image_stream = io.BytesIO(img_bytes)
            with Image.open(image_stream) as im:
                im.convert("RGB")
            # docx'e eklemek için geçici dosya
            tmp = io.BytesIO(img_bytes)
            tmp.name = up.filename  # python-docx için isim gerekli
            doc.add_picture(tmp, width=Inches(5.5))
        except Exception:
            doc.add_paragraph("[Görsel eklenemedi]")
        # örnek açıklama/hashtag
        doc.add_paragraph(f"Açıklama: {hotel_name} için {tone} tonda hazırlanan paylaşım önerisi.")
        doc.add_paragraph("Hashtag: #tatilbudur #otel #tatil")

        doc.add_paragraph("")  # boşluk

    # log
    conn = get_db()
    cur = conn.cursor()
    cur.execute("INSERT INTO logs (username, hotel, month, created_at) VALUES (?,?,?,?)",
                (u["username"], hotel_name, plan_month, datetime.datetime.utcnow().isoformat()))
    conn.commit()
    conn.close()

    # çıktı
    outf = io.BytesIO()
    doc.save(outf)
    outf.seek(0)
    filename = f"{hotel_name.replace(' ','_')}_{plan_month}.docx"
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    return StreamingResponse(outf, media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document", headers=headers)

# Sağlık kontrolü (Render port taraması için)
@app.get("/health")
def health():
    return {"ok": True}
