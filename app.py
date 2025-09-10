# app.py
import os
import sqlite3
import secrets
import hashlib
import json
import io
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, Request, Form, Depends, HTTPException
from fastapi import UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from itsdangerous import URLSafeSerializer
import bcrypt

# =========================
# ENV & Sabitler
# =========================
APP_NAME = os.getenv("APP_NAME", "Otel Planlama Stüdyosu")
SECRET_KEY = os.getenv(
    "SECRET_KEY",
    "3zYyE0y6l3mU2l3H4q2oQk7G8f1uF5Y0vW6zR9rJ2kP1xN8cT4lS3bD2mH7qA5",
)
USAGE_DB_PATH = os.getenv("USAGE_DB_PATH", "/tmp/usage.db")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_MAX_CONCURRENCY = int(os.getenv("GEMINI_MAX_CONCURRENCY", "1"))

USD_TRY_RATE = float(os.getenv("USD_TRY_RATE", "36.0"))
ASSUME_IN_TOKENS = int(os.getenv("ASSUME_IN_TOKENS", "400"))
ASSUME_OUT_TOKENS = int(os.getenv("ASSUME_OUT_TOKENS", "220"))
RATE_IN_PER_MTOK = os.getenv("RATE_IN_PER_MTOK", "")
RATE_OUT_PER_MTOK = os.getenv("RATE_OUT_PER_MTOK", "")
RATE_LIMIT_MAX = int(os.getenv("RATE_LIMIT_MAX", "60"))

ADMIN_CODE_USER = os.getenv("ADMIN_CODE_USER", "admin")
ADMIN_CODE_PASS = os.getenv("ADMIN_CODE_PASS", "admin123")

# =========================
# FastAPI & Middleware
# =========================
app = FastAPI(title=APP_NAME)
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY)
serializer = URLSafeSerializer(SECRET_KEY, salt="csrf")

# static ve templates klasörleri opsiyonel olabilir; yoksa hata almayalım
if os.path.isdir("static"):
    app.mount("/static", StaticFiles(directory="static"), name="static")
templates = None
if os.path.isdir("templates"):
    templates = Jinja2Templates(directory="templates")


# =========================
# Yardımcılar
# =========================
def get_db() -> sqlite3.Connection:
    # /tmp kullanılacağı için ek dosya yaratmayan modlar
    conn = sqlite3.connect(USAGE_DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # WAL yerine MEMORY kullanıyoruz: free planda ek .wal/.shm dosyaları sorun çıkarabilir
    try:
        conn.execute("PRAGMA journal_mode=MEMORY;")
        conn.execute("PRAGMA synchronous=OFF;")
    except Exception:
        pass
    return conn


def ensure_db():
    # /tmp her zaman vardır; ancak dosya yoksa oluşturalım
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            pw_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK (role IN ('admin','editor'))
        );
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            action TEXT NOT NULL,
            ref TEXT,
            created_at TEXT NOT NULL
        );
        """
    )
    conn.commit()
    conn.close()


def hash_pw(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_pw(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        return False


def create_user_if_missing(username: str, password: str, role: str = "admin"):
    conn = get_db()
    cur = conn.cursor()
    pw_hash = hash_pw(password)
    try:
        cur.execute(
            "INSERT INTO users (username, pw_hash, role) VALUES (?, ?, ?)",
            (username, pw_hash, role),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        # Kullanıcı zaten varsa sessizce geç
        pass
    finally:
        conn.close()


def log_action(username: str, action: str, ref: Optional[str] = None):
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO logs (username, action, ref, created_at) VALUES (?, ?, ?, ?)",
        (username, action, ref, datetime.utcnow().isoformat() + "Z"),
    )
    conn.commit()
    conn.close()


def current_user(request: Request) -> Optional[dict]:
    u = request.session.get("user")
    if not u:
        return None
    return u


def require_user(request: Request) -> dict:
    u = current_user(request)
    if not u:
        raise HTTPException(status_code=303, detail="login required")
    return u


def require_admin(request: Request) -> dict:
    u = require_user(request)
    if u["role"] != "admin":
        raise HTTPException(status_code=403, detail="admin only")
    return u


def get_csrf_token(request: Request) -> str:
    token = serializer.dumps({"sid": request.session.get("sid", "") or secrets.token_hex(8)})
    request.session["csrf"] = token
    return token


def check_csrf(request: Request, token: str):
    saved = request.session.get("csrf")
    if not saved or saved != token:
        raise HTTPException(status_code=400, detail="CSRF failed")


def render(request: Request, template_name: str, context: dict) -> Response:
    """
    Template yoksa sade HTML döner; prod’da templates klasörünüz varsa bu çağrı Jinja2 ile render eder.
    """
    base_ctx = {"request": request, "app_name": APP_NAME}
    base_ctx.update(context or {})
    if templates:
        from jinja2 import TemplateNotFound

        try:
            return templates.TemplateResponse(template_name, base_ctx)
        except TemplateNotFound:
            pass
    # Basit fallback HTML
    body = f"""
    <html>
      <head><title>{APP_NAME}</title></head>
      <body style="font-family:Inter,Arial;margin:32px">
        <h2>{APP_NAME}</h2>
        <pre style="background:#f6f6f6;padding:16px;border-radius:8px">{template_name} bulunamadı.
        Geçici basit görünüm gösteriliyor.</pre>
        <div>
          <a href="/login">Giriş</a> |
          <a href="/dashboard">Panel</a> |
          <a href="/admin/users">Kullanıcı Yönetimi</a> |
          <a href="/logs">Loglar</a>
        </div>
        <hr/>
        <div><strong>İçerik:</strong><br/>{json.dumps(base_ctx, ensure_ascii=False, indent=2)}</div>
      </body>
    </html>
    """
    return HTMLResponse(body)


# =========================
# Lifespan
# =========================
def _startup():
    print(f"[INFO] Using DB at: {USAGE_DB_PATH}")
    ensure_db()
    # Admin’i ENV’den tohumla (mevcutsa sessizce geç)
    create_user_if_missing(ADMIN_CODE_USER, ADMIN_CODE_PASS, "admin")


@app.on_event("startup")
def on_startup():
    _startup()


# =========================
# Health Check
# =========================
@app.get("/health")
def health():
    return {"ok": True, "app": APP_NAME, "db": USAGE_DB_PATH}


# =========================
# Auth
# =========================
@app.get("/login")
def login_get(request: Request, next: str = "/"):
    return render(
        request,
        "login.html",
        {
            "next": next,
            "csrf_token": get_csrf_token(request),
        },
    )


@app.post("/login")
def login_post(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    csrf_token: str = Form(...),
    next: str = Form("/"),
):
    check_csrf(request, csrf_token)
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, username, pw_hash, role FROM users WHERE username=?", (username,))
    row = cur.fetchone()
    conn.close()
    if not row or not verify_pw(password, row["pw_hash"]):
        # Basit dönüş
        return render(
            request,
            "login.html",
            {
                "error": "Kullanıcı adı veya parola hatalı",
                "next": next,
                "csrf_token": get_csrf_token(request),
            },
        )
    # Session
    request.session["sid"] = secrets.token_hex(16)
    request.session["user"] = {"id": row["id"], "username": row["username"], "role": row["role"]}
    log_action(row["username"], "login")
    return RedirectResponse(next if next else "/", status_code=303)


@app.get("/logout")
def logout(request: Request):
    u = current_user(request)
    if u:
        log_action(u["username"], "logout")
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# =========================
# Dashboard (örnek)
# =========================
@app.get("/")
def home(request: Request):
    if not current_user(request):
        return RedirectResponse("/login?next=/", status_code=303)
    return RedirectResponse("/dashboard", status_code=303)


@app.get("/dashboard")
def dashboard(request: Request):
    u = require_user(request)
    # Basit özet bilgileri gösterelim
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) AS c FROM logs")
    total_logs = cur.fetchone()["c"]
    cur.execute("SELECT COUNT(*) AS c FROM users")
    total_users = cur.fetchone()["c"]
    conn.close()
    return render(
        request,
        "dashboard.html",
        {
            "user": u,
            "total_logs": total_logs,
            "total_users": total_users,
            "gemini_model": GEMINI_MODEL,
        },
    )


# =========================
# Kullanıcı Yönetimi (Admin)
# =========================
@app.get("/admin/users")
def users_page(request: Request):
    u = require_admin(request)
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, username, role FROM users ORDER BY username")
    users = [dict(r) for r in cur.fetchall()]
    conn.close()
    return render(
        request,
        "users.html",
        {"user": u, "users": users, "csrf_token": get_csrf_token(request)},
    )


@app.post("/admin/users/create")
def users_create(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    role: str = Form("editor"),
    csrf_token: str = Form(...),
):
    u = require_admin(request)
    check_csrf(request, csrf_token)
    if role not in ("admin", "editor"):
        role = "editor"
    create_user_if_missing(username, password, role)
    log_action(u["username"], "user_create", username)
    return RedirectResponse("/admin/users", status_code=303)


@app.post("/admin/users/delete")
def users_delete(
    request: Request,
    username: str = Form(...),
    csrf_token: str = Form(...),
):
    u = require_admin(request)
    check_csrf(request, csrf_token)
    if username == u["username"]:
        raise HTTPException(status_code=400, detail="Kendi hesabınızı silemezsiniz.")
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM users WHERE username=?", (username,))
    conn.commit()
    conn.close()
    log_action(u["username"], "user_delete", username)
    return RedirectResponse("/admin/users", status_code=303)


# =========================
# Loglar
# =========================
@app.get("/logs")
def logs_page(request: Request):
    u = require_user(request)
    conn = get_db()
    cur = conn.cursor()
    if u["role"] == "admin":
        cur.execute("SELECT id, username, action, ref, created_at FROM logs ORDER BY id DESC LIMIT 200")
    else:
        cur.execute(
            "SELECT id, username, action, ref, created_at FROM logs WHERE username=? ORDER BY id DESC LIMIT 200",
            (u["username"],),
        )
    logs = [dict(r) for r in cur.fetchall()]
    conn.close()
    return render(
        request,
        "logs.html",
        {"user": u, "logs": logs, "csrf_token": get_csrf_token(request)},
    )


@app.post("/logs/clear")
def logs_clear(request: Request, csrf_token: str = Form(...)):
    u = require_admin(request)
    check_csrf(request, csrf_token)
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM logs")
    conn.commit()
    conn.close()
    log_action(u["username"], "logs_cleared")
    return RedirectResponse("/logs", status_code=303)


# =========================
# Basit DOCX Üretim Ucu (stub)
# Not: Mevcut şablonlarınız/JS arayüzünüz buna POST atabilir.
# =========================
from docx import Document
from docx.shared import Inches
from PIL import Image


def _safe_add_image(doc: Document, file: UploadFile):
    # UploadFile’i RAM’de işle, /tmp’ye dokunmadan.
    raw = file.file.read()
    img_bytes = io.BytesIO(raw)
    # Boyut doğrulama
    try:
        with Image.open(img_bytes) as im:
            im.verify()
    except Exception:
        raise HTTPException(status_code=400, detail=f"Geçersiz görsel: {file.filename}")
    # tekrar başa sar
    img_bytes.seek(0)
    # docx’e ekle
    doc.add_picture(img_bytes, width=Inches(5.5))


@app.post("/api/generate-docx")
async def generate_docx(
    request: Request,
    hotel_name: str = Form(...),
    month: str = Form(...),  # YYYY-MM
    interval_days: int = Form(3),
    files: list[UploadFile] = File([]),
):
    u = require_user(request)
    # Basit plan (ayın 1'inden başlat, interval’a göre dağıt)
    try:
        start = datetime.strptime(month + "-01", "%Y-%m-%d")
    except Exception:
        raise HTTPException(status_code=400, detail="Geçersiz ay formatı, örn: 2025-10")

    # 29’unda bitecek şekilde (talebiniz)
    plan_dates = []
    day = 1
    while day <= 29 and len(plan_dates) < len(files):
        plan_dates.append(datetime(start.year, start.month, day))
        day += max(1, int(interval_days))

    # DOCX oluştur
    doc = Document()
    doc.add_heading(f"{hotel_name} - {month} Planı", level=1)

    for idx, f in enumerate(files):
        doc.add_paragraph(f"Görsel: {f.filename}")
        # tarih varsa yaz
        if idx < len(plan_dates):
            doc.add_paragraph(f"Paylaşım Tarihi: {plan_dates[idx].strftime('%d.%m.%Y')}")
        # görsel ekle
        _safe_add_image(doc, f)
        # Basit placeholder açıklama/hashtag (Gemini entegrasyonu sizin önceki endpoint’inize bağlı)
        doc.add_paragraph(f"Açıklama (örnek): {hotel_name} için sezon içgörüleri ve fırsatlar.")
        doc.add_paragraph("#tatil #otelifırsat #erkenrezervasyon")
        doc.add_paragraph("----------------------------------------")

    # RAM’de döndür
    buf = io.BytesIO()
    doc.save(buf)
    buf.seek(0)

    # Logla
    log_ref = f"{hotel_name}-{month}"
    log_action(u["username"], "docx_generated", log_ref)

    headers = {
        "Content-Disposition": f'attachment; filename="{hotel_name}_{month}.docx"'
    }
    return Response(content=buf.read(), media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document", headers=headers)


# =========================
# Maliyet Tahmini (opsiyonel)
# =========================
@app.get("/api/estimate")
def estimate_api(request: Request, images: int = 10):
    # Basit varsayım
    in_tok = images * ASSUME_IN_TOKENS
    out_tok = images * ASSUME_OUT_TOKENS
    rate_in = float(RATE_IN_PER_MTOK) if RATE_IN_PER_MTOK else 0.0
    rate_out = float(RATE_OUT_PER_MTOK) if RATE_OUT_PER_MTOK else 0.0
    usd = (in_tok / 1000.0) * rate_in + (out_tok / 1000.0) * rate_out
    tl = usd * USD_TRY_RATE
    return {"images": images, "in_tokens": in_tok, "out_tokens": out_tok, "usd": round(usd, 4), "try": round(tl, 2)}
