import os
import io
import sqlite3
from datetime import datetime, timedelta, date
from typing import List, Optional, Tuple

from fastapi import FastAPI, Request, Form, UploadFile, File, Depends
from fastapi.responses import RedirectResponse, StreamingResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from starlette.status import HTTP_302_FOUND, HTTP_303_SEE_OTHER
from passlib.hash import bcrypt

from docx import Document
from docx.shared import Inches, Pt
from PIL import Image

# -----------------------------------------------------------------------------
# Ortam değişkenleri / sabitler
# -----------------------------------------------------------------------------
APP_NAME = os.getenv("APP_NAME", "Otel Planlama Stüdyosu")
SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-me")
DB_PATH = os.getenv("USAGE_DB_PATH", "/tmp/usage.db")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY") or os.getenv("GeminiAPI")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

ADMIN_CODE_USER = os.getenv("ADMIN_CODE_USER", "admin")
ADMIN_CODE_PASS = os.getenv("ADMIN_CODE_PASS", "admin123")

# -----------------------------------------------------------------------------
# Uygulama
# -----------------------------------------------------------------------------
app = FastAPI(title=APP_NAME)

# Session middleware (request.session için ŞART)
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, same_site="lax", https_only=False)

# Statik dosyalar (tema için gerekli)
this_dir = os.path.dirname(os.path.abspath(__file__))
static_dir = os.path.join(this_dir, "static")
templates_dir = os.path.join(this_dir, "templates")

if not os.path.isdir(static_dir):
    os.makedirs(static_dir, exist_ok=True)
if not os.path.isdir(templates_dir):
    os.makedirs(templates_dir, exist_ok=True)

app.mount("/static", StaticFiles(directory=static_dir), name="static")

# Jinja2
templates = Jinja2Templates(directory=templates_dir)
templates.env.globals["app_name"] = APP_NAME
templates.env.globals["now"] = lambda: datetime.utcnow().year

# -----------------------------------------------------------------------------
# DB yardımcıları
# -----------------------------------------------------------------------------
def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn

def init_db():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'user',
            created_at TEXT NOT NULL
        );
        """
    )
    # Admin varsa ellemeyelim, yoksa tohumla
    cur.execute("SELECT 1 FROM users WHERE username=?", (ADMIN_CODE_USER,))
    if cur.fetchone() is None:
        cur.execute(
            "INSERT INTO users(username, password_hash, role, created_at) VALUES(?,?,?,?)",
            (
                ADMIN_CODE_USER,
                bcrypt.hash(ADMIN_CODE_PASS),
                "admin",
                datetime.utcnow().isoformat(timespec="seconds"),
            ),
        )
    conn.commit()
    conn.close()

def verify_user(username: str, password: str) -> Optional[Tuple[int, str, str]]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT id, username, password_hash, role FROM users WHERE username=?", (username,))
    row = cur.fetchone()
    conn.close()
    if row and bcrypt.verify(password, row[2]):
        return (row[0], row[1], row[3])
    return None

def list_users() -> List[Tuple]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT id, username, role, created_at FROM users ORDER BY id ASC")
    rows = cur.fetchall()
    conn.close()
    return rows

init_db()

# -----------------------------------------------------------------------------
# Auth yardımcıları
# -----------------------------------------------------------------------------
def current_user(request: Request) -> Optional[dict]:
    # SessionMiddleware kurulu; doğrudan erişebiliriz.
    return request.session.get("user")

def require_login(request: Request):
    user = current_user(request)
    if not user:
        return False
    return True

# -----------------------------------------------------------------------------
# Yardımcılar: Gemini (opsiyonel) + DOCX üretimi
# -----------------------------------------------------------------------------
def try_gemini_generate(otel_adi: str, image_bytes: bytes) -> Tuple[str, str]:
    """
    Varsa Gemini ile açıklama ve hashtag üret, yoksa makul bir fallback üret.
    Görseli base64 gömmek yerine, prompt'ı görsel betimleme odaklı kuruyoruz.
    """
    # Fallback (Gemini yoksa)
    def fallback():
        desc = f"{otel_adi} için görsele uygun çekici bir paylaşım metni. Odanın, ortamın ve konseptin öne çıkan özellikleri vurgulanır."
        hashtags = "#otel #tatil #rezervasyon #holiday #oteltavsiye #seyahat #konfor #manzara"
        return desc, hashtags

    if not GEMINI_API_KEY:
        return fallback()

    # Hafif bir istem: görsel içeriğine uygun kısa TR metin + hashtag
    # Not: Burada sade bir REST örneği bırakıyoruz; Render ortamında key varsa çalışır.
    try:
        import json
        import httpx

        prompt = (
            "Aşağıdaki otel görseli için Türkçe, samimi ve kısa bir Instagram açıklaması üret. "
            "1-2 cümle yeterli. Yeni satırda 8-12 arası Türkçe hashtag ver. "
            f"Otel adı: {otel_adi}. Hashtaglerde otel adı geçebilir."
        )

        # Gemini REST (text-only prompt). Görseli şu aşamada zorunlu kılmıyoruz;
        # istenirse image parts ile base64 gönderimi eklenebilir.
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
        payload = {
            "contents": [
                {
                    "parts": [
                        {"text": prompt}
                    ]
                }
            ]
        }
        headers = {"Content-Type": "application/json"}
        with httpx.Client(timeout=20) as client:
            r = client.post(url, headers=headers, content=json.dumps(payload))
            r.raise_for_status()
            data = r.json()
            text = (
                data.get("candidates", [{}])[0]
                .get("content", {})
                .get("parts", [{}])[0]
                .get("text", "")
            )

        if not text:
            return fallback()

        # Metni açıklama + hashtag olarak ikiye ayırmayı dene
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        desc_lines = []
        tag_lines = []
        for ln in lines:
            if ln.startswith("#") or " #" in ln:
                tag_lines.append(ln)
            else:
                desc_lines.append(ln)
        desc = " ".join(desc_lines)[:500] if desc_lines else text[:500]
        if tag_lines:
            hashtags = " ".join(tag_lines)
        else:
            hashtags = "#otel #tatil #rezervasyon #holiday #oteltavsiye #seyahat #konfor #manzara"
        return desc, hashtags

    except Exception:
        return fallback()

def safe_filename(name: str) -> str:
    name = name.strip() or "Instagram_Plani"
    bad = '<>:"/\\|?*'
    for ch in bad:
        name = name.replace(ch, "_")
    if not name.lower().endswith(".docx"):
        name += ".docx"
    return name

def add_heading(p, text: str, bold=True, size=14):
    run = p.add_run(text)
    run.bold = bold
    run.font.size = Pt(size)

def build_docx(
    otel_adi: str,
    iletisim: str,
    baslangic: date,
    tekrar_gunu: int,
    images: List[Tuple[str, bytes]],
) -> bytes:
    """
    Şablonunuz: her sayfa = 1 görsel
     - Üstte "Paylaşım Tarihi: gg.aa.yyyy"
     - Altında: görsele UYGUN açıklama (Gemini veya fallback)
     - Altında: Otel iletişim bilgisi
     - Altında: Hashtagler
    """
    doc = Document()

    # Sayfa kenarları biraz dar
    for section in doc.sections:
        section.left_margin = Inches(0.6)
        section.right_margin = Inches(0.6)
        section.top_margin = Inches(0.6)
        section.bottom_margin = Inches(0.7)

    current_date = baslangic

    for idx, (filename, blob) in enumerate(images):
        if idx > 0:
            doc.add_page_break()

        # Başlık: Paylaşım Tarihi
        p = doc.add_paragraph()
        add_heading(p, f"Paylaşım Tarihi: {current_date.strftime('%d.%m.%Y')}", bold=True, size=14)

        # Görsel: genişliğe sığdır
        image_stream = io.BytesIO(blob)
        try:
            im = Image.open(image_stream)
            im_format = im.format
            image_stream.seek(0)
        except Exception:
            # Görsel açılamazsa atla
            continue

        # Genişliği 6.0 inch gibi verelim (sayfaya yakışır)
        doc.add_picture(image_stream, width=Inches(6.0))

        # Açıklama + Hashtag (Gemini veya fallback)
        desc, hashtags = try_gemini_generate(otel_adi, blob)

        # Açıklama
        p2 = doc.add_paragraph()
        add_heading(p2, "Açıklama:", bold=True, size=12)
        doc.add_paragraph(desc)

        # İletişim
        p3 = doc.add_paragraph()
        add_heading(p3, "Otel İletişim:", bold=True, size=12)
        doc.add_paragraph(iletisim or "")

        # Hashtag
        p4 = doc.add_paragraph()
        add_heading(p4, "Hashtag:", bold=True, size=12)
        doc.add_paragraph(hashtags)

        # Sonraki paylaşım tarihi
        current_date = current_date + timedelta(days=max(1, tekrar_gunu))

    out = io.BytesIO()
    doc.save(out)
    out.seek(0)
    return out.read()

# -----------------------------------------------------------------------------
# Rotalar
# -----------------------------------------------------------------------------
@app.get("/health")
def health():
    return PlainTextResponse("ok")

@app.get("/")
def root(request: Request):
    if current_user(request):
        return RedirectResponse(url="/plan", status_code=HTTP_302_FOUND)
    return RedirectResponse(url="/login", status_code=HTTP_302_FOUND)

@app.get("/login")
def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "app_name": APP_NAME})

@app.post("/login")
def login_post(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    user_info = verify_user(username, password)
    if not user_info:
        # Hata mesajını çok basit bırakalım
        return templates.TemplateResponse(
            "login.html",
            {
                "request": request,
                "app_name": APP_NAME,
                "error": "Kullanıcı adı veya şifre hatalı.",
            },
            status_code=400,
        )
    # Oturum aç
    request.session["user"] = {"id": user_info[0], "username": user_info[1], "role": user_info[2]}
    return RedirectResponse(url="/plan", status_code=HTTP_303_SEE_OTHER)

@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=HTTP_303_SEE_OTHER)

@app.get("/plan")
def plan_page(request: Request):
    if not require_login(request):
        return RedirectResponse(url="/login", status_code=HTTP_302_FOUND)
    return templates.TemplateResponse("plan.html", {"request": request, "app_name": APP_NAME})

@app.post("/generate")
def generate_doc(
    request: Request,
    otel_adi: str = Form(...),
    iletisim: str = Form(""),
    baslangic_tarihi: str = Form(...),
    tekrar_gunu: int = Form(1),
    dosya_adi: str = Form("Instagram_Plani.docx"),
    images: List[UploadFile] = File(...),
):
    if not require_login(request):
        return RedirectResponse(url="/login", status_code=HTTP_302_FOUND)

    # Tarih parse
    try:
        baslangic = datetime.strptime(baslangic_tarihi, "%Y-%m-%d").date()
    except Exception:
        baslangic = date.today()

    # Görselleri oku
    img_list: List[Tuple[str, bytes]] = []
    for uf in images:
        blob = uf.file.read()
        if blob:
            img_list.append((uf.filename, blob))

    if not img_list:
        return PlainTextResponse("Görsel yüklenmedi.", status_code=400)

    content = build_docx(
        otel_adi=otel_adi,
        iletisim=iletisim,
        baslangic=baslangic,
        tekrar_gunu=int(max(1, tekrar_gunu)),
        images=img_list,
    )

    filename = safe_filename(dosya_adi)
    headers = {
        "Content-Disposition": f'attachment; filename="{filename}"'
    }
    return StreamingResponse(io.BytesIO(content), media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document", headers=headers)

@app.get("/admin")
def admin_page(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=HTTP_302_FOUND)
    if user.get("role") != "admin":
        return RedirectResponse(url="/plan", status_code=HTTP_302_FOUND)
    rows = list_users()
    return templates.TemplateResponse("admin.html", {"request": request, "rows": rows, "app_name": APP_NAME})
