import os, io, sqlite3, secrets, datetime
from typing import List, Optional
from fastapi import FastAPI, Request, Form, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
import bcrypt
from docx import Document
from docx.shared import Inches
from PIL import Image

# -------------------- CONFIG --------------------
APP_NAME = os.getenv("APP_NAME", "Otel Planlama Stüdyosu")
SECRET_KEY = os.getenv("SECRET_KEY", secrets.token_urlsafe(48))
USAGE_DB_PATH = os.getenv("USAGE_DB_PATH", "/tmp/usage.db")

# Admin tohumlama (ENV varsa onunla, yoksa default)
ADMIN_CODE_USER = os.getenv("ADMIN_CODE_USER", "admin")
ADMIN_CODE_PASS = os.getenv("ADMIN_CODE_PASS", "admin123")

# -------------------- APP SETUP -----------------
app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

def get_csrf_token(request: Request) -> str:
    token = request.session.get("csrf_token") if "session" in request.scope else None
    if not token:
        token = secrets.token_urlsafe(32)
        # session yoksa SessionMiddleware henüz koşmamıştır; ama normal akışta vardır.
        if "session" in request.scope:
            request.session["csrf_token"] = token
    return token

templates.env.globals["get_csrf_token"] = get_csrf_token
templates.env.globals["APP_NAME"] = APP_NAME

# -------------------- DB ------------------------
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
            username TEXT UNIQUE NOT NULL,
            pw_hash BLOB NOT NULL,
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
    conn.commit(); conn.close()

def create_user_if_missing(username: str, password: str, role: str = "admin"):
    if not username or not password:
        return
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id FROM users WHERE username=?", (username,))
    if not cur.fetchone():
        pw_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt())
        cur.execute("INSERT INTO users (username, pw_hash, role) VALUES (?,?,?)",
                    (username, pw_hash, role))
        conn.commit()
    conn.close()

@app.on_event("startup")
def on_startup():
    ensure_db()
    create_user_if_missing(ADMIN_CODE_USER, ADMIN_CODE_PASS, "admin")
    print(f"[INFO] Using DB at: {USAGE_DB_PATH}")

# -------------------- AUTH UTILS ----------------
def user_in_session(request: Request) -> Optional[dict]:
    # SessionMiddleware henüz çalışmadıysa çakılmamak için güvenli kontrol
    if "session" not in request.scope:
        return None
    return request.session.get("user")

def require_csrf(request: Request, token_from_body: str):
    if "session" not in request.scope:
        raise HTTPException(400, "Session not initialized")
    real = request.session.get("csrf_token")
    if not real or not token_from_body or token_from_body != real:
        raise HTTPException(status_code=400, detail="Invalid CSRF token")

ALLOW_PREFIXES = ("/static",)
ALLOW_EXACT = {"/login", "/health"}

class AuthWall(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path in ALLOW_EXACT or any(path.startswith(p) for p in ALLOW_PREFIXES):
            return await call_next(request)
        u = user_in_session(request)
        if not u:
            nxt = path
            return RedirectResponse(url=f"/login?next={nxt}", status_code=303)
        return await call_next(request)

# DİKKAT: Middleware sırası. En son eklenen İLK çalışır.
# 1) AuthWall'ı ekliyoruz (iç katman)
app.add_middleware(AuthWall)
# 2) SessionMiddleware'i en SON ekliyoruz ki İLK o çalışsın ve request.session hazır olsun.
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, same_site="lax", https_only=False)

# -------------------- ROUTES: AUTH ---------------
@app.get("/login", response_class=HTMLResponse)
def login_get(request: Request, next: Optional[str] = "/"):
    if user_in_session(request):
        return RedirectResponse(url=next or "/", status_code=303)
    return templates.TemplateResponse("login.html", {"request": request, "next": next})

@app.post("/login")
def login_post(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    csrf_token: str = Form(...),
    next: Optional[str] = Form("/")
):
    require_csrf(request, csrf_token)
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, pw_hash, role FROM users WHERE username=?", (username,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return templates.TemplateResponse("login.html", {
            "request": request, "error": "Kullanıcı bulunamadı", "next": next or "/"
        })
    _, pw_hash, role = row
    if not bcrypt.checkpw(password.encode("utf-8"), pw_hash):
        return templates.TemplateResponse("login.html", {
            "request": request, "error": "Şifre hatalı", "next": next or "/"
        })
    request.session["user"] = {"username": username, "role": role}
    return RedirectResponse(url=(next or "/"), status_code=303)

@app.post("/logout")
def logout_post(request: Request, csrf_token: str = Form(...)):
    require_csrf(request, csrf_token)
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)

# -------------------- ROUTES: PAGES --------------
@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    u = user_in_session(request)
    return templates.TemplateResponse("index.html", {"request": request, "user": u})

@app.get("/admin/dashboard", response_class=HTMLResponse)
def admin_dashboard(request: Request):
    u = user_in_session(request)
    if not u or u.get("role") != "admin":
        raise HTTPException(403, "Admin required")
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
    u = user_in_session(request)
    if not u or u.get("role") != "admin":
        raise HTTPException(403, "Admin required")
    require_csrf(request, csrf_token)
    if new_role not in ("admin", "staff"):
        new_role = "staff"

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id FROM users WHERE username=?", (new_username,))
    if cur.fetchone():
        conn.close()
        return RedirectResponse(url="/admin/dashboard?msg=exists", status_code=303)

    pw_hash = bcrypt.hashpw(new_password.encode("utf-8"), bcrypt.gensalt())
    cur.execute("INSERT INTO users (username, pw_hash, role) VALUES (?,?,?)",
                (new_username, pw_hash, new_role))
    conn.commit(); conn.close()
    return RedirectResponse(url="/admin/dashboard?msg=created", status_code=303)

@app.post("/admin/users/delete")
def admin_users_delete(
    request: Request,
    user_id: int = Form(...),
    csrf_token: str = Form(...)
):
    u = user_in_session(request)
    if not u or u.get("role") != "admin":
        raise HTTPException(403, "Admin required")
    require_csrf(request, csrf_token)
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM users WHERE id=?", (user_id,))
    conn.commit(); conn.close()
    return RedirectResponse(url="/admin/dashboard?msg=deleted", status_code=303)

# -------------------- PLAN -----------------------
def month_to_range(year: int, month: int):
    start = datetime.date(year, month, 1)
    end = (datetime.date(year + (month==12), (month % 12) + 1, 1) - datetime.timedelta(days=1))
    return start, end

@app.post("/plan")
async def create_plan(
    request: Request,
    hotel_name: str = Form(...),
    plan_month: str = Form(...),
    interval_days: int = Form(...),
    tone: str = Form(...),
    csrf_token: str = Form(...),
    images: List[UploadFile] = File([])
):
    u = user_in_session(request)
    require_csrf(request, csrf_token)

    y, m = map(int, plan_month.split("-"))
    start, end = month_to_range(y, m)
    dates = []
    d = start
    while d <= end:
        dates.append(d)
        d += datetime.timedelta(days=interval_days)
    if not dates:
        dates = [start]

    doc = Document()
    doc.add_heading(f"{hotel_name} – {start.strftime('%B %Y')} Sosyal Medya Planı", level=1)

    for idx, up in enumerate(images or []):
        img_bytes = await up.read()
        dt = dates[min(idx, len(dates)-1)]
        doc.add_paragraph(f"Paylaşım Tarihi: {dt.strftime('%d.%m.%Y')} – ({tone})")
        try:
            Image.open(io.BytesIO(img_bytes)).convert("RGB")
            tmp = io.BytesIO(img_bytes); tmp.name = up.filename
            doc.add_picture(tmp, width=Inches(5.5))
        except Exception:
            doc.add_paragraph("[Görsel eklenemedi]")
        doc.add_paragraph(f"Açıklama: {hotel_name} için {tone} tonda paylaşım önerisi.")
        doc.add_paragraph("Hashtag: #otel #tatil #rezervasyon")
        doc.add_paragraph("")

    conn = get_db()
    cur = conn.cursor()
    cur.execute("INSERT INTO logs (username, hotel, month, created_at) VALUES (?,?,?,?)",
                (u['username'] if u else None, hotel_name, plan_month, datetime.datetime.utcnow().isoformat()))
    conn.commit(); conn.close()

    outf = io.BytesIO()
    doc.save(outf); outf.seek(0)
    filename = f"{hotel_name.replace(' ','_')}_{plan_month}.docx"
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    return StreamingResponse(outf, media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document", headers=headers)

# -------------------- HEALTH ----------------------
@app.get("/health")
def health():
    return {"ok": True, "db": USAGE_DB_PATH}
