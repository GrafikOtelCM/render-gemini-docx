import io, os, re, json, base64, datetime, sqlite3, csv, traceback, calendar, hashlib, asyncio, secrets, time
from typing import List, Tuple, Optional
from pathlib import Path

import httpx
from fastapi import FastAPI, Request, UploadFile
from fastapi.responses import HTMLResponse, StreamingResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import TemplateNotFound
from starlette.concurrency import run_in_threadpool
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.base import BaseHTTPMiddleware

from PIL import Image
from docx import Document
from docx.shared import Inches
import bcrypt
from openpyxl import Workbook

# ====== TZ (Europe/Istanbul) ======
try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("Europe/Istanbul")
except Exception:
    TZ = None

# ====== Yol ayarları ======
BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

# ========= Uygulama Ayarları =========
APP_NAME = "Plan Otomasyon – Gemini to DOCX"
MAX_IMAGES = 10
MAX_EDGE = 1280
IMG_JPEG_QUALITY = 80

DEFAULT_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "AIzaSyDdUV3SuQ1bbhqILvR_70wGRSMdDGkOoNI")
SECRET_KEY = os.getenv("SECRET_KEY", "kJx2Jesr0Aq_R4oSdfaYBMKu4FDlWkslyZ_DH-nCxUzMaiOEO9Jl8XuRrSXzoF8del4cUjQ-lZxOFrmP4jYmXQ")

# Ücretler (USD / 1M token)
RATE_IN_PER_MTOK  = float(os.getenv("RATE_IN_PER_MTOK",  "0.30"))
RATE_OUT_PER_MTOK = float(os.getenv("RATE_OUT_PER_MTOK", "2.50"))
USD_TRY_RATE = float(os.getenv("USD_TRY_RATE", "41.2"))

# usageMetadata yoksa varsayımlar (görsel başına)
ASSUME_IN_TOKENS  = int(os.getenv("ASSUME_IN_TOKENS",  "400"))
ASSUME_OUT_TOKENS = int(os.getenv("ASSUME_OUT_TOKENS", "80"))

# Paralellik kontrolü
GEMINI_MAX_CONCURRENCY = int(os.getenv("GEMINI_MAX_CONCURRENCY", "4"))
GEMINI_SEM = asyncio.Semaphore(GEMINI_MAX_CONCURRENCY)

# Basit oran sınırlama
RATE_LIMIT_WINDOW = 60
RATE_LIMIT_MAX = int(os.getenv("RATE_LIMIT_MAX", "12"))

BANNED_WORDS_RE = re.compile(r"(kaçış|kaçamak|kraliyet)", re.IGNORECASE)
DB_PATH = os.getenv("USAGE_DB_PATH", "usage.db")

# ========= Admin'i koddan belirle (burayı kendine göre düzenle) =========
ADMIN_CODE_USER = "otelgrafikadmin"        # <-- KENDİ ADMIN ADIN
ADMIN_CODE_PASS = "otelgrafikpass"  # <-- KENDİ ADMIN PAROLAN
# Var olan admin’i her açılışta BU parola ile güncellemek ister misin?
ADMIN_FORCE_SYNC = False  # True yaparsan mevcut admin parolası da bu değere set edilir.

# ========= FastAPI & Middleware =========
app = FastAPI(title=APP_NAME)
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, max_age=60*60*8)  # 8 saat oturum

class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        resp = await call_next(request)
        resp.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        resp.headers["Permissions-Policy"] = "camera=(), geolocation=(), microphone=()"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "img-src 'self' data: blob:; "
            "style-src 'self' 'unsafe-inline'; "
            "script-src 'self'; "
            "connect-src 'self'; "
            "frame-ancestors 'none'; "
            "base-uri 'none'; form-action 'self'"
        )
        return resp
app.add_middleware(SecurityHeadersMiddleware)

# Statik/Template mount
if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# (İsteğe bağlı) İlk deploy’da yoksa basit şablonları oluştur
def ensure_default_templates():
    TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
    (STATIC_DIR).mkdir(parents=True, exist_ok=True)

    login_html = TEMPLATES_DIR / "login.html"
    index_html = TEMPLATES_DIR / "index.html"
    dashboard_html = TEMPLATES_DIR / "dashboard.html"
    admin_users_html = TEMPLATES_DIR / "admin_users.html"

    if not login_html.exists():
        login_html.write_text("""<!doctype html>
<html lang="tr">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="stylesheet" href="/static/styles.css">
<title>Giriş</title>
</head>
<body class="container">
  <div class="card">
    <h1>Giriş</h1>
    {% if error %}<div class="alert">{{ error }}</div>{% endif %}
    <form method="post" action="/login">
      <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
      <input type="hidden" name="next" value="{{ next }}">
      <label>Kullanıcı Adı</label>
      <input name="username" required>
      <label>Parola</label>
      <input name="password" type="password" required>
      <button type="submit" class="btn">Giriş Yap</button>
    </form>
  </div>
</body>
</html>""", encoding="utf-8")

    if not index_html.exists():
        index_html.write_text("""<!doctype html>
<html lang="tr">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="stylesheet" href="/static/styles.css">
<title>Plan Oluştur</title>
</head>
<body class="container">
  <header class="topbar">
    <div class="brand">{{ app_name }}</div>
    <nav>
      <a href="/dashboard">Dashboard</a>
      {% if user and user.role == 'admin' %} • <a href="/admin/users">Kullanıcılar</a>{% endif %}
      • <a href="/logout">Çıkış</a>
    </nav>
  </header>

  <div class="card">
    <h1>DOCX Plan Oluştur</h1>
    <form id="genForm" method="post" action="/generate" enctype="multipart/form-data">
      <input type="hidden" name="csrf_token" value="{{ csrf_token }}">

      <div class="grid">
        <div>
          <label>Doküman Adı</label>
          <input name="doc_name" value="{{ suggested_name }}" required>
        </div>
        <div>
          <label>Proje/Otel Etiketi</label>
          <input name="project_tag" placeholder="Martı Prime, vb.">
        </div>
      </div>

      <div class="grid">
        <div>
          <label>Plan Ayı</label>
          <input type="month" name="plan_month" value="{{ default_month }}" required>
        </div>
        <div>
          <label>Kaç günde bir paylaşım?</label>
          <input type="number" name="interval_days" min="1" value="1" required>
        </div>
      </div>

      <label>İletişim Bilgisi (caption altına eklenecek)</label>
      <textarea name="contact_info" rows="3" placeholder="Tel, web, konum..."></textarea>

      <label>Görseller (en fazla 10)</label>
      <input type="file" name="files" accept="image/*" multiple required>
      <p class="hint">Seçtiğiniz sırayla tarihlere yerleştirilecektir (1–29 arası).</p>

      <button class="btn primary" type="submit">Oluştur & İndir</button>
    </form>
  </div>
</body>
</html>""", encoding="utf-8")

    if not dashboard_html.exists():
        dashboard_html.write_text("""<!doctype html>
<html lang="tr">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="stylesheet" href="/static/styles.css">
<title>Dashboard</title>
</head>
<body class="container">
  <header class="topbar">
    <div class="brand">Dashboard</div>
    <nav>
      <a href="/">Plan Oluştur</a>
      {% if user and user.role == 'admin' %} • <a href="/admin/users">Kullanıcılar</a>{% endif %}
      • <a href="/logout">Çıkış</a>
    </nav>
  </header>

  <div class="card">
    <h2>Özet (Bu Ay)</h2>
    <div class="stats">
      <div class="stat"><div class="k">{{ summary.runs }}</div><div class="l">Çalıştırma</div></div>
      <div class="stat"><div class="k">{{ summary.images }}</div><div class="l">Görsel</div></div>
      <div class="stat"><div class="k">{{ summary.in_tokens }}</div><div class="l">Girdi Token</div></div>
      <div class="stat"><div class="k">{{ summary.out_tokens }}</div><div class="l">Çıktı Token</div></div>
      <div class="stat"><div class="k">${{ '%.4f'|format(summary.usd) }}</div><div class="l">USD</div></div>
      <div class="stat"><div class="k">{{ '%.2f'|format(summary.try) }} ₺</div><div class="l">TRY</div></div>
    </div>
  </div>

  <div class="card">
    <h2>Son 50 İşlem</h2>
    <div class="table-wrap">
      <table>
        <thead>
          <tr><th>Tarih</th><th>Doküman</th><th>Proje</th><th>Ay</th><th>Görsel</th><th>InTok</th><th>OutTok</th><th>USD</th><th>TRY</th></tr>
        </thead>
        <tbody>
          {% for r in rows %}
          <tr>
            <td>{{ r[0] }}</td>
            <td>{{ r[1] }}</td>
            <td>{{ r[2] or '-' }}</td>
            <td>{{ r[3] or '-' }}</td>
            <td>{{ r[4] }}</td>
            <td>{{ r[5] }}</td>
            <td>{{ r[6] }}</td>
            <td>${{ '%.4f'|format(r[7]) }}</td>
            <td>{{ '%.2f'|format(r[8]) }} ₺</td>
          </tr>
          {% endfor %}
        </tbody>
      </table>
    </div>
  </div>
</body>
</html>""", encoding="utf-8")

    if not admin_users_html.exists():
        admin_users_html.write_text("""<!doctype html>
<html lang="tr">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="stylesheet" href="/static/styles.css">
<title>Kullanıcı Yönetimi</title>
</head>
<body class="container">
  <header class="topbar">
    <div class="brand">Kullanıcı Yönetimi</div>
    <nav>
      <a href="/">Plan Oluştur</a> • <a href="/dashboard">Dashboard</a> • <a href="/logout">Çıkış</a>
    </nav>
  </header>

  {% if flash %}<div class="card"><div class="alert">{{ flash }}</div></div>{% endif %}

  <div class="card">
    <h2>Yeni Kullanıcı Oluştur</h2>
    <form method="post" action="/admin/users/create">
      <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
      <div class="grid">
        <div>
          <label>Kullanıcı Adı</label>
          <input name="username" required>
        </div>
        <div>
          <label>Parola</label>
          <input name="password" required type="password">
        </div>
      </div>
      <div class="grid">
        <div>
          <label>Rol</label>
          <select name="role" required>
            <option value="staff">staff</option>
            <option value="admin">admin</option>
          </select>
        </div>
      </div>
      <button class="btn primary" type="submit">Oluştur</button>
    </form>
  </div>

  <div class="card">
    <h2>Mevcut Kullanıcılar</h2>
    <div class="table-wrap">
      <table>
        <thead><tr><th>ID</th><th>Kullanıcı</th><th>Rol</th><th>İşlemler</th></tr></thead>
        <tbody>
          {% for u in users %}
          <tr>
            <td>{{ u.id }}</td>
            <td>{{ u.username }}</td>
            <td>{{ u.role }}</td>
            <td>
              <form method="post" action="/admin/users/reset_password" style="display:inline">
                <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
                <input type="hidden" name="user_id" value="{{ u.id }}">
                <input type="password" name="new_password" placeholder="Yeni parola" required>
                <button class="btn">Parola Sıfırla</button>
              </form>
              <form method="post" action="/admin/users/delete" style="display:inline" onsubmit="return confirm('Silinsin mi?');">
                <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
                <input type="hidden" name="user_id" value="{{ u.id }}">
                <button class="btn danger" {% if u.id == me.id %}disabled title="Kendini silemezsin"{% endif %}>Sil</button>
              </form>
            </td>
          </tr>
          {% endfor %}
        </tbody>
      </table>
    </div>
    <p class="hint">Son admin’i silmeye izin verilmez. Admin sayısı ≥ 1 kalmalı.</p>
  </div>
</body>
</html>""", encoding="utf-8")

# ========= DB Yardımcıları =========
def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None, timeout=30)
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn

def ensure_db():
    for attempt in range(10):
        try:
            conn = get_db()
            cur = conn.cursor()
            try:
                cur.execute("PRAGMA journal_mode=WAL;")
            except sqlite3.OperationalError:
                pass
            cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              username TEXT UNIQUE NOT NULL,
              pw_hash TEXT NOT NULL,
              role TEXT NOT NULL CHECK(role IN ('admin','staff'))
            );""")
            cur.execute("""
            CREATE TABLE IF NOT EXISTS usage (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              ts TEXT NOT NULL,
              user_id INTEGER,
              user_key TEXT,
              model TEXT NOT NULL,
              doc_name TEXT NOT NULL,
              project_tag TEXT,
              plan_month TEXT,
              images INTEGER NOT NULL,
              in_tokens INTEGER NOT NULL,
              out_tokens INTEGER NOT NULL,
              cost_usd REAL NOT NULL,
              cost_try REAL NOT NULL,
              FOREIGN KEY(user_id) REFERENCES users(id)
            );""")
            cur.execute("""
            CREATE TABLE IF NOT EXISTS caption_cache (
              hash TEXT PRIMARY KEY,
              caption TEXT NOT NULL,
              tags_json TEXT NOT NULL,
              in_tokens INTEGER NOT NULL,
              out_tokens INTEGER NOT NULL,
              created_at TEXT NOT NULL
            );""")
            conn.commit(); conn.close()
            return
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower():
                time.sleep(0.5); continue
            else:
                raise
    raise RuntimeError("SQLite başlangıcında kilit sorunu: tek worker ile çalıştırın veya kalıcı DB kullanın.")

def create_user_if_missing(username: str, password: str, role: str):
    if not username or not password: return
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT id FROM users WHERE username=?", (username,))
    row = cur.fetchone()
    pw_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    if not row:
        cur.execute("INSERT INTO users (username, pw_hash, role) VALUES (?, ?, ?)", (username, pw_hash, role))
    else:
        if ADMIN_FORCE_SYNC and role == "admin" and username == ADMIN_CODE_USER:
            cur.execute("UPDATE users SET pw_hash=?, role=? WHERE username=?", (pw_hash, role, username))
    conn.commit(); conn.close()

def find_user(username: str) -> Optional[dict]:
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT id, username, pw_hash, role FROM users WHERE username=?", (username,))
    row = cur.fetchone(); conn.close()
    if not row: return None
    return {"id": row[0], "username": row[1], "pw_hash": row[2], "role": row[3]}

def get_user_by_id(uid: int) -> Optional[dict]:
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT id, username, role FROM users WHERE id=?", (uid,))
    r = cur.fetchone(); conn.close()
    return {"id": r[0], "username": r[1], "role": r[2]} if r else None

def list_users() -> List[dict]:
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT id, username, role FROM users ORDER BY role DESC, username ASC")
    rows = cur.fetchall(); conn.close()
    return [{"id": r[0], "username": r[1], "role": r[2]} for r in rows]

def count_admins() -> int:
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM users WHERE role='admin'")
    n = cur.fetchone()[0]; conn.close(); return int(n)

def delete_user(uid: int):
    conn = get_db(); cur = conn.cursor()
    cur.execute("DELETE FROM users WHERE id=?", (uid,))
    conn.commit(); conn.close()

def update_user_password(uid: int, new_password: str):
    pw_hash = bcrypt.hashpw(new_password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    conn = get_db(); cur = conn.cursor()
    cur.execute("UPDATE users SET pw_hash=? WHERE id=?", (pw_hash, uid))
    conn.commit(); conn.close()

def log_usage(ts: str, user_id: Optional[int], user_key: str, model: str, doc_name: str,
              project_tag: str, plan_month: str, images: int,
              in_tok: int, out_tok: int, cost_usd: float, cost_try: float):
    conn = get_db(); cur = conn.cursor()
    cur.execute("""
      INSERT INTO usage (ts, user_id, user_key, model, doc_name, project_tag, plan_month,
                         images, in_tokens, out_tokens, cost_usd, cost_try)
      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (ts, user_id, user_key, model, doc_name, project_tag, plan_month,
          images, in_tok, out_tok, cost_usd, cost_try))
    conn.commit(); conn.close()

def cache_get(h: str):
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT caption, tags_json, in_tokens, out_tokens FROM caption_cache WHERE hash=?", (h,))
    row = cur.fetchone(); conn.close()
    if not row: return None
    caption, tags_json, in_t, out_t = row
    return caption, json.loads(tags_json), int(in_t), int(out_t)

def cache_put(h: str, caption: str, tags: List[str], in_t: int, out_t: int):
    conn = get_db(); cur = conn.cursor()
    cur.execute("""
      INSERT OR REPLACE INTO caption_cache (hash, caption, tags_json, in_tokens, out_tokens, created_at)
      VALUES (?, ?, ?, ?, ?, ?)
    """, (h, caption, json.dumps(tags, ensure_ascii=False), int(in_t), int(out_t),
          (datetime.datetime.now(TZ) if TZ else datetime.datetime.now()).isoformat(timespec="seconds")))
    conn.commit(); conn.close()

# ========= CSRF =========
def get_csrf_token(request: Request) -> str:
    tok = request.session.get("csrf")
    if not tok:
        tok = secrets.token_urlsafe(32)
        request.session["csrf"] = tok
    return tok

def check_csrf(request: Request, form) -> bool:
    sess = request.session.get("csrf")
    posted = form.get("csrf_token")
    return bool(sess and posted and sess == posted)

# ========= Takvim yardımcıları =========
MONTHS_TR = ["Ocak","Şubat","Mart","Nisan","Mayıs","Haziran","Temmuz","Ağustos","Eylül","Ekim","Kasım","Aralık"]
WEEKDAYS_TR = ["Pazartesi","Salı","Çarşamba","Perşembe","Cuma","Cumartesi","Pazar"]  # Monday=0

def format_date_tr(d: datetime.date) -> str:
    wd = WEEKDAYS_TR[d.weekday()]
    return f"{d.day:02d} {MONTHS_TR[d.month-1]} {d.year} {wd}"

def generate_schedule(year: int, month: int, every_n_days: int) -> List[datetime.date]:
    last_day = calendar.monthrange(year, month)[1]
    cutoff = min(29, last_day)
    if every_n_days < 1: every_n_days = 1
    dates = []; day = 1
    while day <= cutoff:
        dates.append(datetime.date(year, month, day))
        day += every_n_days
    return dates

# ========= JSON ayıklayıcı =========
import re as _re
def _extract_json_maybe(text: str):
    if not text: return None
    m = _re.search(r"```(?:json)?\s*({[\s\S]*?})\s*```", text, _re.IGNORECASE)
    if m:
        try: return json.loads(m.group(1))
        except Exception: pass
    m = _re.search(r"({[\s\S]*})", text)
    if m:
        raw = m.group(1)
        open_cnt = raw.count("{"); close_cnt = raw.count("}")
        if close_cnt < open_cnt: raw = raw + ("}" * (open_cnt - close_cnt))
        try: return json.loads(raw)
        except Exception: pass
    return None

# ========= Görsel yardımcıları =========
def jpeg_preview_bytes(raw_bytes: bytes) -> bytes:
    img = Image.open(io.BytesIO(raw_bytes)).convert("RGB")
    w, h = img.size
    scale = min(MAX_EDGE / max(w, h), 1.0)
    if scale < 1.0:
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=IMG_JPEG_QUALITY, optimize=True, progressive=True)
    out.seek(0)
    return out.read()

async def make_preview_bytes(upload: UploadFile) -> Tuple[bytes, str, str]:
    raw = await upload.read()
    await run_in_threadpool(lambda: None)
    jpeg_bytes = await run_in_threadpool(jpeg_preview_bytes, raw)
    fname = os.path.splitext(upload.filename or f"img_{id(upload)}")[0]
    h = hashlib.sha256(jpeg_bytes).hexdigest()
    return jpeg_bytes, fname, h

# ========= Gemini çağrısı (ASYNC) =========
async def call_gemini_for_caption_and_tags(jpeg_bytes: bytes) -> Tuple[str, List[str], int, int]:
    if not GEMINI_API_KEY or GEMINI_API_KEY == "YOUR_API_KEY_HERE":
        raise RuntimeError("GEMINI_API_KEY tanımlı değil. Render Environment'a ekleyin.")

    base64_img = base64.b64encode(jpeg_bytes).decode("utf-8")
    prompt = (
        "Yalnızca JSON üret. Şema: {\"caption\": string, \"hashtags\": [string, string, string]}.\n"
        "Kurallar: Türkçe, 1-2 cümle kısa pazarlama açıklaması; emoji makul; marka/özel isim verme. "
        "Tam 3 hashtag üret. Şu kelimeleri asla kullanma: kaçış, kaçamak, kraliyet."
    )
    payload = {
        "contents": [{
            "role": "user",
            "parts": [{"text": prompt},
                      {"inlineData": {"mimeType": "image/jpeg", "data": base64_img}}]
        }],
        "generationConfig": {"response_mime_type": "application/json"}
    }
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{DEFAULT_MODEL}:generateContent?key={GEMINI_API_KEY}"

    async with GEMINI_SEM:
        async with httpx.AsyncClient(timeout=60) as client:
            res = await client.post(url, json=payload)

    if res.status_code != 200:
        msg = res.text.strip()[:500]
        raise RuntimeError(f"Gemini API hata {res.status_code}: {msg}")

    data = res.json()
    in_tok, out_tok = ASSUME_IN_TOKENS, ASSUME_OUT_TOKENS
    try:
        usage = data.get("usageMetadata") or (data.get("candidates", [{}])[0].get("usageMetadata") or {})
        in_tok  = int(usage.get("promptTokenCount", in_tok))
        out_tok = int(usage.get("candidatesTokenCount", usage.get("outputTokenCount", out_tok)))
    except Exception:
        pass

    parts = (data.get("candidates") or [{}])[0].get("content", {}).get("parts", [])
    text = "".join(p.get("text", "") for p in parts).strip()

    try:
        obj = json.loads(text)
    except Exception:
        obj = _extract_json_maybe(text)
    if not isinstance(obj, dict):
        raise RuntimeError(f"Gemini beklenen JSON'u döndürmedi. Ham yanıtın başı: {text[:240]}")

    caption = str(obj.get("caption", "")).strip()
    hashtags = obj.get("hashtags", [])
    caption = re.sub(r"\s+", " ", caption)
    caption = BANNED_WORDS_RE.sub("", caption)
    caption = re.sub(r"https?://\S+", "", caption).strip()

    tags = []
    for t in (hashtags or []):
        t = str(t).strip()
        if not t: continue
        if not t.startswith("#"): t = "#" + t.lstrip("#")
        if BANNED_WORDS_RE.search(t): continue
        if t not in tags: tags.append(t)
        if len(tags) == 3: break

    if not caption or len(tags) != 3:
        raise RuntimeError(f"Gemini JSON eksik/boş döndü. caption='{caption}', tags={tags}")

    return caption, tags, in_tok, out_tok

# ========= DOCX yazımı =========
def _build_docx_bytes(doc_name: str, contact_info: str,
                      images: List[Tuple[str, bytes, str]],
                      plan_dates: List[datetime.date],
                      captions: List[Tuple[str, List[str], int, int]]) -> bytes:
    document = Document()
    section = document.sections[0]
    section.top_margin = section.bottom_margin = section.left_margin = section.right_margin = Inches(0.5)

    for idx, (stub, jpeg_bytes, _) in enumerate(images):
        date_str = format_date_tr(plan_dates[idx]) if idx < len(plan_dates) else "Tarih plan dışı"
        p = document.add_paragraph(); r = p.add_run(f"Paylaşım Tarihi: {date_str}"); r.bold = True

        pic_stream = io.BytesIO(jpeg_bytes)
        try:
            document.add_picture(pic_stream, width=Inches(6))
        except Exception:
            im = Image.open(io.BytesIO(jpeg_bytes)).convert("RGB")
            buf = io.BytesIO(); im.save(buf, format="JPEG", quality=IMG_JPEG_QUALITY); buf.seek(0)
            document.add_picture(buf, width=Inches(6))

        cap, tags, _, _ = captions[idx]
        document.add_paragraph(cap)
        document.add_paragraph(contact_info)
        document.add_paragraph(" ".join(tags))

        if idx != len(images) - 1:
            document.add_page_break()

    out = io.BytesIO(); document.save(out); out.seek(0)
    return out.read()

async def build_docx_and_collect_usage(doc_name: str, contact_info: str,
                                       images: List[Tuple[str, bytes, str]],
                                       plan_dates: List[datetime.date]) -> Tuple[bytes, int, int, List[Tuple[str,List[str],int,int]]]:
    total_in = total_out = 0
    results: List[Tuple[str, List[str], int, int]] = [None] * len(images)

    async def process_one(i: int):
        nonlocal total_in, total_out
        _, jpeg_bytes, h = images[i]
        cached = cache_get(h)
        if cached:
            cap, tags, in_t, out_t = cached
        else:
            cap, tags, in_t, out_t = await call_gemini_for_caption_and_tags(jpeg_bytes)
            cache_put(h, cap, tags, in_t, out_t)
        results[i] = (cap, tags, in_t, out_t)

    await asyncio.gather(*(process_one(i) for i in range(len(images))))

    for _, _, in_t, out_t in results:
        total_in += in_t; total_out += out_t

    content = await run_in_threadpool(_build_docx_bytes, doc_name, contact_info, images, plan_dates, results)
    return content, total_in, total_out, results

# ========= Rate limit =========
from collections import defaultdict, deque
_rate_state = defaultdict(deque)

class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        if request.url.path == "/generate":
            ip = request.client.host if request.client else "unknown"
            now = datetime.datetime.now().timestamp()
            q = _rate_state[ip]
            while q and now - q[0] > RATE_LIMIT_WINDOW:
                q.popleft()
            if len(q) >= RATE_LIMIT_MAX:
                return PlainTextResponse("Çok fazla istek. Lütfen birazdan tekrar deneyin.", status_code=429)
            q.append(now)
        return await call_next(request)
app.add_middleware(RateLimitMiddleware)

# ========= Auth yardımcıları =========
def current_user(request: Request) -> Optional[dict]:
    u = request.session.get("user")
    return u if u else None

def require_login(request: Request) -> Optional[RedirectResponse]:
    if not current_user(request):
        return RedirectResponse(url="/login?next=" + request.url.path, status_code=303)
    return None

def require_admin(request: Request) -> Optional[RedirectResponse]:
    u = current_user(request)
    if not u: return RedirectResponse(url="/login?next=" + request.url.path, status_code=303)
    if u.get("role") != "admin":
        return RedirectResponse(url="/", status_code=303)
    return None

# ========= Startup =========
def seed_admin_from_code():
    # Koddan belirlediğin admini oluştur (yoksa); ADMIN_FORCE_SYNC True ise parolasını her açılışta senkronlar
    if not ADMIN_CODE_USER or not ADMIN_CODE_PASS:
        return
    create_user_if_missing(ADMIN_CODE_USER, ADMIN_CODE_PASS, "admin")

@app.on_event("startup")
def _startup():
    ensure_default_templates()
    ensure_db()
    seed_admin_from_code()

@app.get("/healthz")
def healthz():
    return {"ok": True}

@app.head("/")
def index_head():
    return PlainTextResponse("", status_code=200)

# ========= Login / Logout =========
@app.get("/login", response_class=HTMLResponse)
def login_get(request: Request, next: str = "/"):
    return templates.TemplateResponse("login.html", {"request": request, "app_name": APP_NAME, "next": next, "csrf_token": get_csrf_token(request)})

@app.post("/login")
async def login_post(request: Request):
    form = await request.form()
    if not check_csrf(request, form):
        return PlainTextResponse("CSRF doğrulaması başarısız", status_code=403)
    username = (form.get("username") or "").strip()
    password = (form.get("password") or "").strip()
    next_url = form.get("next") or "/"
    user = find_user(username)
    if not user or not bcrypt.checkpw(password.encode("utf-8"), user["pw_hash"].encode("utf-8")):
        return templates.TemplateResponse("login.html",
            {"request": request, "app_name": APP_NAME, "error": "Geçersiz kullanıcı veya parola", "next": next_url, "csrf_token": get_csrf_token(request)},
            status_code=401)
    request.session["user"] = {"id": user["id"], "username": user["username"], "role": user["role"]}
    return RedirectResponse(url=next_url, status_code=303)

@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)

# ========= Sayfalar =========
@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    need = require_login(request)
    if need: return need
    now = datetime.datetime.now(TZ) if TZ else datetime.datetime.now()
    suggested = now.strftime("%Y%m%d-%H%M")
    default_month = now.strftime("%Y-%m")
    try:
        return templates.TemplateResponse(
            "index.html",
            {"request": request, "app_name": APP_NAME,
             "suggested_name": f"Instagram_Plani_{suggested}",
             "default_month": default_month,
             "user": current_user(request),
             "csrf_token": get_csrf_token(request)}
        )
    except TemplateNotFound:
        return HTMLResponse(f"<h1>{APP_NAME}</h1><p>templates/index.html bulunamadı.</p>", status_code=200)

# ========= Month bounds =========
def month_bounds(dt: datetime.datetime):
    first = dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    nxt = (first.replace(year=first.year+1, month=1) if first.month == 12 else first.replace(month=first.month+1))
    last = nxt - datetime.timedelta(seconds=1)
    return first, last

# ========= Dashboard =========
@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, user_id: str = "me"):
    need = require_login(request)
    if need: return need
    me = current_user(request)

    now = datetime.datetime.now(TZ) if TZ else datetime.datetime.now()
    start, end = month_bounds(now)

    conn = get_db(); cur = conn.cursor()
    params = [start.isoformat(), end.isoformat()]
    where = "WHERE ts >= ? AND ts <= ?"

    if me["role"] == "admin":
        selected_user_id = None
        if user_id not in ("me", "all"):
            try: selected_user_id = int(user_id)
            except: selected_user_id = None
        if selected_user_id is not None:
            where += " AND user_id = ?"; params.append(selected_user_id)
    else:
        where += " AND user_id = ?"; params.append(me["id"])

    cur.execute(f"""
      SELECT COUNT(*), COALESCE(SUM(images),0), COALESCE(SUM(in_tokens),0), COALESCE(SUM(out_tokens),0),
             COALESCE(SUM(cost_usd),0), COALESCE(SUM(cost_try),0)
      FROM usage
      {where}
    """, params)
    cnt, img_sum, in_sum, out_sum, usd_sum, try_sum = cur.fetchone() or (0,0,0,0,0.0,0.0)

    cur.execute(f"""
      SELECT ts, doc_name, project_tag, plan_month, images, in_tokens, out_tokens, cost_usd, cost_try
      FROM usage
      {where}
      ORDER BY ts DESC LIMIT 50
    """, params)
    rows = cur.fetchall()
    conn.close()

    try:
        return templates.TemplateResponse("dashboard.html", {
            "request": request,
            "app_name": APP_NAME,
            "summary": {"runs": cnt, "images": img_sum, "in_tokens": in_sum, "out_tokens": out_sum,
                        "usd": usd_sum, "try": try_sum},
            "rows": rows,
            "user": me,
            "csrf_token": get_csrf_token(request)
        })
    except TemplateNotFound:
        return HTMLResponse("<h1>Dashboard</h1><p>templates/dashboard.html bulunamadı.</p>", status_code=200)

# ========= XLSX export =========
@app.get("/logs.xlsx")
def logs_xlsx(request: Request, user_id: str = "me"):
    need = require_login(request)
    if need: return need
    me = current_user(request)

    now = datetime.datetime.now(TZ) if TZ else datetime.datetime.now()
    start, end = month_bounds(now)

    conn = get_db(); cur = conn.cursor()
    params = [start.isoformat(), end.isoformat()]
    where = "WHERE ts >= ? AND ts <= ?"

    if me["role"] == "admin":
        selected_user_id = None
        if user_id not in ("me", "all"):
            try: selected_user_id = int(user_id)
            except: selected_user_id = None
        if selected_user_id is not None:
            where += " AND user_id = ?"; params.append(selected_user_id)
    else:
        where += " AND user_id = ?"; params.append(me["id"])

    cur.execute(f"""
      SELECT ts, user_id, model, doc_name, project_tag, plan_month, images, in_tokens, out_tokens, cost_usd, cost_try
      FROM usage
      {where}
      ORDER BY ts ASC
    """, params)
    rows = cur.fetchall()
    conn.close()

    wb = Workbook(); ws = wb.active; ws.title = "Logs"
    headers = ["ts","user_id","model","doc_name","project_tag","plan_month","images","in_tokens","out_tokens","cost_usd","cost_try"]
    ws.append(headers)
    for r in rows: ws.append(list(r))

    buf = io.BytesIO(); wb.save(buf); buf.seek(0)
    fname = f"logs_{('all' if me['role']=='admin' and user_id=='all' else 'me')}_{now.strftime('%Y%m')}.xlsx"
    return StreamingResponse(buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                             headers={"Content-Disposition": f'attachment; filename="{fname}"'})

# ========= Admin: Kullanıcı Yönetimi =========
@app.get("/admin/users", response_class=HTMLResponse)
def admin_users_get(request: Request, flash: str = ""):
    need = require_admin(request)
    if need: return need
    users = list_users()
    me = current_user(request)
    return templates.TemplateResponse("admin_users.html", {
        "request": request,
        "users": users,
        "me": me,
        "flash": flash,
        "csrf_token": get_csrf_token(request)
    })

@app.post("/admin/users/create")
async def admin_user_create(request: Request):
    need = require_admin(request)
    if need: return need
    form = await request.form()
    if not check_csrf(request, form):
        return PlainTextResponse("CSRF doğrulaması başarısız", status_code=403)

    username = (form.get("username") or "").strip()
    password = (form.get("password") or "").strip()
    role = (form.get("role") or "staff").strip()
    if role not in ("admin","staff"):
        role = "staff"
    if not username or not password:
        return RedirectResponse(url="/admin/users?flash=Kullanıcı+adı+ve+parola+gerekli", status_code=303)

    # benzersiz kontrol
    if find_user(username):
        return RedirectResponse(url="/admin/users?flash=Bu+kullanıcı+zaten+var", status_code=303)

    create_user_if_missing(username, password, role)
    return RedirectResponse(url="/admin/users?flash=Kullanıcı+oluşturuldu", status_code=303)

@app.post("/admin/users/delete")
async def admin_user_delete(request: Request):
    need = require_admin(request)
    if need: return need
    form = await request.form()
    if not check_csrf(request, form):
        return PlainTextResponse("CSRF doğrulaması başarısız", status_code=403)
    me = current_user(request)
    try:
        uid = int(form.get("user_id"))
    except:
        return RedirectResponse(url="/admin/users?flash=Geçersiz+ID", status_code=303)
    if uid == me["id"]:
        return RedirectResponse(url="/admin/users?flash=Kendini+silmezsin", status_code=303)

    target = get_user_by_id(uid)
    if not target:
        return RedirectResponse(url="/admin/users?flash=Kullanıcı+bulunamadı", status_code=303)

    if target["role"] == "admin" and count_admins() <= 1:
        return RedirectResponse(url="/admin/users?flash=Son+admin+silinemez", status_code=303)

    delete_user(uid)
    return RedirectResponse(url="/admin/users?flash=Kullanıcı+silindi", status_code=303)

@app.post("/admin/users/reset_password")
async def admin_user_reset_password(request: Request):
    need = require_admin(request)
    if need: return need
    form = await request.form()
    if not check_csrf(request, form):
        return PlainTextResponse("CSRF doğrulaması başarısız", status_code=403)
    try:
        uid = int(form.get("user_id"))
    except:
        return RedirectResponse(url="/admin/users?flash=Geçersiz+ID", status_code=303)
    new_password = (form.get("new_password") or "").strip()
    if not new_password:
        return RedirectResponse(url="/admin/users?flash=Parola+boş+olamaz", status_code=303)
    update_user_password(uid, new_password)
    return RedirectResponse(url="/admin/users?flash=Parola+güncellendi", status_code=303)

# ========= Plan üretimi (POST /generate) =========
@app.post("/generate")
async def generate(request: Request):
    need = require_login(request)
    if need: return need
    user = current_user(request)

    form = await request.form()
    if not check_csrf(request, form):
        return PlainTextResponse("CSRF doğrulaması başarısız", status_code=403)

    doc_name = (form.get("doc_name") or "").strip()
    contact_info = (form.get("contact_info") or "").strip()
    plan_month = (form.get("plan_month") or "").strip()   # YYYY-MM
    interval_days = int(form.get("interval_days") or "1")
    project_tag = (form.get("project_tag") or "").strip()
    files = form.getlist("files")

    if not files:
        return PlainTextResponse("Görsel yüklenmedi. Lütfen en az 1 görsel seçin.", status_code=400)
    if len(files) > MAX_IMAGES:
        return PlainTextResponse(f"En fazla {MAX_IMAGES} görsel yükleyebilirsiniz.", status_code=400)

    try:
        y_s, m_s = plan_month.split("-"); year, month = int(y_s), int(m_s)
        if not (1 <= month <= 12): raise ValueError()
    except Exception:
        return PlainTextResponse("Plan ayı hatalı. Lütfen YYYY-AA formatında bir ay seçin.", status_code=400)

    dates = generate_schedule(year, month, int(interval_days))
    if len(files) > len(dates):
        return PlainTextResponse(
            f"Seçilen aralıkla {len(dates)} tarih üretiliyor, ancak {len(files)} görsel yüklediniz. "
            f"Aralığı küçültün (örn. 1-2 gün) ya da görsel sayısını azaltın.", status_code=400
        )

    async def prep(upload: UploadFile):
        jpeg_bytes, stub, h = await make_preview_bytes(upload)
        namespace = f"{user['id']}|{project_tag}"
        h_ns = hashlib.sha256((namespace + "|" + h).encode()).hexdigest()
        return (stub, jpeg_bytes, h_ns)

    processed = await asyncio.gather(*(prep(f) for f in files))

    try:
        content, total_in, total_out, _ = await build_docx_and_collect_usage(doc_name, contact_info, processed, dates[:len(processed)])
    except Exception as e:
        print("Üretim hatası:", traceback.format_exc()[:2000])
        return PlainTextResponse(f"Üretim hatası: {str(e)}", status_code=500)

    cost_usd = (total_in / 1_000_000.0) * RATE_IN_PER_MTOK + (total_out / 1_000_000.0) * RATE_OUT_PER_MTOK
    cost_try = cost_usd * USD_TRY_RATE
    ts = (datetime.datetime.now(TZ) if TZ else datetime.datetime.now()).isoformat(timespec="seconds")
    user_key = request.client.host if request.client else "unknown"
    try:
        await run_in_threadpool(
            log_usage, ts, user["id"], user_key, DEFAULT_MODEL, doc_name,
            project_tag, plan_month, len(processed),
            total_in, total_out, cost_usd, cost_try
        )
    except Exception as e:
        print("DB log error:", e)

    headers = {
        "Content-Disposition": f'attachment; filename="{doc_name}.docx"',
        "X-API-Cost-USD": f"{cost_usd:.6f}",
        "X-API-Cost-TRY": f"{cost_try:.2f}",
        "X-API-InTokens": str(total_in),
        "X-API-OutTokens": str(total_out),
    }
    return StreamingResponse(
        io.BytesIO(content),
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers=headers
    )
