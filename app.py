import os
import io
import sqlite3
import secrets
from datetime import datetime
from typing import List, Optional

from fastapi import FastAPI, Request, Form, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.templating import Jinja2Templates
from passlib.hash import bcrypt

# -----------------------------------------------------------------------------
# ENV & CONSTANTS
# -----------------------------------------------------------------------------
APP_NAME = os.getenv("APP_NAME", "Otel Planlama Stüdyosu")
SECRET_KEY = os.getenv("SECRET_KEY", "change-me-please")
USAGE_DB_PATH = os.getenv("USAGE_DB_PATH", "/tmp/usage.db")

ADMIN_CODE_USER = os.getenv("ADMIN_CODE_USER", "admin")
ADMIN_CODE_PASS = os.getenv("ADMIN_CODE_PASS", "admin123")

MONTHS = [
    ("01", "Ocak"), ("02", "Şubat"), ("03", "Mart"), ("04", "Nisan"),
    ("05", "Mayıs"), ("06", "Haziran"), ("07", "Temmuz"), ("08", "Ağustos"),
    ("09", "Eylül"), ("10", "Ekim"), ("11", "Kasım"), ("12", "Aralık"),
]

# -----------------------------------------------------------------------------
# APP
# -----------------------------------------------------------------------------
app = FastAPI(title=APP_NAME)
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, same_site="lax")

templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")


# -----------------------------------------------------------------------------
# DB
# -----------------------------------------------------------------------------
def get_db():
    conn = sqlite3.connect(USAGE_DB_PATH, timeout=30, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def ensure_db():
    conn = get_db()
    cur = conn.cursor()
    # users
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
    # usage (opsiyonel)
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS usage_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT,
            action TEXT,
            meta TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.commit()

    # seed admin
    cur.execute("SELECT id FROM users WHERE username = ?", (ADMIN_CODE_USER,))
    if cur.fetchone() is None:
        cur.execute(
            "INSERT INTO users (username, password_hash, role, created_at) VALUES (?, ?, ?, ?)",
            (ADMIN_CODE_USER, bcrypt.hash(ADMIN_CODE_PASS), "admin", datetime.utcnow().isoformat()),
        )
        conn.commit()
    conn.close()


@app.on_event("startup")
def _startup():
    ensure_db()
    print(f"[INFO] Using DB at: {USAGE_DB_PATH}")


# -----------------------------------------------------------------------------
# HELPERS: auth, csrf, context
# -----------------------------------------------------------------------------
def current_user(request: Request) -> Optional[dict]:
    return request.session.get("user")


def require_login(request: Request) -> dict:
    user = current_user(request)
    if not user:
        nxt = request.url.path
        raise HTTPException(status_code=303, detail="redirect", headers={"Location": f"/login?next={nxt}"})
    return user


def ensure_csrf(request: Request) -> str:
    token = request.session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf_token"] = token
    return token


def check_csrf(request: Request, token_from_form: str):
    token = request.session.get("csrf_token")
    if not token or not token_from_form or secrets.compare_digest(token, token_from_form) is False:
        raise HTTPException(status_code=400, detail="CSRF doğrulaması başarısız")


def log_usage(username: str, action: str, meta: str = ""):
    try:
        conn = get_db()
        conn.execute(
            "INSERT INTO usage_log (username, action, meta, created_at) VALUES (?, ?, ?, ?)",
            (username, action, meta[:1000], datetime.utcnow().isoformat()),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass  # kullanım logu kritik değil


# İsteğe bağlı: tüm template render'larına ortak context
class ContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # CSRF token her istekte hazır olsun
        ensure_csrf(request)
        response = await call_next(request)
        return response


app.add_middleware(ContextMiddleware)


# -----------------------------------------------------------------------------
# ROUTES: health
# -----------------------------------------------------------------------------
@app.get("/health", response_class=PlainTextResponse)
def health():
    return "ok"


# -----------------------------------------------------------------------------
# ROUTES: auth
# -----------------------------------------------------------------------------
@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = "/"):
    if current_user(request):
        return RedirectResponse(next or "/", status_code=303)
    return templates.TemplateResponse(
        "login.html",
        {
            "request": request,
            "title": APP_NAME,
            "csrf_token": ensure_csrf(request),
            "next": next,
            "user": None,
        },
    )


@app.post("/login")
def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    csrf_token: str = Form(...),
    next: str = Form("/"),
):
    check_csrf(request, csrf_token)
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, username, password_hash, role, created_at FROM users WHERE username = ?", (username,))
    row = cur.fetchone()
    conn.close()

    if not row or not bcrypt.verify(password, row[2]):
        # yeniden form
        return templates.TemplateResponse(
            "login.html",
            {
                "request": request,
                "title": APP_NAME,
                "csrf_token": ensure_csrf(request),
                "user": None,
                "next": next,
                "error": "Kullanıcı adı veya şifre hatalı.",
            },
            status_code=401,
        )

    request.session["user"] = {"id": row[0], "username": row[1], "role": row[3]}
    # CSRF rotate
    request.session["csrf_token"] = secrets.token_urlsafe(32)
    log_usage(row[1], "login")

    return RedirectResponse(next or "/", status_code=303)


@app.post("/logout")
@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# -----------------------------------------------------------------------------
# ROUTES: index (planlama formu)
# -----------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "title": APP_NAME,
            "user": user,
            "csrf_token": ensure_csrf(request),
            "months": MONTHS,
        },
    )


# -----------------------------------------------------------------------------
# ROUTE: planla (DOCX üretir ve indirtir)
# -----------------------------------------------------------------------------
@app.post("/planla")
async def planla(
    request: Request,
    file_name: str = Form("otel_plani.docx"),
    month: str = Form(...),
    every_days: int = Form(...),
    hotel_contact: str = Form(""),
    images: List[UploadFile] = File(default=[]),
    csrf_token: str = Form(...),
):
    user = require_login(request)
    check_csrf(request, csrf_token)

    # Ay adı
    month_name = dict(MONTHS).get(month, month)

    # python-docx ile belge oluştur
    from docx import Document
    from docx.shared import Inches, Pt
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    doc = Document()

    # Başlık
    title = doc.add_heading(f"{APP_NAME} – {month_name}", level=1)
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER

    # Meta
    p = doc.add_paragraph()
    p.add_run("Oluşturan: ").bold = True
    p.add_run(user["username"])
    p.add_run("   •   Tarih: ").bold = True
    p.add_run(datetime.now().strftime("%d.%m.%Y %H:%M"))
    p.add_run("   •   Periyot: ").bold = True
    p.add_run(f"{every_days} günde bir")

    # Otel iletişim
    if hotel_contact.strip():
        doc.add_paragraph().add_run("Otel İletişim").bold = True
        ct = doc.add_paragraph(hotel_contact.strip())
        ct_format = ct.runs[0].font
        ct_format.size = Pt(11)

    # Basit bir program çizelgesi örneği
    doc.add_paragraph().add_run("Program").bold = True
    tbl = doc.add_table(rows=1, cols=3)
    hdr_cells = tbl.rows[0].cells
    hdr_cells[0].text = "Gün"
    hdr_cells[1].text = "Tarih"
    hdr_cells[2].text = "Not"
    # Örnek 5 satır
    from datetime import date, timedelta
    today = date.today().replace(day=1)
    for i in range(5):
        r = tbl.add_row().cells
        r[0].text = f"{i+1}"
        r[1].text = (today + timedelta(days=i*every_days)).strftime("%d.%m.%Y")
        r[2].text = ""

    # Görseller
    if images:
        doc.add_paragraph().add_run("Görseller").bold = True
        for img in images:
            try:
                content = await img.read()
                if not content:
                    continue
                # küçük önizleme boyutunda ekle
                # (2.0 inch genişlik iyi bir thumbnail görünümü)
                doc.add_picture(io.BytesIO(content), width=Inches(2.0))
            except Exception:
                # tek bir görsel hatası tüm işlemi bozmasın
                pass

    # Dosya adı uzantısı
    if not file_name.lower().endswith(".docx"):
        file_name = f"{file_name}.docx"

    # Bellekten döndür
    buf = io.BytesIO()
    doc.save(buf)
    buf.seek(0)

    # kullanım logu
    log_usage(user["username"], "planla", f"file={file_name}; month={month_name}; every_days={every_days}")

    headers = {
        "Content-Disposition": f'attachment; filename="{file_name}"'
    }
    return StreamingResponse(buf, media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document", headers=headers)


# -----------------------------------------------------------------------------
# ROUTES: admin
# -----------------------------------------------------------------------------
def require_admin(request: Request) -> dict:
    user = require_login(request)
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Yetkisiz")
    return user


@app.get("/admin", response_class=HTMLResponse)
def admin_index(request: Request):
    user = require_admin(request)
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
            "title": f"{APP_NAME} • Admin",
            "user": user,
            "csrf_token": ensure_csrf(request),
            "users": users,
        },
    )


@app.post("/admin/users/create")
def admin_user_create(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    role: str = Form("user"),
    csrf_token: str = Form(...),
):
    user = require_admin(request)
    check_csrf(request, csrf_token)

    if role not in ("user", "admin"):
        role = "user"

    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO users (username, password_hash, role, created_at) VALUES (?, ?, ?, ?)",
            (username, bcrypt.hash(password), role, datetime.utcnow().isoformat()),
        )
        conn.commit()
        log_usage(user["username"], "admin_create_user", f"{username}:{role}")
    except sqlite3.IntegrityError:
        conn.close()
        raise HTTPException(status_code=400, detail="Kullanıcı adı zaten var")
    conn.close()
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/users/delete")
def admin_user_delete(
    request: Request,
    user_id: int = Form(...),
    csrf_token: str = Form(...),
):
    user = require_admin(request)
    check_csrf(request, csrf_token)

    conn = get_db()
    # Kendini silmesin
    cur = conn.cursor()
    cur.execute("SELECT username FROM users WHERE id = ?", (user_id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Kullanıcı bulunamadı")
    target_username = row[0]
    if target_username == user["username"]:
        conn.close()
        raise HTTPException(status_code=400, detail="Kendi hesabınızı silemezsiniz")

    conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    conn.commit()
    conn.close()
    log_usage(user["username"], "admin_delete_user", target_username)
    return RedirectResponse("/admin", status_code=303)


# -----------------------------------------------------------------------------
# 404/exception basit cevaplar
# -----------------------------------------------------------------------------
@app.exception_handler(303)
def _see_other_handler(request: Request, exc: HTTPException):
    # FastAPI 303 HTTPException redirect
    return RedirectResponse(exc.headers.get("Location", "/login"), status_code=303)
