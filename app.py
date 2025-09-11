import os
import io
import sqlite3
from datetime import datetime, timedelta
from typing import List, Optional

from fastapi import FastAPI, Request, Form, UploadFile, File, HTTPException
from fastapi.responses import RedirectResponse, StreamingResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from starlette.status import HTTP_303_SEE_OTHER

from PIL import Image, ImageOps
from docx import Document
from docx.shared import Inches

# ---------- Config ----------
APP_NAME = os.getenv("APP_NAME", "Otel Planlama Stüdyosu")
SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-this")
USERS_DB = os.getenv("USERS_DB_PATH", "/tmp/users.db")
USAGE_DB_PATH = os.getenv("USAGE_DB_PATH", "/tmp/usage.db")  # sadece log için
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY") or os.getenv("GeminiAPI")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")

# HEIC/HEIF desteği
HEIF_OK = False
try:
    from pillow_heif import register_heif_opener  # type: ignore
    register_heif_opener()
    HEIF_OK = True
except Exception:
    HEIF_OK = False

# Gemini opsiyonel
GEMINI_OK = False
try:
    import google.generativeai as genai  # type: ignore
    if GEMINI_API_KEY:
        genai.configure(api_key=GEMINI_API_KEY)
        GEMINI_OK = True
except Exception:
    GEMINI_OK = False

# ---------- App ----------
app = FastAPI(title=APP_NAME)
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, same_site="lax")

os.makedirs("static/css", exist_ok=True)
os.makedirs("static/js", exist_ok=True)
os.makedirs("templates", exist_ok=True)

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")
templates.env.globals["app_name"] = APP_NAME

# ---------- DB (users) ----------
def db():
    conn = sqlite3.connect(USERS_DB)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE,
            password TEXT,
            role TEXT DEFAULT 'user',
            created_at TEXT
        )"""
    )
    # default admin
    cur = conn.execute("SELECT COUNT(*) FROM users WHERE role='admin'")
    if cur.fetchone()[0] == 0:
        admin_user = os.getenv("ADMIN_CODE_USER", "admin")
        admin_pass = os.getenv("ADMIN_CODE_PASS", "admin123")
        conn.execute(
            "INSERT OR IGNORE INTO users(username,password,role,created_at) VALUES(?,?,?,?)",
            (admin_user, admin_pass, "admin", datetime.utcnow().isoformat()),
        )
        conn.commit()
    return conn

# ---------- Helpers ----------
def get_user(req: Request):
    return req.session.get("user")

def require_login(req: Request) -> Optional[RedirectResponse]:
    u = get_user(req)
    if not u:
        return RedirectResponse(url="/login", status_code=HTTP_303_SEE_OTHER)
    return None

def require_admin(req: Request) -> Optional[RedirectResponse]:
    u = get_user(req)
    if not u or u.get("role") != "admin":
        return RedirectResponse(url="/login", status_code=HTTP_303_SEE_OTHER)
    return None

def guess_extension(content_type: str) -> str:
    if not content_type:
        return ".jpg"
    if "png" in content_type:
        return ".png"
    if "webp" in content_type:
        return ".webp"
    if "heic" in content_type or "heif" in content_type:
        return ".heic"
    return ".jpg"

def open_image_safely(data: bytes) -> Image.Image:
    """
    Her tür görüntüyü Pillow ile aç; EXIF düzeltmesi yap;
    alfa varsa beyaza merge et.
    """
    img = Image.open(io.BytesIO(data))
    try:
        img = ImageOps.exif_transpose(img)
    except Exception:
        pass
    if img.mode in ("RGBA", "LA"):
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[-1])
        img = bg
    elif img.mode != "RGB":
        img = img.convert("RGB")
    return img

def add_image_to_doc(doc: Document, data: bytes, max_width_inch: float = 6.0):
    img = open_image_safely(data)
    # genişlik sınırla
    w, h = img.size
    max_width_px = int(max_width_inch * 96)  # 96 dpi
    if w > max_width_px:
        ratio = max_width_px / float(w)
        new_size = (max_width_px, int(h * ratio))
        img = img.resize(new_size, Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=88)
    buf.seek(0)
    doc.add_picture(buf, width=Inches(max_width_inch))

EMOJIS = ["✨", "🏖️", "🌅", "🌊", "🌿", "🧳", "📷", "🌞", "💫", "🍹", "🌙", "🏨", "💙"]

def fallback_caption_and_tags(index: int, hotel_info: str) -> (str, str):
    # Basit, emojili yerel üretim
    base = [
        "Denizin sesi, gün batımının sıcaklığı 🌅🌊",
        "Yeni anılar biriktirme zamanı 🧳✨",
        "Sakinlik ve konfor bir arada 🏨🌿",
        "Tatil modunu açma vakti 🌞🍹",
        "Fotoğraf gibi kareler 📷💫",
    ]
    text = base[index % len(base)]
    if hotel_info:
        text += f"  {hotel_info}"
    tags = "#otel #tatil #konfor #deniz #günbatımı #holiday #travel #relax"
    return text, tags

async def gemini_caption_and_tags(image_bytes: bytes, hotel_info: str) -> (str, str):
    if not GEMINI_OK:
        return fallback_caption_and_tags(0, hotel_info)
    try:
        model = genai.GenerativeModel(GEMINI_MODEL)
        prompt = (
            "Aşağıdaki fotoğrafı analiz et ve görsele TAM olarak uygun, "
            "Türkçe, 1-2 cümlelik, sıcak ve çekici bir Instagram açıklaması yaz. "
            "Açıklama mutlaka EMOJİ içersin. "
            "Ardından en sona tek satırda 8-12 kısa hashtag yaz (mekân ve tatil odaklı). "
            "Hiçbir başlık (Açıklama/Hashtag) kullanma, sadece iki satır üret: "
            "1. satır açıklama (emojili), 2. satır hashtagler. "
            f"Otelle ilgili şu bilgiyi doğalca açıklamaya yedir: {hotel_info or '—'}"
        )
        img_part = {"mime_type": "image/jpeg", "data": image_bytes}
        res = await model.generate_content_async([prompt, img_part])
        text = (res.text or "").strip()
        # iki satıra ayırmaya çalış
        parts = [p.strip() for p in text.splitlines() if p.strip()]
        if len(parts) >= 2:
            cap = parts[0]
            tags = parts[1]
        else:
            cap, tags = fallback_caption_and_tags(0, hotel_info)
        # emojisiz kaldıysa min 1 emoji enjekte et
        if not any(e in cap for e in EMOJIS):
            cap = cap + " " + EMOJIS[0]
        return cap, tags
    except Exception:
        return fallback_caption_and_tags(0, hotel_info)

def write_plan_docx(
    images: List[bytes],
    start_date: datetime,
    every_n_days: int,
    hotel_info: str,
    title: str,
    captions: List[str],
    tags_list: List[str],
) -> bytes:
    """
    Her sayfa:
      [Paylaşım Tarihi]
      [Görsel]
      [Açıklama (emojili, başlıksız)]
      [Otel iletişim (başlıksız)]
      [Hashtag (tek satır, başlıksız)]
    """
    doc = Document()
    for i, img in enumerate(images):
        share_date = (start_date + timedelta(days=i * every_n_days)).strftime("%d.%m.%Y")
        # Tarih (üstte)
        p_date = doc.add_paragraph(share_date)
        p_date.runs[0].bold = True

        # Görsel
        add_image_to_doc(doc, img, max_width_inch=6.0)
        doc.add_paragraph("")  # boşluk

        # Açıklama
        doc.add_paragraph(captions[i])

        # Otel iletişim (etiketsiz)
        if hotel_info:
            doc.add_paragraph(hotel_info)

        # Hashtag (tek satır)
        doc.add_paragraph(tags_list[i])

        # Sayfa sonu (son sayfa hariç)
        if i < len(images) - 1:
            doc.add_page_break()

    bio = io.BytesIO()
    doc.save(bio)
    bio.seek(0)
    return bio.read()

# ---------- Routes ----------
@app.get("/health")
def health():
    return {"ok": True}

@app.get("/", response_class=HTMLResponse)
def root(req: Request):
    u = get_user(req)
    if not u:
        return RedirectResponse("/login", status_code=HTTP_303_SEE_OTHER)
    return RedirectResponse("/plan", status_code=HTTP_303_SEE_OTHER)

@app.get("/login", response_class=HTMLResponse)
def login_page(req: Request):
    return templates.TemplateResponse("login.html", {"request": req})

@app.post("/login")
def do_login(
    req: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    with db() as conn:
        cur = conn.execute("SELECT id, username, password, role FROM users WHERE username=?", (username,))
        row = cur.fetchone()
    if row and password == row[2]:
        req.session["user"] = {"id": row[0], "username": row[1], "role": row[3]}
        return RedirectResponse("/plan", status_code=HTTP_303_SEE_OTHER)
    return templates.TemplateResponse("login.html", {"request": req, "error": "Kullanıcı adı veya şifre hatalı."})

@app.get("/logout")
def logout(req: Request):
    req.session.clear()
    return RedirectResponse("/login", status_code=HTTP_303_SEE_OTHER)

@app.get("/admin", response_class=HTMLResponse)
def admin_page(req: Request):
    guard = require_admin(req)
    if guard:
        return guard
    with db() as conn:
        users = [
            {"id": r[0], "username": r[1], "role": r[3], "created_at": r[4]}
            for r in conn.execute("SELECT id,username,password,role,created_at FROM users ORDER BY id DESC")
        ]
    return templates.TemplateResponse("admin.html", {"request": req, "users": users})

@app.post("/admin/create-user")
def admin_create_user(
    req: Request,
    username: str = Form(...),
    password: str = Form(...),
    role: str = Form("user"),
):
    guard = require_admin(req)
    if guard:
        return guard
    with db() as conn:
        try:
            conn.execute(
                "INSERT INTO users(username,password,role,created_at) VALUES(?,?,?,?)",
                (username, password, role, datetime.utcnow().isoformat()),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            return templates.TemplateResponse("admin.html", {"request": req, "error": "Bu kullanıcı zaten var."})
    return RedirectResponse("/admin", status_code=HTTP_303_SEE_OTHER)

@app.get("/plan", response_class=HTMLResponse)
def plan_page(req: Request):
    guard = require_login(req)
    if guard:
        return guard
    return templates.TemplateResponse("plan.html", {"request": req})

@app.post("/api/plan")
async def api_plan(
    req: Request,
    doc_title: str = Form("Instagram_Plani"),
    start_date: str = Form(...),          # YYYY-MM-DD
    every_n_days: int = Form(1),
    hotel_contact: str = Form(""),
    files: List[UploadFile] = File(...),
):
    guard = require_login(req)
    if guard:
        return guard

    # Görselleri oku (orijinal sırayı koru)
    images_bytes: List[bytes] = []
    for f in files:
        data = await f.read()
        # HEIC/HEIF -> Pillow zaten açacak, docx için JPEG’e dönüştürme add_image_to_doc içinde
        images_bytes.append(data)

    if not images_bytes:
        raise HTTPException(400, "Görsel yüklenmedi.")

    # Tarih
    try:
        start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(400, "Geçersiz başlangıç tarihi.")

    # Başlık/isim
    safe_title = "".join(ch for ch in doc_title if ch.isalnum() or ch in ("_", "-", " ")).strip() or "Instagram_Plani"
    filename = f"{safe_title}.docx"

    # Açıklama + hashtag üretimi (emojili) her görsel için
    captions: List[str] = []
    tags_list: List[str] = []
    for idx, img in enumerate(images_bytes):
        # Gemini varsa multimodal
        if GEMINI_OK:
            cap, tags = await gemini_caption_and_tags(img, hotel_contact)
        else:
            cap, tags = fallback_caption_and_tags(idx, hotel_contact)
        # Başlık YOK: direkt metin
        # Emojisiz kalırsa garanti et:
        if not any(e in cap for e in EMOJIS):
            cap += " " + EMOJIS[idx % len(EMOJIS)]
        captions.append(cap)
        tags_list.append(tags)

    # DOCX oluştur
    doc_bytes = write_plan_docx(
        images=images_bytes,
        start_date=start_dt,
        every_n_days=every_n_days,
        hotel_info=hotel_contact.strip(),
        title=safe_title,
        captions=captions,
        tags_list=tags_list,
    )

    return StreamingResponse(
        io.BytesIO(doc_bytes),
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"'
        },
    )
