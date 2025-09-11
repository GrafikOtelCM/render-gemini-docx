import os
import io
import sqlite3
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

from fastapi import FastAPI, Request, Form, UploadFile, File, HTTPException
from fastapi.responses import RedirectResponse, StreamingResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from starlette.status import HTTP_303_SEE_OTHER

from PIL import Image, ImageOps
from docx import Document
from docx.shared import Inches, Pt
from docx.oxml.ns import qn

# ---------- Config ----------
APP_NAME = os.getenv("APP_NAME", "Otel Planlama Stüdyosu")
SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-this")
USERS_DB = os.getenv("USERS_DB_PATH", "/tmp/users.db")
USAGE_DB_PATH = os.getenv("USAGE_DB_PATH", "/tmp/usage.db")
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

# Basit emoji seti
EMOJIS = ["✨", "🏖️", "🌅", "🌊", "🌿", "🧳", "📷", "🌞", "💫", "🍹", "🌙", "🏨", "💙"]

def fallback_caption_and_tags(index: int, hotel_info: str) -> Tuple[str, str]:
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
    tags = "#otel #tatil #konfor #deniz #günbatımı #holiday #travel #relax #booking #getaway"
    return text, tags

async def gemini_caption_and_tags(image_bytes: bytes, hotel_info: str) -> Tuple[str, str]:
    if not GEMINI_OK:
        return fallback_caption_and_tags(0, hotel_info)
    try:
        model = genai.GenerativeModel(GEMINI_MODEL)
        prompt = (
            "Fotoğrafı analiz et ve görsele uygun, TÜRKÇE, 1–2 cümlelik KISA bir Instagram açıklaması yaz. "
            "Açıklama mutlaka EMOJİ içersin. Maksimum ~220 karakter. "
            "Sonraki satırda tek satırlık 8–10 hashtag üret. "
            "Hiçbir başlık (Açıklama/Hashtag) yazma; sadece iki satır: 1) açıklama 2) hashtagler. "
            f"Otelle ilgili bilgiyi doğalca açıklamaya yedir: {hotel_info or '—'}"
        )
        img_part = {"mime_type": "image/jpeg", "data": image_bytes}
        res = await model.generate_content_async([prompt, img_part])
        text = (res.text or "").strip()
        parts = [p.strip() for p in text.splitlines() if p.strip()]
        if len(parts) >= 2:
            cap = parts[0]
            tags = parts[1]
        else:
            cap, tags = fallback_caption_and_tags(0, hotel_info)
        # güvenlik: çok uzun hashtag tek satırda kalsın
        if " " in tags:
            tag_tokens = [t for t in tags.split() if t.startswith("#")]
            tag_tokens = tag_tokens[:10]
            tags = " ".join(tag_tokens) if tag_tokens else tags
        if not any(e in cap for e in EMOJIS):
            cap = cap + " " + EMOJIS[0]
        return cap, tags
    except Exception:
        return fallback_caption_and_tags(0, hotel_info)

# ---- Yerleşim yardımcıları ----
def set_default_doc_styling(doc: Document):
    style = doc.styles["Normal"]
    font = style.font
    font.size = Pt(10)
    font.name = "Calibri"
    # Türkçe dil bilgisi (opsiyonel)
    try:
        style.element.rPr.rPrChange.rPr.set(qn("w:lang"), None)  # temizle
    except Exception:
        pass

def estimate_lines(text: str, chars_per_line: int) -> int:
    text = " ".join(text.split())  # satır sonlarını tek boşluğa indir
    if not text:
        return 0
    import math
    return max(1, math.ceil(len(text) / max(10, chars_per_line)))

def compute_picture_size_for_page(
    img_w_px: int,
    img_h_px: int,
    text_lines_total: int,
    page_width_in: float,
    page_height_in: float,
    margin_left_in: float,
    margin_right_in: float,
    margin_top_in: float,
    margin_bottom_in: float,
    line_height_in: float = 0.1667,  # ~12pt
    top_block_in: float = 0.15,      # tarih satırı ve küçük boşluk
    bottom_buffer_in: float = 0.12,  # sayfa taşmasını kesin önleme payı
) -> Tuple[float, float]:
    """
    Resmin genişlik/yüksekliğini (inch) döndürür; oran korunur, metin için yer bırakılır.
    """
    text_height_in = text_lines_total * line_height_in
    available_h_in = max(
        0.5,
        page_height_in - margin_top_in - margin_bottom_in - text_height_in - top_block_in - bottom_buffer_in
    )

    # metin sütunu genişliği (resmi bununla başlat)
    text_width_in = max(3.0, page_width_in - margin_left_in - margin_right_in)

    # Resmin bu genişlikteki doğal yüksekliği
    natural_h_in = (img_h_px / img_w_px) * text_width_in

    if natural_h_in <= available_h_in:
        return text_width_in, natural_h_in

    # Sığmıyorsa, aynı oranla küçült
    scale = available_h_in / natural_h_in
    final_w_in = max(3.0, text_width_in * scale)
    final_h_in = (img_h_px / img_w_px) * final_w_in
    return final_w_in, final_h_in

def add_text_paragraph(doc: Document, text: str, bold: bool = False):
    p = doc.add_paragraph(text)
    if p.runs:
        p.runs[0].bold = bold
    fmt = p.paragraph_format
    fmt.space_before = Pt(0)
    fmt.space_after = Pt(2)
    fmt.line_spacing = 1.0
    return p

def add_image_with_fit(doc: Document, img_bytes: bytes, width_in: float, height_in: float):
    # PIL ile yeniden örnekleyip ekle (kalite kontrolü)
    img = open_image_safely(img_bytes)
    target_w_px = int(width_in * 96)
    target_h_px = int(height_in * 96)
    img = img.resize((target_w_px, target_h_px), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=88)
    buf.seek(0)
    doc.add_picture(buf, width=Inches(width_in), height=Inches(height_in))

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
      [Tarih]
      [Görsel - oran korunarak kalan yüksekliğe sığdırılır]
      [Açıklama (emojili, başlıksız)]
      [Otel iletişim (başlıksız)]
      [Hashtag (tek satır, başlıksız)]
    Tamamı tek sayfada kalacak şekilde resim boyutu dinamik ayarlanır.
    """
    doc = Document()
    set_default_doc_styling(doc)

    # Kenar boşlukları
    sect = doc.sections[0]
    sect.top_margin = Inches(0.5)
    sect.bottom_margin = Inches(0.5)
    sect.left_margin = Inches(0.6)
    sect.right_margin = Inches(0.6)

    page_w_in = sect.page_width.inches
    page_h_in = sect.page_height.inches
    ml = sect.left_margin.inches
    mr = sect.right_margin.inches
    mt = sect.top_margin.inches
    mb = sect.bottom_margin.inches

    # Karakter/satır tahmini (10pt için ~12 cpi * metin genişliği)
    text_width_in = page_w_in - ml - mr
    chars_per_line = max(40, int(text_width_in * 12))

    for i, raw in enumerate(images):
        share_date = (start_date + timedelta(days=i * every_n_days)).strftime("%d.%m.%Y")
        caption = " ".join(captions[i].split())
        tags = " ".join(tags_list[i].split())
        # hashtag güvenliği: tek satırda kalsın
        tag_tokens = [t for t in tags.split() if t.startswith("#")]
        tag_tokens = tag_tokens[:10]
        tags = " ".join(tag_tokens) if tag_tokens else tags

        # metin satır tahmini
        total_text_lines = 1  # tarih
        total_text_lines += estimate_lines(caption, chars_per_line)
        if hotel_info:
            total_text_lines += estimate_lines(hotel_info, chars_per_line)
        total_text_lines += 1  # hashtag tek satır varsayıyoruz

        # resim için boyut hesabı
        img = open_image_safely(raw)
        pic_w_in, pic_h_in = compute_picture_size_for_page(
            img_w_px=img.size[0],
            img_h_px=img.size[1],
            text_lines_total=total_text_lines,
            page_width_in=page_w_in,
            page_height_in=page_h_in,
            margin_left_in=ml,
            margin_right_in=mr,
            margin_top_in=mt,
            margin_bottom_in=mb,
        )

        # içerik yazımı
        add_text_paragraph(doc, share_date, bold=True)
        add_image_with_fit(doc, raw, pic_w_in, pic_h_in)
        add_text_paragraph(doc, caption, bold=False)
        if hotel_info:
            add_text_paragraph(doc, hotel_info, bold=False)
        add_text_paragraph(doc, tags, bold=False)

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
    start_date: str = Form(...),
    every_n_days: int = Form(1),
    hotel_contact: str = Form(""),
    files: List[UploadFile] = File(...),
):
    guard = require_login(req)
    if guard:
        return guard

    images_bytes: List[bytes] = []
    for f in files:
        data = await f.read()
        images_bytes.append(data)
    if not images_bytes:
        raise HTTPException(400, "Görsel yüklenmedi.")

    try:
        start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(400, "Geçersiz başlangıç tarihi.")

    safe_title = "".join(ch for ch in doc_title if ch.isalnum() or ch in ("_", "-", " ")).strip() or "Instagram_Plani"
    filename = f"{safe_title}.docx"

    captions: List[str] = []
    tags_list: List[str] = []
    for idx, img in enumerate(images_bytes):
        if GEMINI_OK:
            cap, tags = await gemini_caption_and_tags(img, hotel_contact)
        else:
            cap, tags = fallback_caption_and_tags(idx, hotel_contact)
        if not any(e in cap for e in EMOJIS):
            cap += " " + EMOJIS[idx % len(EMOJIS)]
        # Hashtag tek satır güvenliği
        tag_tokens = [t for t in tags.split() if t.startswith("#")]
        tags = " ".join(tag_tokens[:10]) if tag_tokens else tags
        captions.append(cap)
        tags_list.append(tags)

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
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
