import io
import os
import sqlite3
import uuid
from datetime import datetime
from typing import Optional, Dict, Any, List

from fastapi import (
    FastAPI,
    Request,
    Depends,
    Form,
    UploadFile,
    File,
    HTTPException,
    status,
)
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, FileResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from starlette.staticfiles import StaticFiles
from starlette.datastructures import URL
from passlib.hash import bcrypt
from dotenv import load_dotenv

# ----------------------------
# ENV & APP
# ----------------------------
load_dotenv()

APP_NAME = os.getenv("APP_NAME", "Otel Planlama Stüdyosu")
SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-me")
DB_PATH = os.getenv("USAGE_DB_PATH", "/tmp/usage.db")

ADMIN_CODE_USER = os.getenv("ADMIN_CODE_USER", "admin")
ADMIN_CODE_PASS = os.getenv("ADMIN_CODE_PASS", "admin123")

FILES_DIR = "/tmp"  # Render üzerinde kalıcı değil ama indirme için yeterli

app = FastAPI(title=APP_NAME)

# Session cookie
app.add_middleware(
    SessionMiddleware,
    secret_key=SECRET_KEY,
    same_site="lax",
    https_only=False,  # Render'da HTTPS var; istersen True yapabilirsin
)

# Static mount (CSS/JS)
app.mount("/static", StaticFiles(directory="static"), name="static")

templates = Jinja2Templates(directory="templates")
templates.env.globals.update(app_name=APP_NAME)


# ----------------------------
# DB HELPERS
# ----------------------------
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with get_db() as conn:
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
        # admin mevcut mu?
        cur.execute("SELECT id FROM users WHERE username = ?", (ADMIN_CODE_USER,))
        row = cur.fetchone()
        if not row:
            cur.execute(
                "INSERT INTO users(username, password_hash, role, created_at) VALUES(?,?,?,?)",
                (
                    ADMIN_CODE_USER,
                    bcrypt.hash(ADMIN_CODE_PASS),
                    "admin",
                    datetime.utcnow().isoformat(),
                ),
            )
        conn.commit()


@app.on_event("startup")
def _startup():
    init_db()
    print(f"[INFO] Using DB at: {DB_PATH}")


# ----------------------------
# AUTH HELPERS
# ----------------------------
def current_user(request: Request) -> Optional[Dict[str, Any]]:
    # SessionMiddleware kuruluysa request.session kullanılabilir
    user = request.session.get("user") if hasattr(request, "session") else None
    return user


def require_login(request: Request) -> Dict[str, Any]:
    user = current_user(request)
    if not user:
        # login sayfasına yönlendir
        raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, detail="login required")
    return user


def require_admin(request: Request) -> Dict[str, Any]:
    user = require_login(request)
    if user.get("role") != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")
    return user


# ----------------------------
# ROUTES: AUTH
# ----------------------------
@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
    # plan oluşturma ekranı
    return templates.TemplateResponse("index.html", {"request": request, "user": user, "title": "Plan Oluştur"})


@app.get("/login", response_class=HTMLResponse)
async def login_form(request: Request, next: Optional[str] = "/"):
    user = current_user(request)
    if user:
        return RedirectResponse(url=next or "/", status_code=status.HTTP_302_FOUND)
    return templates.TemplateResponse("login.html", {"request": request, "title": "Giriş Yap"})


@app.post("/login")
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: Optional[str] = Form(default="/"),
):
    # kullanıcıyı DB'den bul
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id, username, password_hash, role, created_at FROM users WHERE username = ?", (username,))
        row = cur.fetchone()

    if not row or not bcrypt.verify(password, row["password_hash"]):
        # tekrar formu göster
        ctx = {"request": request, "title": "Giriş Yap", "error": "Kullanıcı adı veya şifre hatalı."}
        return templates.TemplateResponse("login.html", ctx, status_code=401)

    # session'a yaz
    request.session["user"] = {"id": row["id"], "username": row["username"], "role": row["role"]}
    # yönlendir
    redirect_to = next or "/"
    return RedirectResponse(url=redirect_to, status_code=status.HTTP_303_SEE_OTHER)


@app.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)


# ----------------------------
# ROUTES: ADMIN PAGES + API
# ----------------------------
@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    user = require_admin(request)
    return templates.TemplateResponse("admin.html", {"request": request, "user": user, "title": "Admin Paneli"})


@app.get("/admin/users")
async def admin_list_users(request: Request):
    require_admin(request)
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id, username, role, created_at FROM users ORDER BY id DESC")
        rows = cur.fetchall()
        users = [
            {"id": r["id"], "username": r["username"], "role": r["role"], "created_at": r["created_at"]} for r in rows
        ]
        return JSONResponse(users)


@app.post("/admin/users")
async def admin_create_user(request: Request):
    require_admin(request)
    data = await request.json()
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    role = data.get("role") or "user"
    if not username or not password:
        raise HTTPException(400, "username & password required")

    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO users(username, password_hash, role, created_at) VALUES(?,?,?,?)",
                (username, bcrypt.hash(password), role, datetime.utcnow().isoformat()),
            )
            conn.commit()
            user_id = cur.lastrowid
    except sqlite3.IntegrityError:
        raise HTTPException(409, "username already exists")

    return JSONResponse({"id": user_id, "username": username, "role": role})


@app.delete("/admin/users/{user_id}")
async def admin_delete_user(request: Request, user_id: int):
    require_admin(request)
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM users WHERE id = ?", (user_id,))
        conn.commit()
    return JSONResponse({"ok": True})


# ----------------------------
# PLAN: FILE GENERATION
# ----------------------------
def _safe_filename(s: str) -> str:
    s = "".join(ch for ch in s if ch.isalnum() or ch in (" ", "-", "_")).strip()
    return s or "plan"


def _make_docx(file_name: str, month: str, interval_days: int, start_date: Optional[str], hotel_contact: str, images: List[UploadFile]) -> str:
    from docx import Document
    from docx.shared import Inches

    doc = Document()
    doc.add_heading(f"{file_name}", level=0)
    doc.add_paragraph(f"Ay: {month}")
    doc.add_paragraph(f"Periyot (gün): {interval_days}")
    if start_date:
        doc.add_paragraph(f"Başlangıç: {start_date}")
    if hotel_contact:
        doc.add_paragraph("Otel İletişim:")
        doc.add_paragraph(hotel_contact)

    if images:
        doc.add_heading("Görseller", level=1)
        for img in images:
            try:
                content = img.file.read()
                if not content:
                    continue
                bio = io.BytesIO(content)
                doc.add_picture(bio, width=Inches(2.0))
            except Exception:
                pass

    fname = f"plan-{uuid.uuid4().hex}.docx"
    path = os.path.join(FILES_DIR, fname)
    doc.save(path)
    return fname


def _make_xlsx(file_name: str, month: str, interval_days: int, start_date: Optional[str], hotel_contact: str) -> str:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Plan"

    ws["A1"] = "Dosya Adı"
    ws["B1"] = file_name
    ws["A2"] = "Ay"
    ws["B2"] = month
    ws["A3"] = "Periyot (gün)"
    ws["B3"] = interval_days
    ws["A4"] = "Başlangıç"
    ws["B4"] = start_date or ""
    ws["A5"] = "Otel İletişim"
    ws["B5"] = hotel_contact or ""

    fname = f"plan-{uuid.uuid4().hex}.xlsx"
    path = os.path.join(FILES_DIR, fname)
    wb.save(path)
    return fname


def _plan_response_urls(request: Request, docx_name: Optional[str], xlsx_name: Optional[str]) -> Dict[str, Any]:
    def file_url(name: str) -> str:
        base: URL = request.url
        return str(base.replace(path=f"/files/{name}", query=""))

    out: Dict[str, Any] = {"message": "Plan başarıyla hazırlandı."}
    if docx_name:
        out["docx_url"] = file_url(docx_name)
    if xlsx_name:
        out["xlsx_url"] = file_url(xlsx_name)
    return out


@app.post("/api/plan")
async def api_plan(
    request: Request,
    file_name: str = Form(...),
    month: str = Form(...),
    interval_days: int = Form(...),
    start_date: Optional[str] = Form(default=None),
    hotel_contact: Optional[str] = Form(default=""),
    images: List[UploadFile] = File(default_factory=list),
):
    require_login(request)

    file_name_clean = _safe_filename(file_name)
    try:
        docx_name = _make_docx(file_name_clean, month, int(interval_days), start_date, hotel_contact or "", images)
        xlsx_name = _make_xlsx(file_name_clean, month, int(interval_days), start_date, hotel_contact or "")
    except Exception as e:
        raise HTTPException(500, f"Dosya oluşturulamadı: {e}")

    return JSONResponse(_plan_response_urls(request, docx_name, xlsx_name))


# Fallback form-post (aynı işlev)
@app.post("/plan")
async def plan_fallback(
    request: Request,
    file_name: str = Form(...),
    month: str = Form(...),
    interval_days: int = Form(...),
    start_date: Optional[str] = Form(default=None),
    hotel_contact: Optional[str] = Form(default=""),
    images: List[UploadFile] = File(default_factory=list),
):
    return await api_plan(request, file_name, month, interval_days, start_date, hotel_contact, images)


# İndirilebilir dosyalar
@app.get("/files/{name}")
async def get_file(name: str):
    path = os.path.join(FILES_DIR, name)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="not found")
    media = "application/octet-stream"
    if name.endswith(".docx"):
        media = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    elif name.endswith(".xlsx"):
        media = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    return FileResponse(path, media_type=media, filename=name)


# ----------------------------
# HEALTH
# ----------------------------
@app.get("/health")
def health():
    return {"ok": True}


@app.get("/healthz")
def healthz():
    return {"ok": True}


# ----------------------------
# ERROR HANDLERS
# ----------------------------
@app.exception_handler(HTTPException)
async def http_exc_handler(request: Request, exc: HTTPException):
    # 303 -> login yönlendirmesi
    if exc.status_code == status.HTTP_303_SEE_OTHER:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    # 403 -> admin yoksa
    if exc.status_code == status.HTTP_403_FORBIDDEN:
        # basit bir metin döndür (istersen template yapabilirsin)
        return PlainTextResponse("Yetkisiz işlem.", status_code=403)

    # varsayılan
    return PlainTextResponse(exc.detail or "Hata", status_code=exc.status_code)
