import io
import os
import json
import sqlite3
from datetime import datetime, timedelta
from typing import Optional, List

from fastapi import FastAPI, Request, Form, UploadFile, File, HTTPException
from fastapi.responses import RedirectResponse, StreamingResponse, JSONResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from starlette.status import HTTP_303_SEE_OTHER, HTTP_307_TEMPORARY_REDIRECT

from PIL import Image
from docx import Document
from docx.shared import Inches

# ---- Gemini (opsiyonel) ----
GEMINI_AVAILABLE = False
try:
    import google.generativeai as genai  # type: ignore
    GEMINI_AVAILABLE = True
except Exception:
    GEMINI_AVAILABLE = False


# =========================
# Ayarlar
# =========================
APP_NAME = os.getenv("APP_NAME", "Otel Planlama Stüdyosu")
SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-this")
USAGE_DB_PATH = os.getenv("USAGE_DB_PATH", "/tmp/usage.db")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY") or os.getenv("GeminiAPI")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

# =========================
# App & Static & Templates
# =========================
app = FastAPI()
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, same_site="lax")

os.makedirs("static/css", exist_ok=True)
os.makedirs("static/js", exist_ok=True)
os.makedirs("templates", exist_ok=True)

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")
templates.env.globals["app_name"] = APP_NAME


# =========================
# Yardımcılar: DB & Auth
# =========================
def db_conn():
    conn = sqlite3.connect(USAGE_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db_conn() as con:
        cur = con.cursor()
        # Düz metin parola (istenildiği gibi)
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS users(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'user',
                created_at TEXT NOT NULL
            );
            """
        )
        con.commit()

        # Admin varsa dokunma, yoksa tohumla
        admin_user = os.getenv("ADMIN_CODE_USER", "admin")
        admin_pass = os.getenv("ADMIN_CODE_PASS", "admin123")
        cur.execute("SELECT id FROM users WHERE username=?", (admin_user,))
        row = cur.fetchone()
        if not row:
            cur.execute(
                "INSERT INTO users(username, password, role, created_at) VALUES(?,?,?,?)",
                (admin_user, admin_pass, "admin", datetime.utcnow().isoformat(timespec="seconds")),
            )
            con.commit()


@app.on_event("startup")
def on_startup():
    init_db()
    if GEMINI_AVAILABLE and GEMINI_API_KEY:
        try:
            genai.configure(api_key=GEMINI_API_KEY)
        except Exception:
            pass
    print(f"[INFO] Using DB at: {USAGE_DB_PATH}")


def ensure_csrf(request: Request) -> str:
    token = request.session.get("csrf_token")
    if not token:
        import secrets

        token = secrets.token_urlsafe(32)
        request.session["csrf_token"] = token
    return token


def verify_csrf(request: Request, token_from_form: str):
    token = request.session.get("csrf_token")
    if not token or (token_from_form or "").strip() != token:
        raise HTTPException(status_code=400, detail="CSRF doğrulaması başarısız")


def session_user(request: Request) -> Optional[dict]:
    return request.session.get("user")


def require_login(request: Request) -> dict:
    u = session_user(request)
    if not u:
        # 307 ile login'e yönlendir
        raise HTTPException(status_code=HTTP_307_TEMPORARY_REDIRECT, detail="login", headers={"Location": "/login"})
    return u


def require_admin(request: Request) -> dict:
    u = require_login(request)
    if u.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Yetkisiz")
    return u


# =========================
# Rotalar (UI)
# =========================
@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    if session_user(request):
        return RedirectResponse("/plan", status_code=HTTP_303_SEE_OTHER)
    return RedirectResponse("/login", status_code=HTTP_303_SEE_OTHER)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    ctx = {"request": request, "csrf_token": ensure_csrf(request), "error": None}
    return templates.TemplateResponse("login.html", ctx)


@app.post("/login")
def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    csrf_token: str = Form(...)
):
    try:
        verify_csrf(request, csrf_token)
    except HTTPException:
        ctx = {"request": request, "csrf_token": ensure_csrf(request), "error": "Oturum doğrulaması başarısız."}
        return templates.TemplateResponse("login.html", ctx, status_code=400)

    with db_conn() as con:
        cur = con.cursor()
        cur.execute("SELECT id, username, password, role FROM users WHERE username=?", (username,))
        row = cur.fetchone()

    if not row or str(row["password"]) != password:
        ctx = {"request": request, "csrf_token": ensure_csrf(request), "error": "Kullanıcı adı / şifre hatalı."}
        return templates.TemplateResponse("login.html", ctx, status_code=401)

    request.session["user"] = {"id": row["id"], "username": row["username"], "role": row["role"]}
    return RedirectResponse("/plan", status_code=HTTP_303_SEE_OTHER)


@app.post("/logout")
def logout(request: Request, csrf_token: str = Form(...)):
    verify_csrf(request, csrf_token)
    request.session.clear()
    return RedirectResponse("/login", status_code=HTTP_303_SEE_OTHER)


@app.get("/plan", response_class=HTMLResponse)
def plan_page(request: Request):
    try:
        require_login(request)
    except HTTPException:
        return RedirectResponse("/login", status_code=HTTP_303_SEE_OTHER)
    ctx = {
        "request": request,
        "csrf_token": ensure_csrf(request),
        "session_user": session_user(request),
    }
    return templates.TemplateResponse("plan.html", ctx)


@app.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request):
    try:
        require_admin(request)
    except HTTPException as ex:
        if ex.status_code == 307:
            return RedirectResponse("/login", status_code=HTTP_303_SEE_OTHER)
        raise

    with db_conn() as con:
        cur = con.cursor()
        cur.execute("SELECT id, username, role, created_at FROM users ORDER BY id ASC")
        users = [{"id": r[0], "username": r[1], "role": r[2], "created_at": r[3]} for r in cur.fetchall()]

    ctx = {"request": request, "csrf_token": ensure_csrf(request), "users": users}
    return templates.TemplateResponse("admin.html", ctx)


@app.post("/admin/users/create")
def admin_user_create(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    role: str = Form("user"),
    csrf_token: str = Form(...)
):
    require_admin(request)
    verify_csrf(request, csrf_token)
    if role not in ("user", "admin"):
        role = "user"
    try:
        with db_conn() as con:
            cur = con.cursor()
            cur.execute(
                "INSERT INTO users(username, password, role, created_at) VALUES(?,?,?,?)",
                (username, password, role, datetime.utcnow().isoformat(timespec="seconds")),
            )
            con.commit()
    except sqlite3.IntegrityError:
        return JSONResponse({"detail": "Bu kullanıcı adı zaten var."}, status_code=400)

    return RedirectResponse("/admin", status_code=HTTP_303_SEE_OTHER)


@app.post("/admin/users/{uid}/delete")
def admin_user_delete(request: Request, uid: int, csrf_token: str = Form(...)):
    require_admin(request)
    verify_csrf(request, csrf_token)
    with db_conn() as con:
        cur = con.cursor()
        cur.execute("SELECT username FROM users WHERE id=?", (uid,))
        r = cur.fetchone()
        if not r:
            return RedirectResponse("/admin", status_code=HTTP_303_SEE_OTHER)
        if r["username"] == "admin":
            return RedirectResponse("/admin", status_code=HTTP_303_SEE_OTHER)
        cur.execute("DELETE FROM users WHERE id=?", (uid,))
        con.commit()
    return RedirectResponse("/admin", status_code=HTTP_303_SEE_OTHER)


# =========================
# Sağlık
# =========================
@app.get("/health")
def health():
    return {"ok": True, "app": APP_NAME}


# =========================
# Gemini yardımcıları
# =========================
def _strip_json(text: str) -> str:
    t = text.strip()
    if "```" in t:
        s = t.find("```")
        e = t.rfind("```")
        if s != -1 and e != -1 and e > s:
            inner = t[s + 3 : e]
            if "\n" in inner:
                inner = inner.split("\n", 1)[1]
            t = inner.strip()
    first = t.find("{")
    last = t.rfind("}")
    if first != -1 and last != -1 and last > first:
        t = t[first : last + 1]
    return t


def gemini_generate_for_image(image_bytes: bytes, mime_type: str, hotel_name: str) -> dict:
    if not (GEMINI_AVAILABLE and GEMINI_API_KEY):
        return {
            "caption": f"{hotel_name} ile keyifli anlar! Tatil ruhu bu karede.",
            "hashtags": ["#otel", "#tatil", "#keşfet", "#konaklama", "#seyahat", "#hotellife"],
        }

    model = genai.GenerativeModel(GEMINI_MODEL)
    prompt = (
        "Rolün: Otel için Instagram içerik yazarı.\n"
        "Verilecek fotoğrafa uygun **Türkçe** 1-2 cümlelik sıcak bir açıklama yaz.\n"
        "Ardından 8-12 adet yine görsele uygun **Türkçe** hashtag üret.\n"
        f"Otel adı: {hotel_name}\n"
        "Sadece şu JSON çıktıyı ver:\n"
        "{\n  \"caption\": \"...\",\n  \"hashtags\": [\"#...\"]\n}\n"
        "Başka metin ekleme."
    )
    img_part = {"mime_type": mime_type or "image/jpeg", "data": image_bytes}

    try:
        resp = model.generate_content([prompt, img_part])
        text = resp.text or ""
        cleaned = _strip_json(text)
        data = json.loads(cleaned)
        cap = str(data.get("caption", "")).strip() or f"{hotel_name} ile unutulmaz anlar!"
        hashtags = data.get("hashtags", [])
        if isinstance(hashtags, str):
            hashtags = [h.strip() for h in hashtags.split() if h.strip()]
        hashtags = [h if h.startswith("#") else f"#{h}" for h in hashtags][:12]
        if not hashtags:
            hashtags = ["#otel", "#tatil", "#keşfet", "#seyahat"]
        return {"caption": cap, "hashtags": hashtags}
    except Exception:
        return {
            "caption": f"{hotel_name} için özel bir kare! Sizi bekliyoruz.",
            "hashtags": ["#otel", "#tatil", "#keşfet", "#holiday"],
        }


# =========================
# DOCX oluşturucu
# =========================
def build_plan_docx(
    hotel_name: str,
    contact_info: str,
    start_date: datetime,
    interval_days: int,
    images: List[UploadFile],
) -> bytes:
    doc = Document()
    for idx, uf in enumerate(images):
        # Tarih başlığı
        date_str = (start_date + timedelta(days=idx * interval_days)).strftime("%d.%m.%Y")
        h = doc.add_heading(date_str, level=1)
        h.alignment = 1

        # Görsel
        data = uf.file.read()
        uf.file.seek(0)
        try:
            img = io.BytesIO(data)
            Image.open(img).close()
            img.seek(0)
            doc.add_picture(img, width=Inches(6.0))
        except Exception:
            doc.add_paragraph("[Görsel eklenemedi]")

        # Açıklama / İletişim / Hashtag
        gen = gemini_generate_for_image(data, uf.content_type, hotel_name)
        caption = gen.get("caption", "").strip()
        hashtags = gen.get("hashtags", [])

        doc.add_paragraph(f"Açıklama: {caption}")
        if contact_info.strip():
            doc.add_paragraph(f"Otel iletişim bilgisi: {contact_info.strip()}")
        if hashtags:
            doc.add_paragraph("Hashtagler: " + " ".join(hashtags))

        if idx < len(images) - 1:
            doc.add_page_break()

    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()


# =========================
# API
# =========================
@app.post("/api/plan/create")
async def api_plan_create(
    request: Request,
    hotel_name: str = Form(...),
    contact_info: str = Form(""),
    interval_days: int = Form(1),
    docx_filename: str = Form("Instagram_Plani.docx"),
    start_date: str = Form(...),
    csrf_token: str = Form(...),
    images: List[UploadFile] = File(...),
):
    require_login(request)
    verify_csrf(request, csrf_token)

    if not images:
        return JSONResponse({"detail": "En az bir görsel seçin."}, status_code=400)

    try:
        sd = datetime.strptime(start_date, "%Y-%m-%d")
    except ValueError:
        return JSONResponse({"detail": "Başlangıç tarihi geçersiz."}, status_code=400)

    try:
        blob = build_plan_docx(
            hotel_name=hotel_name.strip(),
            contact_info=contact_info.strip(),
            start_date=sd,
            interval_days=max(1, int(interval_days)),
            images=images,
        )
    except Exception as e:
        print("[ERROR] plan create failed:", repr(e))
        return JSONResponse({"detail": "Plan oluşturulamadı."}, status_code=500)

    fn = (docx_filename or "Instagram_Plani.docx").strip()
    if not fn.lower().endswith(".docx"):
        fn += ".docx"

    headers = {"Content-Disposition": f'attachment; filename="{fn}"'}
    return StreamingResponse(io.BytesIO(blob),
                             media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                             headers=headers)
