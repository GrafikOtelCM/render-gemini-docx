import os, io, time, calendar, secrets, base64, sqlite3, hashlib, json
from datetime import datetime, date
from typing import List, Optional

from fastapi import FastAPI, Request, UploadFile, File, Form, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.datastructures import URL
import bcrypt
import requests
from docx import Document
from docx.shared import Inches
from PIL import Image

# -------------------------
# CONFIG
# -------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")
STATIC_DIR = os.path.join(BASE_DIR, "static")

# DB yolunu /tmp'ye alıyoruz ki free planda disksiz sorunsuz çalışsın.
USAGE_DB_PATH = os.getenv("USAGE_DB_PATH", "/tmp/usage.db")
DB_DIR = os.path.dirname(USAGE_DB_PATH) or BASE_DIR
os.makedirs(DB_DIR, exist_ok=True)

print(f"[INFO] Using DB at: {USAGE_DB_PATH}")

APP_NAME = os.getenv("APP_NAME", "Gemini Plan DOCX")
# SECRET_KEY env’de yoksa çalışsın diye default veriyoruz (deploymentta ENV koyman yine de önerilir).
SECRET_KEY = os.getenv("SECRET_KEY", "dev-" + secrets.token_urlsafe(48))

# Kod içinden admin seed (sen kodda istediğini yazarsın); ENV varsa o öncelikli.
ADMIN_CODE_USER = os.getenv("ADMIN_CODE_USER", "otelgrafikadmin")
ADMIN_CODE_PASS = os.getenv("ADMIN_CODE_PASS", "otelgrafikpass")
DEFAULT_USER_ROLE = "user"

# Gemini
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

# Planlama: ayın 1'inden **29’una** kadar (sen istemiştin)
PLAN_CUTOFF_DAY = int(os.getenv("PLAN_CUTOFF_DAY", "29"))

# -------------------------
# APP & MIDDLEWARE
# -------------------------
app = FastAPI()
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, same_site="lax", https_only=False)

class SecurityHeaders(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        resp = await call_next(request)
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        resp.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        return resp

app.add_middleware(SecurityHeaders)

# Static ve templates mount
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Jinja2Templates(directory=TEMPLATES_DIR)

# -------------------------
# DB
# -------------------------
def get_db() -> sqlite3.Connection:
    try:
        conn = sqlite3.connect(USAGE_DB_PATH, timeout=30, check_same_thread=False)
    except sqlite3.OperationalError as e:
        raise RuntimeError(
            f"SQLite açılamadı: {USAGE_DB_PATH} — {e}. "
            "USAGE_DB_PATH'i /tmp/usage.db yapın veya mount path yazılabilir olmalı."
        )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn

def ensure_db():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        pw_hash TEXT NOT NULL,
        role TEXT NOT NULL CHECK (role in ('admin','employee'))
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT NOT NULL,
        role TEXT NOT NULL,
        hotel_tag TEXT,
        month INTEGER,
        year INTEGER,
        images_count INTEGER,
        created_at TEXT NOT NULL
    )
    """)
    conn.commit()
    conn.close()

def create_user_if_missing(username: str, password_plain: str, role: str = "admin"):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM users WHERE username = ?", (username,))
    if cur.fetchone():
        conn.close()
        return
    pw_hash = bcrypt.hashpw(password_plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    cur.execute(
        "INSERT OR IGNORE INTO users (username, pw_hash, role) VALUES (?, ?, ?)",
        (username, pw_hash, role),
    )
    conn.commit()
    conn.close()

def seed_admin_from_code():
    create_user_if_missing(ADMIN_CODE_USER, ADMIN_CODE_PASS, "admin")

@app.on_event("startup")
def on_startup():
    ensure_db()
    seed_admin_from_code()
    print("[INFO] Startup complete.")

# -------------------------
# AUTH HELPERS
# -------------------------
def get_current_user(request: Request):
    user = request.session.get("user")
    role = request.session.get("role")
    if not user:
        raise HTTPException(status_code=401, detail="Giriş gerekli")
    return user, role

def require_admin(role: str):
    if role != "admin":
        raise HTTPException(status_code=403, detail="Yalnızca admin")

# -------------------------
# ROUTES: AUTH
# -------------------------
@app.get("/", include_in_schema=False)
def root(request: Request):
    if request.session.get("user"):
        return RedirectResponse("/dashboard", status_code=303)
    return RedirectResponse("/login?next=/dashboard", status_code=303)

@app.get("/login", response_class=HTMLResponse)
def login_get(request: Request, next: Optional[str] = "/dashboard"):
    return templates.TemplateResponse("login.html", {
        "request": request,
        "app_name": APP_NAME,
        "next": next
    })

@app.post("/login")
def login_post(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: Optional[str] = Form("/dashboard")
):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT username, pw_hash, role FROM users WHERE username = ?", (username,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return templates.TemplateResponse("login.html", {
            "request": request, "app_name": APP_NAME, "next": next,
            "error": "Kullanıcı bulunamadı"
        }, status_code=400)
    if not bcrypt.checkpw(password.encode("utf-8"), row["pw_hash"].encode("utf-8")):
        return templates.TemplateResponse("login.html", {
            "request": request, "app_name": APP_NAME, "next": next,
            "error": "Parola hatalı"
        }, status_code=400)
    request.session["user"] = row["username"]
    request.session["role"] = row["role"]
    return RedirectResponse(next or "/dashboard", status_code=303)

@app.get("/logout", include_in_schema=False)
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)

# -------------------------
# ROUTES: DASHBOARD & USERS
# -------------------------
@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request):
    if not request.session.get("user"):
        return RedirectResponse("/login?next=/dashboard", status_code=303)
    user = request.session["user"]; role = request.session["role"]

    # Kişiye özel log görünürlüğü: admin her şeyi, employee sadece kendi logunu görür.
    conn = get_db()
    cur = conn.cursor()
    if role == "admin":
        cur.execute("SELECT * FROM logs ORDER BY id DESC LIMIT 50")
    else:
        cur.execute("SELECT * FROM logs WHERE username = ? ORDER BY id DESC LIMIT 50", (user,))
    logs = [dict(r) for r in cur.fetchall()]
    conn.close()

    months = [
        {"value": 1, "label": "Ocak"},
        {"value": 2, "label": "Şubat"},
        {"value": 3, "label": "Mart"},
        {"value": 4, "label": "Nisan"},
        {"value": 5, "label": "Mayıs"},
        {"value": 6, "label": "Haziran"},
        {"value": 7, "label": "Temmuz"},
        {"value": 8, "label": "Ağustos"},
        {"value": 9, "label": "Eylül"},
        {"value": 10, "label": "Ekim"},
        {"value": 11, "label": "Kasım"},
        {"value": 12, "label": "Aralık"},
    ]
    years = list(range(2024, 2031))
    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "app_name": APP_NAME,
        "user": user,
        "role": role,
        "months": months,
        "years": years,
        "logs": logs
    })

@app.get("/admin/users", response_class=HTMLResponse)
def users_admin_panel(request: Request):
    user, role = get_current_user(request)
    require_admin(role)
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, username, role FROM users ORDER BY id DESC")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return templates.TemplateResponse("users.html", {
        "request": request,
        "app_name": APP_NAME,
        "user": user,
        "rows": rows
    })

@app.post("/admin/users/create")
def users_create(request: Request,
                 new_username: str = Form(...),
                 new_password: str = Form(...),
                 new_role: str = Form(...)):
    user, role = get_current_user(request)
    require_admin(role)
    if new_role not in ("admin", "employee"):
        raise HTTPException(400, "Rol: admin/employee")
    create_user_if_missing(new_username, new_password, new_role)
    return RedirectResponse("/admin/users", status_code=303)

@app.post("/admin/users/delete")
def users_delete(request: Request, user_id: int = Form(...)):
    user, role = get_current_user(request)
    require_admin(role)
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM users WHERE id = ?", (user_id,))
    conn.commit()
    conn.close()
    return RedirectResponse("/admin/users", status_code=303)

# -------------------------
# GEMINI: Caption & Hashtag
# -------------------------
def gemini_caption_and_hashtags_for_image(img_bytes: bytes) -> dict:
    """
    GEMINI_API_KEY yoksa basit fallback metin üretir.
    Varsa REST ile Gemini 2.5 Flash'a çağrı dener (başarısız olursa fallback).
    """
    fallback = {
        "caption": "Tatil ruhunu yansıtan ferah bir kare. Konfor ve şıklık bir arada.",
        "hashtags": ["#tatilbudur", "#oteltavsiyesi", "#erkenrezervasyon"]
    }
    if not GEMINI_API_KEY:
        return fallback

    try:
        # Basit base64 encode; birçok REST örneği bu şekilde multi-part prompt kabul eder.
        b64 = base64.b64encode(img_bytes).decode("utf-8")
        payload = {
            "contents": [{
                "role": "user",
                "parts": [
                    {"text": "Bu görsel için Türkçe, pazarlama odaklı 1 açıklama ve 5 otel/seyahat odaklı hashtag üret. 'tatilbudur' ve otel lokasyonu temalı olsun. JSON ver."},
                    {"inline_data": {"mime_type": "image/jpeg", "data": b64}}
                ]
            }]
        }
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
        r = requests.post(url, json=payload, timeout=30)
        r.raise_for_status()
        data = r.json()
        # Basit parse
        text = ""
        try:
            text = data["candidates"][0]["content"]["parts"][0]["text"]
        except Exception:
            text = ""
        # Metinde JSON varsa çek
        try:
            # JSON parçayı bulma (çok basit yaklaşım)
            start = text.find("{")
            end = text.rfind("}")
            if start != -1 and end != -1 and end > start:
                obj = json.loads(text[start:end+1])
                caption = obj.get("caption") or obj.get("aciklama") or fallback["caption"]
                hashtags = obj.get("hashtags") or obj.get("etiketler") or fallback["hashtags"]
                if isinstance(hashtags, str):
                    hashtags = [h.strip() for h in hashtags.split() if h.strip().startswith("#")]
                return {"caption": caption, "hashtags": hashtags[:5] or fallback["hashtags"]}
        except Exception:
            pass

        # JSON yoksa metinden yakala
        if text:
            lines = [l.strip() for l in text.splitlines() if l.strip()]
            cap = lines[0][:220] if lines else fallback["caption"]
            tags = [w for w in " ".join(lines[1:]).split() if w.startswith("#")]
            if not tags:
                tags = fallback["hashtags"]
            return {"caption": cap, "hashtags": tags[:5]}
    except Exception as e:
        print("[WARN] Gemini fallback:", e)

    return fallback

# -------------------------
# PLAN TARİHLERİ
# -------------------------
def build_schedule(year: int, month: int, step_days: int) -> List[date]:
    # 1'den başla, 29'unda bitir (isteğin doğrultusunda). Ay 29'dan kısa ise min al.
    last = min(PLAN_CUTOFF_DAY, calendar.monthrange(year, month)[1])
    out = []
    d = 1
    while d <= last:
        out.append(date(year, month, d))
        d += max(step_days, 1)
    return out

# -------------------------
# DOCX OLUŞTUR
# -------------------------
def image_to_jpeg_bytes(img_bytes: bytes) -> bytes:
    img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    o = io.BytesIO()
    img.save(o, format="JPEG", quality=90)
    return o.getvalue()

def build_docx(hotel_tag: str, schedule: List[date], images: List[bytes]) -> bytes:
    doc = Document()
    for idx, img_raw in enumerate(images):
        when = schedule[idx % len(schedule)]
        doc.add_heading(f"{hotel_tag} – Paylaşım Tarihi: {when.strftime('%d.%m.%Y')}", level=2)

        # Gemini içeriği
        gen = gemini_caption_and_hashtags_for_image(img_raw)
        caption = gen["caption"]
        hashtags = " ".join(gen["hashtags"])

        # Görseli ekle
        jpeg = image_to_jpeg_bytes(img_raw)
        img_stream = io.BytesIO(jpeg)
        doc.add_paragraph(caption)
        doc.add_paragraph(hashtags)
        doc.add_picture(img_stream, width=Inches(5.8))
        doc.add_page_break()
    buf = io.BytesIO()
    doc.save(buf)
    buf.seek(0)
    return buf.read()

# -------------------------
# ROUTE: OLUŞTUR ve İNDİR
# -------------------------
@app.post("/generate")
async def generate(
    request: Request,
    hotel_tag: str = Form(...),
    year: int = Form(...),
    month: int = Form(...),
    every_n_days: int = Form(...),
    files: List[UploadFile] = File(...)
):
    user, role = get_current_user(request)
    # Görselleri oku
    images = []
    for f in files:
        content = await f.read()
        images.append(content)

    # Takvimi kur
    schedule = build_schedule(year, month, every_n_days)
    if not schedule:
        raise HTTPException(400, "Plan boş: ay/step ayarlarını kontrol et")

    # DOCX üret
    docx_bytes = build_docx(hotel_tag.strip() or "Plan", schedule, images)

    # Logla (user rol izolasyonu dashboard’da)
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO logs (username, role, hotel_tag, month, year, images_count, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (user, role, hotel_tag.strip(), month, year, len(images), datetime.utcnow().isoformat()))
    conn.commit()
    conn.close()

    filename = f"{hotel_tag}_{year}-{month:02d}_plan.docx".replace(" ", "_")
    return StreamingResponse(
        io.BytesIO(docx_bytes),
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )
