import os
import sqlite3
import secrets
import time
from typing import Optional

from fastapi import FastAPI, Request, Form, HTTPException, status, Depends
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    PlainTextResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.base import BaseHTTPMiddleware

# ==========
# ENV & CONST
# ==========
APP_NAME = os.getenv("APP_NAME", "Otel Planlama Stüdyosu")
# Render Free: kalıcı disk yok; SQLite'ı /tmp'te tutuyoruz
USAGE_DB_PATH = os.getenv("USAGE_DB_PATH", "/tmp/usage.db")

# Güvenlik
SECRET_KEY = os.getenv(
    "SECRET_KEY",
    # Yedek anahtar: prod'da mutlaka ENV ile verin.
    "dev-" + secrets.token_urlsafe(48),
)

# Admin tohumlama (opsiyonel)
ADMIN_CODE_USER = os.getenv("ADMIN_CODE_USER", "admin")
ADMIN_CODE_PASS = os.getenv("ADMIN_CODE_PASS", "admin123")

# Opsiyonel: log etiketi
LOG_TAG = os.getenv("APP_LOG_TAG", "Martı Prime")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")
STATIC_DIR = os.path.join(BASE_DIR, "static")

# ==========
# APP
# ==========
app = FastAPI(title=APP_NAME)

# Session middleware mutlaka ÖNCE eklensin ki diğer middleware/handler'lar request.session'a erişebilsin
app.add_middleware(
    SessionMiddleware,
    secret_key=SECRET_KEY,
    https_only=True,          # Render prod'da TLS var
    same_site="lax",
    max_age=60 * 60 * 8,      # 8 saat
)

# Basit auth duvarı: login/health/static harici sayfalar için oturum zorunlu
class AuthWall(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # Serbest bırakılan yollar
        if (
            path.startswith("/static")
            or path.startswith("/health")
            or path.startswith("/login")
            or path.startswith("/csrf")
        ):
            return await call_next(request)

        user = request.session.get("user")
        if user is None:
            # Tarayıcı ise login'e yönlendir, API ise 401
            accept = request.headers.get("accept", "")
            if "text/html" in accept or path == "/":
                next_url = request.url.path
                if request.url.query:
                    next_url += f"?{request.url.query}"
                return RedirectResponse(
                    url=f"/login?next={next_url}",
                    status_code=status.HTTP_303_SEE_OTHER,
                )
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)

        return await call_next(request)

app.add_middleware(AuthWall)

# Statik & Jinja (varsa)
if os.path.isdir(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Jinja2Templates(directory=TEMPLATES_DIR) if os.path.isdir(TEMPLATES_DIR) else None

# ==========
# DB
# ==========
def get_db() -> sqlite3.Connection:
    # /tmp dizini mevcut; dosya yoksa sqlite oluşturur
    conn = sqlite3.connect(USAGE_DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def ensure_db() -> None:
    conn = get_db()
    try:
        cur = conn.cursor()
        # Kullanıcılar
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                pw_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'user',
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        # Basit kullanım log'u (opsiyonel)
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS usage_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                username TEXT,
                action TEXT,
                meta TEXT
            )
            """
        )
        conn.commit()
    finally:
        conn.close()

def log_usage(username: Optional[str], action: str, meta: str = ""):
    try:
        conn = get_db()
        with conn:
            conn.execute(
                "INSERT INTO usage_logs (username, action, meta) VALUES (?, ?, ?)",
                (username, f"{LOG_TAG}:{action}", meta),
            )
    except Exception:
        # log hatası uygulamayı bozmasın
        pass

# ==========
# Security helpers
# ==========
import bcrypt

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

def verify_password(password: str, pw_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), pw_hash.encode("utf-8"))
    except Exception:
        return False

# CSRF token: oturumda sakla ve form/header ile doğrula
def get_csrf_token(request: Request) -> str:
    token = request.session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf_token"] = token
    return token

def require_csrf(request: Request, provided: Optional[str]) -> None:
    expected = request.session.get("csrf_token")
    header_token = request.headers.get("x-csrf-token")
    token = provided or header_token
    if not expected or not token or not secrets.compare_digest(expected, token):
        raise HTTPException(status_code=400, detail="Invalid or missing CSRF token")

# ==========
# Admin seeding
# ==========
def create_user_if_missing(username: str, password: str, role: str = "user") -> None:
    pw_hash = hash_password(password)
    conn = get_db()
    try:
        with conn:
            # INSERT OR IGNORE: varsa patlamasın
            conn.execute(
                "INSERT OR IGNORE INTO users (username, pw_hash, role) VALUES (?, ?, ?)",
                (username, pw_hash, role),
            )
    finally:
        conn.close()

def seed_admin_from_code():
    create_user_if_missing(ADMIN_CODE_USER, ADMIN_CODE_PASS, "admin")

# ==========
# Startup
# ==========
@app.on_event("startup")
def on_startup():
    ensure_db()
    seed_admin_from_code()
    print(f"[INFO] Using DB at: {USAGE_DB_PATH}")

# ==========
# Utilities
# ==========
def user_in_session(request: Request) -> Optional[dict]:
    return request.session.get("user")

def require_admin(request: Request) -> dict:
    user = user_in_session(request)
    if not user or user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin required")
    return user

# ==========
# Health & HEAD (Render uyum)
# ==========
@app.get("/health")
def health():
    ok = True
    try:
        conn = get_db()
        conn.execute("SELECT 1")
        conn.close()
    except Exception:
        ok = False
    return {"ok": ok, "db": USAGE_DB_PATH, "app": APP_NAME}

@app.head("/")
def head_root():
    return Response(status_code=204)

@app.head("/login")
def head_login():
    return Response(status_code=204)

# ==========
# CSRF fetch endpoint (SPA/Ajax için)
# ==========
@app.get("/csrf")
def csrf(request: Request):
    return {"csrf_token": get_csrf_token(request)}

# ==========
# Auth
# ==========
def render_login_html(request: Request, next_url: str, error: str = "") -> HTMLResponse:
    # Jinja varsa onu kullan
    if templates:
        return templates.TemplateResponse(
            "login.html",
            {
                "request": request,
                "app_name": APP_NAME,
                "next": next_url,
                "csrf_token": get_csrf_token(request),
                "error": error,
            },
            status_code=200,
        )
    # Minimal yerleşik HTML (şablon yoksa)
    csrf = get_csrf_token(request)
    html = f"""
    <!doctype html>
    <html lang="tr">
    <head>
      <meta charset="utf-8" />
      <meta name="viewport" content="width=device-width, initial-scale=1" />
      <title>{APP_NAME} · Giriş</title>
      <link rel="preconnect" href="https://fonts.googleapis.com">
      <style>
        body{{font-family:system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;background:#0b1220;color:#eaeef8;display:flex;align-items:center;justify-content:center;height:100vh;margin:0}}
        .card{{background:#121a2b;border:1px solid #1f2a44;border-radius:16px;box-shadow:0 10px 30px rgba(0,0,0,.4);padding:24px;max-width:360px;width:100%}}
        h1{{font-size:18px;margin:0 0 12px}}
        .muted{{color:#9fb0d1;font-size:13px;margin-bottom:18px}}
        label{{display:block;margin:10px 0 6px;font-size:13px;color:#c9d7f7}}
        input{{width:100%;padding:10px;border-radius:10px;border:1px solid #243251;background:#0c1322;color:#eaeef8}}
        button{{margin-top:14px;width:100%;padding:10px;border-radius:10px;border:0;background:#3b82f6;color:white;font-weight:600;cursor:pointer}}
        .err{{color:#ff6b6b;font-size:13px;margin-top:6px;min-height:16px}}
        .row{{display:flex;gap:8px}}
      </style>
    </head>
    <body>
      <form class="card" method="post" action="/login">
        <h1>Giriş yap</h1>
        <div class="muted">Admin'in verdiği kullanıcıyla devam edin.</div>
        {"<div class='err'>" + error + "</div>" if error else "<div class='err'>&nbsp;</div>"}
        <input type="hidden" name="csrf_token" value="{csrf}" />
        <input type="hidden" name="next" value="{next_url}" />
        <label>Kullanıcı adı</label>
        <input type="text" name="username" autofocus required />
        <label>Şifre</label>
        <input type="password" name="password" required />
        <button type="submit">Giriş</button>
      </form>
    </body>
    </html>
    """
    return HTMLResponse(content=html, status_code=200)

@app.get("/login", response_class=HTMLResponse)
def login_get(request: Request, next: str = "/"):
    return render_login_html(request, next_url=next)

# CSRF'yi kendimiz doğruladığımız için Pydantic "Field required" hatasını engelliyoruz
@app.post("/login")
def login_post(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
    csrf_token: Optional[str] = Form(None),
    next: str = Form("/"),
):
    try:
        require_csrf(request, csrf_token)
    except HTTPException as e:
        # Form eksikse bile güzel hata verelim
        return render_login_html(request, next_url=next, error="CSRF doğrulaması başarısız.")

    # Kimlik doğrulama
    conn = get_db()
    try:
        cur = conn.execute("SELECT username, pw_hash, role FROM users WHERE username = ?", (username.strip(),))
        row = cur.fetchone()
    finally:
        conn.close()

    if not row or not verify_password(password, row["pw_hash"]):
        log_usage(None, "login_failed", f"user={username}")
        return render_login_html(request, next_url=next, error="Kullanıcı adı veya şifre hatalı.")

    # Oturumu yaz
    request.session["user"] = {"username": row["username"], "role": row["role"], "t": int(time.time())}
    log_usage(row["username"], "login_ok")

    # Güvenli yönlendirme
    target = next if next.startswith("/") else "/"
    return RedirectResponse(url=target, status_code=status.HTTP_303_SEE_OTHER)

@app.get("/logout")
def logout(request: Request):
    u = request.session.get("user")
    if u:
        log_usage(u.get("username"), "logout")
    request.session.clear()
    return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

# ==========
# Root (örnek sayfa)
# ==========
def render_index_html(request: Request) -> HTMLResponse:
    user = user_in_session(request)
    username = user.get("username") if user else "?"
    role = user.get("role") if user else "user"
    admin_link = '<a href="/admin" style="margin-right:12px">Admin</a>' if role == "admin" else ""
    logout = '<a href="/logout">Çıkış</a>'
    # Jinja varsa templates/index.html tercih edilecek
    if templates:
        return templates.TemplateResponse(
            "index.html",
            {
                "request": request,
                "app_name": APP_NAME,
                "user": user,
                "csrf_token": get_csrf_token(request),
            },
            status_code=200,
        )
    html = f"""
    <!doctype html><html lang="tr"><head>
      <meta charset="utf-8" />
      <meta name="viewport" content="width=device-width, initial-scale=1" />
      <title>{APP_NAME}</title>
      <style>
        body{{font-family:system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;background:#0b1220;color:#eaeef8;margin:0}}
        header{{display:flex;justify-content:space-between;align-items:center;padding:16px;border-bottom:1px solid #1f2a44;background:#0f172a}}
        main{{padding:20px}}
        .nav a{{color:#c9d7f7;text-decoration:none;margin-right:12px}}
        .card{{background:#121a2b;border:1px solid #1f2a44;border-radius:16px;padding:20px;max-width:900px}}
        .muted{{color:#9fb0d1}}
        #drop{{margin-top:16px;border:2px dashed #2d3a5f;border-radius:16px;padding:30px;text-align:center}}
        #thumbs{{display:flex;gap:10px;flex-wrap:wrap;margin-top:12px}}
        #thumbs img{{width:96px;height:96px;object-fit:cover;border-radius:10px;border:1px solid #22304f}}
      </style>
    </head><body>
      <header>
        <div class="nav">
          <strong>{APP_NAME}</strong>
        </div>
        <div class="nav">
          {admin_link}
          {logout}
        </div>
      </header>
      <main>
        <div class="card">
          <div>Merhaba, <strong>{username}</strong> <span class="muted">({role})</span></div>
          <p class="muted">Eski arayüz hissiyle basit sürükle-bırak önizleme alanı aşağıda.</p>
          <div id="drop">Görselleri buraya sürükleyin veya tıklayın<input id="file" type="file" multiple accept="image/*" style="display:none"/></div>
          <div id="thumbs"></div>
        </div>
      </main>
      <script>
        const drop = document.getElementById('drop');
        const fileInput = document.getElementById('file');
        const thumbs = document.getElementById('thumbs');

        const openPicker = () => fileInput.click();
        drop.addEventListener('click', openPicker);

        const onFiles = (files) => {{
          thumbs.innerHTML = '';
          [...files].forEach(f => {{
            const url = URL.createObjectURL(f);
            const img = document.createElement('img');
            img.src = url;
            thumbs.appendChild(img);
          }});
        }};

        drop.addEventListener('dragover', e => {{ e.preventDefault(); drop.style.opacity = 0.8; }});
        drop.addEventListener('dragleave', e => {{ drop.style.opacity = 1; }});
        drop.addEventListener('drop', e => {{
          e.preventDefault(); drop.style.opacity = 1;
          onFiles(e.dataTransfer.files);
        }});
        fileInput.addEventListener('change', e => onFiles(e.target.files));
      </script>
    </body></html>
    """
    return HTMLResponse(html)

@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return render_index_html(request)

# ==========
# Admin: kullanıcı yönetimi
# ==========
def render_admin_html(request: Request, users: list[sqlite3.Row], error: str = "", ok: str = "") -> HTMLResponse:
    if templates:
        return templates.TemplateResponse(
            "admin.html",
            {
                "request": request,
                "app_name": APP_NAME,
                "users": users,
                "csrf_token": get_csrf_token(request),
                "error": error,
                "ok": ok,
            },
            status_code=200,
        )
    rows = "".join(
        f"<tr><td>{u['username']}</td><td>{u['role']}</td>"
        f"<td><form method='post' action='/admin/delete-user' style='display:inline'>"
        f"<input type='hidden' name='csrf_token' value='{get_csrf_token(request)}'/>"
        f"<input type='hidden' name='username' value='{u['username']}'/>"
        f"<button onclick=\"return confirm('Silinsin mi?')\">Sil</button></form></td></tr>"
        for u in users
    )
    msg = f"<div style='color:#7cf08a'>{ok}</div>" if ok else ""
    err = f"<div style='color:#ff6b6b'>{error}</div>" if error else ""
    html = f"""
    <!doctype html><html lang="tr"><head>
      <meta charset="utf-8" /><meta name="viewport" content="width=device-width, initial-scale=1" />
      <title>Admin · {APP_NAME}</title>
      <style>
        body{{font-family:system-ui;background:#0b1220;color:#eaeef8;margin:0}}
        header{{display:flex;justify-content:space-between;align-items:center;padding:16px;border-bottom:1px solid #1f2a44;background:#0f172a}}
        main{{padding:20px;max-width:960px;margin:0 auto}}
        table{{width:100%;border-collapse:collapse}}
        th,td{{border-bottom:1px solid #25345a;padding:8px}}
        input,select,button{{padding:8px;border-radius:8px;border:1px solid #243251;background:#0c1322;color:#eaeef8}}
        .row{{display:flex;gap:8px;flex-wrap:wrap;margin:10px 0}}
        a{{color:#c9d7f7;text-decoration:none;margin-left:12px}}
      </style>
    </head><body>
      <header>
        <div><strong>Admin · {APP_NAME}</strong></div>
        <div>
          <a href="/">Planlama</a>
          <a href="/logout">Çıkış</a>
        </div>
      </header>
      <main>
        {msg}{err}
        <h2>Kullanıcılar</h2>
        <table>
          <thead><tr><th>Kullanıcı</th><th>Rol</th><th>İşlem</th></tr></thead>
          <tbody>{rows}</tbody>
        </table>

        <h3>Yeni Kullanıcı</h3>
        <form method="post" action="/admin/create-user">
          <input type="hidden" name="csrf_token" value="{get_csrf_token(request)}" />
          <div class="row">
            <input name="new_username" placeholder="kullanıcı adı" required />
            <input name="new_password" type="password" placeholder="şifre" required />
            <select name="role">
              <option value="user">user</option>
              <option value="admin">admin</option>
            </select>
            <button type="submit">Oluştur</button>
          </div>
        </form>
      </main>
    </body></html>
    """
    return HTMLResponse(html)

@app.get("/admin", response_class=HTMLResponse)
def admin_get(request: Request, _: dict = Depends(require_admin)):
    conn = get_db()
    try:
        users = conn.execute("SELECT username, role FROM users ORDER BY username").fetchall()
    finally:
        conn.close()
    return render_admin_html(request, users)

@app.post("/admin/create-user")
def admin_create_user(
    request: Request,
    _: dict = Depends(require_admin),
    new_username: str = Form(""),
    new_password: str = Form(""),
    role: str = Form("user"),
    csrf_token: Optional[str] = Form(None),
):
    require_csrf(request, csrf_token)
    new_username = new_username.strip()
    if not new_username or not new_password:
        raise HTTPException(400, "Eksik bilgi")

    if role not in ("user", "admin"):
        role = "user"

    conn = get_db()
    try:
        with conn:
            conn.execute(
                "INSERT INTO users (username, pw_hash, role) VALUES (?, ?, ?)",
                (new_username, hash_password(new_password), role),
            )
        log_usage(user_in_session(request)["username"], "admin_create_user", new_username)
    except sqlite3.IntegrityError:
        # zaten var
        pass
    finally:
        conn.close()

    return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)

@app.post("/admin/delete-user")
def admin_delete_user(
    request: Request,
    _: dict = Depends(require_admin),
    username: str = Form(""),
    csrf_token: Optional[str] = Form(None),
):
    require_csrf(request, csrf_token)
    username = username.strip()
    me = user_in_session(request)["username"]
    if username == me:
        return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)

    conn = get_db()
    try:
        with conn:
            conn.execute("DELETE FROM users WHERE username = ?", (username,))
        log_usage(me, "admin_delete_user", username)
    finally:
        conn.close()

    return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)
