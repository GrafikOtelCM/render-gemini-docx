import os, io, base64, sqlite3, calendar, uuid, time
from datetime import datetime, date, timedelta, timezone
from typing import List, Optional, Tuple

from fastapi import FastAPI, Request, UploadFile, Form, Depends, HTTPException
from fastapi.responses import RedirectResponse, FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.datastructures import FormData
import httpx
from PIL import Image
from docx import Document
from docx.shared import Inches, Pt
from docx.enum.text import WD_ALIGN_PARAGRAPH
import bcrypt
import openpyxl

APP_NAME = "Gemini Plan/Docx"
TZ = timezone(timedelta(hours=3))  # Europe/Istanbul (+03:00)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# --- ENV & Config ---
SECRET_KEY = os.environ.get("SECRET_KEY")
if not SECRET_KEY:
    raise RuntimeError("SECRET_KEY env değişkeni zorunlu. Render → Environment ekranından ekleyin.")

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "AIzaSyDdUV3SuQ1bbhqILvR_70wGRSMdDGkOoNI")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

USD_TRY_RATE = float(os.environ.get("USD_TRY_RATE", "41.20"))
RATE_IN_PER_MTOK = float(os.environ.get("RATE_IN_PER_MTOK", "0.30"))   # USD / 1M input tokens
RATE_OUT_PER_MTOK = float(os.environ.get("RATE_OUT_PER_MTOK", "2.50")) # USD / 1M output tokens
ASSUME_IN_TOKENS = int(os.environ.get("ASSUME_IN_TOKENS", "400"))
ASSUME_OUT_TOKENS = int(os.environ.get("ASSUME_OUT_TOKENS", "80"))

USAGE_DB_PATH = os.environ.get("USAGE_DB_PATH", os.path.join(BASE_DIR, "usage.db"))
UPLOAD_TMP_DIR = os.path.join(BASE_DIR, "tmp_uploads")
GENERATED_DIR = os.path.join(BASE_DIR, "generated")
os.makedirs(UPLOAD_TMP_DIR, exist_ok=True)
os.makedirs(GENERATED_DIR, exist_ok=True)

# Admin sabitleri (sen düzenle)
ADMIN_CODE_USER = "otelgrafikadmin"
ADMIN_CODE_PASS = "otelgrafikpass"

# --- FastAPI ---
app = FastAPI(title=APP_NAME)
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, session_cookie="sid", https_only=False, max_age=60*60*8)

# Static & templates
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

# --- DB helpers ---
def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(USAGE_DB_PATH, timeout=30, check_same_thread=False)
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
        role TEXT NOT NULL CHECK(role IN ('admin','staff')),
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    )""")
    cur.execute("""
    CREATE TABLE IF NOT EXISTS logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        username TEXT NOT NULL,
        role TEXT NOT NULL,
        hotel TEXT NOT NULL,
        month INTEGER NOT NULL,
        year INTEGER NOT NULL,
        images_count INTEGER NOT NULL,
        file_name TEXT NOT NULL,
        cost_usd REAL NOT NULL,
        cost_try REAL NOT NULL,
        token_in INTEGER NOT NULL,
        token_out INTEGER NOT NULL,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        FOREIGN KEY(user_id) REFERENCES users(id)
    )""")
    conn.commit()
    conn.close()

def create_user_if_missing(username: str, password: str, role: str = "staff"):
    conn = get_db()
    try:
        pw_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
        conn.execute(
            "INSERT OR IGNORE INTO users (username, pw_hash, role) VALUES (?, ?, ?)",
            (username, pw_hash, role),
        )
        # Varsa rolü güncelle
        conn.execute("UPDATE users SET role = ? WHERE username = ?", (role, username))
        conn.commit()
    finally:
        conn.close()

def get_user_by_username(username: str):
    conn = get_db()
    try:
        cur = conn.execute("SELECT * FROM users WHERE username=?", (username,))
        return cur.fetchone()
    finally:
        conn.close()

def get_user_by_id(uid: int):
    conn = get_db()
    try:
        cur = conn.execute("SELECT * FROM users WHERE id=?", (uid,))
        return cur.fetchone()
    finally:
        conn.close()

def seed_admin_from_code():
    try:
        create_user_if_missing(ADMIN_CODE_USER, ADMIN_CODE_PASS, "admin")
    except Exception as e:
        print(f"[WARN] seed_admin_from_code skipped: {e}")

# --- CSRF helpers ---
def get_csrf_token(request: Request) -> str:
    token = request.session.get("csrf")
    if not token:
        token = base64.urlsafe_b64encode(os.urandom(32)).decode("utf-8").rstrip("=")
        request.session["csrf"] = token
    return token

def require_csrf(request: Request, token: str):
    sess = request.session.get("csrf")
    if not sess or not token or token != sess:
        raise HTTPException(status_code=400, detail="CSRF doğrulaması başarısız")

# --- Auth dependencies ---
def require_login(request: Request):
    uid = request.session.get("uid")
    if not uid:
        next_url = request.url.path or "/"
        return RedirectResponse(url=f"/login?next={next_url}", status_code=303)
    return None

def require_admin(request: Request):
    uid = request.session.get("uid")
    role = request.session.get("role")
    if not uid or role != "admin":
        return RedirectResponse(url="/", status_code=303)
    return None

# --- Rate limit (basit IP + path) ---
RATE_LIMIT_MAX = int(os.environ.get("RATE_LIMIT_MAX", "12"))
_RATE_BUCKET = {}  # {(ip, path): [ts, count]}

class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        ip = request.headers.get("x-forwarded-for", request.client.host if request.client else "0.0.0.0").split(",")[0].strip()
        key = (ip, request.url.path)
        now = time.time()
        window = 60.0
        ts, cnt = _RATE_BUCKET.get(key, (now, 0))
        if now - ts > window:
            ts, cnt = now, 0
        cnt += 1
        _RATE_BUCKET[key] = (ts, cnt)
        if cnt > RATE_LIMIT_MAX and request.method != "GET":
            return JSONResponse({"error": "Rate limit aşıldı. Biraz sonra tekrar deneyin."}, status_code=429)
        return await call_next(request)

app.add_middleware(RateLimitMiddleware)

# --- Utils ---
def make_dates_for_month(year: int, month: int, every_n_days: int, count: int) -> List[date]:
    if every_n_days < 1:
        every_n_days = 1
    last_day = 29  # özel istek: ay 1–29 arası
    d = date(year, month, 1)
    dates = []
    while d.day <= last_day and len(dates) < count:
        dates.append(d)
        d = d + timedelta(days=every_n_days)
        if d.month != month:
            break
    # Eğer görsel sayısı > planlanan tarih sayısıysa, en son tarihten devam
    while len(dates) < count and (dates[-1].month == month and dates[-1].day <= last_day):
        d = dates[-1] + timedelta(days=every_n_days)
        if d.month != month or d.day > last_day:
            break
        dates.append(d)
    # Hala yetmiyorsa kalanları son güne sabitle
    while len(dates) < count:
        dates.append(date(year, month, min(last_day, calendar.monthrange(year, month)[1])))
    return dates

def image_to_inline_part(img_bytes: bytes, mime: str) -> dict:
    b64 = base64.b64encode(img_bytes).decode("utf-8")
    return {"inlineData": {"mimeType": mime, "data": b64}}

async def gemini_caption_and_tags(client: httpx.AsyncClient, img_bytes: bytes, mime: str, hotel_name: str) -> Tuple[str, List[str], int, int]:
    """
    Döner: (caption, hashtags, input_tokens, output_tokens)
    """
    if not GEMINI_API_KEY:
        # API yoksa, mock metin
        caption = f"{hotel_name} için özenle seçilmiş karelerden biri. Tatil ruhunu şimdi yakalayın!"
        tags = ["#tatil", "#otel", "#erkenrezervasyon", "#muhtesemkareler", "#yazkacagi", "#gununkaresi", "#turkiye", "#tatilbudur"]
        return caption, tags[:8], ASSUME_IN_TOKENS, ASSUME_OUT_TOKENS

    url = f"https://generativelanguage.googleapis.com/v1/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    system_prompt = (
        "Sen deneyimli bir sosyal medya içerik editörüsün. Görseli analiz ederek özgün, kısa ve aksiyon çağrısı içeren "
        "Türkçe bir açıklama yaz. 110–180 karakter arası olsun, emoji kullanımı makul. Ardından 8–12 adet Türkçe hashtag öner "
        "ve otel/konum bağlamına uygun olsun. Marka: TatilBudur. Aşırı genel (#photo, #insta) taglardan kaçın. "
        f"Otel adı: {hotel_name}."
    )
    payload = {
        "contents": [
            {
                "parts": [
                    {"text": system_prompt},
                    image_to_inline_part(img_bytes, mime),
                ]
            }
        ],
        "generationConfig": {
            "temperature": 0.7,
            "topK": 64,
            "topP": 0.9,
            "maxOutputTokens": 180,
        }
    }

    r = await client.post(url, json=payload, timeout=60)
    r.raise_for_status()
    data = r.json()
    # metin çıkar
    text = ""
    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception:
        text = ""

    # caption / hashtag ayırma
    caption = text
    hashtags: List[str] = []
    # basit ayrıştırma
    lines = [l.strip() for l in caption.splitlines() if l.strip()]
    h_candidates: List[str] = []
    for ln in lines:
        if "#" in ln:
            h_candidates.extend([w for w in ln.split() if w.startswith("#")])
    if h_candidates:
        hashtags = list(dict.fromkeys(h_candidates))[:12]  # uniq + limit
        # caption’dan hashtag satırlarını temizle
        caption = "\n".join([ln for ln in lines if not ln.startswith("#")]).strip()
        if not caption:
            caption = f"{hotel_name}’da tatil ruhunu yakalayın. Rezervasyon için profili ziyaret edin!"
    # usage
    in_tok = data.get("usageMetadata", {}).get("promptTokenCount", ASSUME_IN_TOKENS)
    out_tok = data.get("usageMetadata", {}).get("candidatesTokenCount", ASSUME_OUT_TOKENS)
    return caption, hashtags[:12], int(in_tok), int(out_tok)

def token_cost_usd(in_tok: int, out_tok: int) -> float:
    usd = (in_tok/1_000_000.0)*RATE_IN_PER_MTOK + (out_tok/1_000_000.0)*RATE_OUT_PER_MTOK
    return round(usd, 6)

def px_to_inches(px: int, dpi=96) -> float:
    return px / float(dpi)

async def build_docx_with_captions(
    files: List[UploadFile], hotel: str, year: int, month: int, every_n: int, username: str
) -> Tuple[str, int, int, float, float]:
    """
    Docx dosyasını oluşturur.
    Döner: (file_path, total_in_tokens, total_out_tokens, cost_usd, cost_try)
    """
    doc = Document()
    section = doc.sections[0]
    # kenar boşlukları
    for s in doc.sections:
        s.top_margin = Inches(0.8)
        s.bottom_margin = Inches(0.8)
        s.left_margin = Inches(0.8)
        s.right_margin = Inches(0.8)

    # Kapak
    title = doc.add_heading(level=0)
    run = title.add_run(f"{hotel} • {calendar.month_name[month]} {year} Sosyal Medya Planı")
    run.font.size = Pt(20)
    run.bold = True
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    doc.add_paragraph("")  # boş satır

    dates = make_dates_for_month(year, month, every_n, len(files))

    total_in = 0
    total_out = 0

    async with httpx.AsyncClient() as client:
        for idx, up in enumerate(files):
            img_bytes = await up.read()
            mime = up.content_type or "image/jpeg"

            # Tarih başlığı
            dt = dates[idx]
            p = doc.add_paragraph()
            r = p.add_run(dt.strftime("%d.%m.%Y"))
            r.bold = True
            r.font.size = Pt(12)

            # Görseli ekle (genişliğe oturt)
            try:
                im = Image.open(io.BytesIO(img_bytes))
                im_format = im.format or "JPEG"
                # Resize: sayfa genişliğine ~6.0 inç
                target_width_in = 6.0
                dpi = 96
                width_px = int(target_width_in * dpi)
                if im.width > width_px:
                    ratio = width_px / float(im.width)
                    new_size = (width_px, int(im.height * ratio))
                    im = im.resize(new_size, Image.LANCZOS)
                buff = io.BytesIO()
                im.save(buff, format=im_format)
                buff.seek(0)
                doc.add_picture(buff, width=Inches(target_width_in))
            except Exception:
                # Hata olursa orijinali koymayı dene
                doc.add_picture(io.BytesIO(img_bytes), width=Inches(6.0))

            # İçerik + hashtag (Gemini)
            caption, hashtags, in_tok, out_tok = await gemini_caption_and_tags(client, img_bytes, mime, hotel)
            total_in += in_tok
            total_out += out_tok

            doc.add_paragraph(caption)
            if hashtags:
                doc.add_paragraph(" ".join(hashtags))

            doc.add_paragraph("")  # ayraç

    usd = token_cost_usd(total_in, total_out)
    tl = round(usd * USD_TRY_RATE, 4)

    # Footer maliyet özeti
    doc.add_page_break()
    doc.add_heading("Maliyet Özeti", level=1)
    tbl = doc.add_table(rows=5, cols=2)
    rows = [
        ("Girdi Token", str(total_in)),
        ("Çıktı Token", str(total_out)),
        ("USD", f"${usd:.6f}"),
        ("Kur (USD→TRY)", f"{USD_TRY_RATE}"),
        ("TRY", f"{tl:.4f} ₺"),
    ]
    for i, (k, v) in enumerate(rows):
        tbl.cell(i, 0).text = k
        tbl.cell(i, 1).text = v

    # Kaydet
    safe_hotel = "".join(c for c in hotel if c.isalnum() or c in (" ", "-", "_")).strip().replace(" ", "_")
    fname = f"{safe_hotel}_{year}-{str(month).zfill(2)}_{uuid.uuid4().hex[:6]}.docx"
    fpath = os.path.join(GENERATED_DIR, fname)
    doc.save(fpath)
    return fpath, total_in, total_out, usd, tl

def log_generation(user_id: int, username: str, role: str, hotel: str, month: int, year: int,
                   images_count: int, file_name: str, usd: float, tl: float, tin: int, tout: int):
    conn = get_db()
    try:
        conn.execute("""
            INSERT INTO logs (user_id, username, role, hotel, month, year, images_count, file_name, cost_usd, cost_try, token_in, token_out)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (user_id, username, role, hotel, month, year, images_count, file_name, usd, tl, tin, tout))
        conn.commit()
    finally:
        conn.close()

# --- Startup ---
def _startup():
    ensure_db()
    seed_admin_from_code()

@app.on_event("startup")
def on_startup():
    _startup()

# --- Routes ---
@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    if not request.session.get("uid"):
        return RedirectResponse(url="/login?next=/", status_code=303)
    return RedirectResponse(url="/dashboard", status_code=303)

@app.get("/login", response_class=HTMLResponse)
def login_get(request: Request, next: Optional[str] = "/"):
    return templates.TemplateResponse("login.html", {
        "request": request, "app_name": APP_NAME, "next": next, "csrf_token": get_csrf_token(request)
    })

@app.post("/login", response_class=HTMLResponse)
def login_post(request: Request, username: str = Form(...), password: str = Form(...), csrf_token: str = Form(...), next: Optional[str] = "/"):
    require_csrf(request, csrf_token)
    row = get_user_by_username(username)
    if not row:
        return templates.TemplateResponse("login.html", {"request": request, "app_name": APP_NAME, "next": next, "error": "Kullanıcı bulunamadı", "csrf_token": get_csrf_token(request)})
    if not bcrypt.checkpw(password.encode("utf-8"), row["pw_hash"].encode("utf-8")):
        return templates.TemplateResponse("login.html", {"request": request, "app_name": APP_NAME, "next": next, "error": "Şifre hatalı", "csrf_token": get_csrf_token(request)})

    request.session["uid"] = row["id"]
    request.session["username"] = row["username"]
    request.session["role"] = row["role"]
    return RedirectResponse(url=next or "/dashboard", status_code=303)

@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login?next=/", status_code=303)

@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request):
    need = require_login(request)
    if need:
        return need
    uid = request.session["uid"]
    role = request.session["role"]
    conn = get_db()
    try:
        if role == "admin":
            cur = conn.execute("SELECT * FROM logs ORDER BY id DESC LIMIT 20")
        else:
            cur = conn.execute("SELECT * FROM logs WHERE user_id=? ORDER BY id DESC LIMIT 20", (uid,))
        logs = cur.fetchall()
    finally:
        conn.close()
    today = datetime.now(TZ).date()
    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "app_name": APP_NAME,
        "csrf_token": get_csrf_token(request),
        "today": today.isoformat(),
        "username": request.session.get("username"),
        "role": request.session.get("role"),
        "logs": logs
    })

@app.post("/generate")
async def generate(
    request: Request,
    hotel: str = Form(...),
    year: int = Form(...),
    month: int = Form(...),
    every_n_days: int = Form(1),
    files: List[UploadFile] = Form(...),
    csrf_token: str = Form(...)
):
    need = require_login(request)
    if need:
        return need
    require_csrf(request, csrf_token)
    if not files:
        return JSONResponse({"error": "En az 1 görsel yükleyin."}, status_code=400)
    # Docx oluştur
    fpath, tin, tout, usd, tl = await build_docx_with_captions(files, hotel.strip(), year, month, every_n_days, request.session["username"])
    # Log
    log_generation(
        user_id=request.session["uid"],
        username=request.session["username"],
        role=request.session["role"],
        hotel=hotel.strip(), month=month, year=year, images_count=len(files),
        file_name=os.path.basename(fpath), usd=usd, tl=tl, tin=tin, tout=tout
    )
    return JSONResponse({
        "ok": True,
        "file": f"/download/{os.path.basename(fpath)}",
        "usd": usd, "try_": tl,
        "token_in": tin, "token_out": tout
    })

@app.get("/download/{name}")
def download(request: Request, name: str):
    need = require_login(request)
    if need:
        return need
    fpath = os.path.join(GENERATED_DIR, name)
    if not os.path.isfile(fpath):
        raise HTTPException(status_code=404, detail="Dosya yok")
    return FileResponse(fpath, filename=name, media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")

@app.get("/plan_dates")
def plan_dates_api(request: Request, year: int, month: int, every: int, count: int):
    need = require_login(request)
    if need:
        return need
    ds = make_dates_for_month(year, month, every, count)
    return {"dates": [d.strftime("%d.%m.%Y") for d in ds]}

# --- Logs export (xlsx) ---
@app.get("/logs/export")
def export_logs(request: Request):
    need = require_login(request)
    if need:
        return need
    role = request.session["role"]
    uid = request.session["uid"]
    conn = get_db()
    try:
        if role == "admin":
            cur = conn.execute("SELECT * FROM logs ORDER BY id DESC")
        else:
            cur = conn.execute("SELECT * FROM logs WHERE user_id=? ORDER BY id DESC", (uid,))
        rows = cur.fetchall()
    finally:
        conn.close()
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "logs"
    headers = ["id","user_id","username","role","hotel","month","year","images_count","file_name","cost_usd","cost_try","token_in","token_out","created_at"]
    ws.append(headers)
    for r in rows:
        ws.append([r[h] for h in headers])
    out = io.BytesIO()
    wb.save(out)
    out.seek(0)
    fname = f"logs_{role}_{datetime.now(TZ).strftime('%Y%m%d_%H%M%S')}.xlsx"
    return FileResponse(out, filename=fname, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

# --- User management (admin only) ---
@app.get("/users", response_class=HTMLResponse)
def users_page(request: Request):
    need = require_admin(request)
    if need:
        return need
    conn = get_db()
    try:
        cur = conn.execute("SELECT id, username, role, created_at FROM users ORDER BY id DESC")
        users = cur.fetchall()
    finally:
        conn.close()
    return templates.TemplateResponse("manage_users.html", {
        "request": request, "app_name": APP_NAME, "users": users, "csrf_token": get_csrf_token(request)
    })

@app.post("/users/create")
def users_create(request: Request, username: str = Form(...), password: str = Form(...), role: str = Form(...), csrf_token: str = Form(...)):
    need = require_admin(request)
    if need:
        return need
    require_csrf(request, csrf_token)
    role = role if role in ("admin","staff") else "staff"
    create_user_if_missing(username.strip(), password, role)
    return RedirectResponse(url="/users", status_code=303)

@app.post("/users/delete")
def users_delete(request: Request, user_id: int = Form(...), csrf_token: str = Form(...)):
    need = require_admin(request)
    if need:
        return need
    require_csrf(request, csrf_token)
    # Admin kendini silemesin
    if user_id == request.session["uid"]:
        raise HTTPException(status_code=400, detail="Kendi hesabını silemezsin.")
    conn = get_db()
    try:
        conn.execute("DELETE FROM users WHERE id=?", (user_id,))
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse(url="/users", status_code=303)
