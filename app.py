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

# ---------- Config ----------
APP_NAME = os.getenv("APP_NAME", "Otel Planlama Stüdyosu")
SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-this")
USERS_DB = os.getenv("USERS_DB_PATH", "/tmp/users.db")
USAGE_DB_PATH = os.getenv("USAGE_DB_PATH", "/tmp/usage.db")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY") or os.getenv("GeminiAPI")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")

# HEIC/HEIF desteği
try:
    from pillow_heif import register_heif_opener  # type: ignore
    register_heif_opener()
except Exception:
    pass

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

def fallback_caption_and_tags(index: int) -> Tuple[str, str]:
    base = [
        "Denizin sesi, gün batımının sıcaklığı 🌅🌊",
        "Yeni anılar biriktirme zamanı 🧳✨",
        "Sakinlik ve konfor bir arada 🏨🌿",
        "Tatil modunu açma vakti 🌞🍹",
        "Fotoğraf gibi kareler 📷💫",
    ]
    text = base[index % len(base)]
    tags = "#otel #tatil #konfor #deniz #holiday #travel #relax #booking #getaway #sunset"
    # 8 tag’a indir
    tag_tokens = [t for t in tags.split() if t.startswith("#")][:5]
    return text, " ".join(tag_tokens)

async def gemini_caption_and_tags(image_bytes: bytes) -> Tuple[str, str]:
    # Otel iletişim bilgisi ASLA karıştırılmıyor
    if not GEMINI_OK:
        return fallback_caption_and_tags(0)
    try:
        model = genai.GenerativeModel(GEMINI_MODEL)
        prompt = (
            "Bu fotoğraf için TÜRKÇE, görsele uygun ve EMOJİLİ, 1–2 cümlelik KISA bir Instagram açıklaması üret. "
            "En fazla ~220 karakter. İletişim bilgisi veya başlık (Açıklama/Hashtag) yazma.\n"
            "Ayrıca ikinci satırda 8 kısa hashtag ver. Sadece iki satır döndür:\n"
            "1) açıklama\n2) hashtagler (8 adet, tek satır)."
        )
        img_part = {"mime_type": "image/jpeg", "data": image_bytes}
        res = await model.generate_content_async([prompt, img_part])
        text = (res.text or "").strip()
        parts = [p.strip() for p in text.splitlines() if p.strip()]
        if len(parts) >= 2:
            cap = parts[0]
            tags = parts[1]
        else:
            cap, tags = fallback_caption_and_tags(0)
        # hashtagleri 8’e indir ve tek satır
        tag_tokens = [t for t in tags.split() if t.startswith("#")][:8]
        tags = " ".join(tag_tokens) if tag_tokens else tags
        if not any(e in cap for e in EMOJIS):
            cap = cap + " " + EMOJIS[0]
        return cap, tags
    except Exception:
        return fallback_caption_and_tags(0)

# ---- Yerleşim yardımcıları ----
def set_default_doc_styling(doc: Document, base_pt: float = 10.0):
    style = doc.styles["Normal"]
    style.font.size = Pt(base_pt)
    style.font.name = "Calibri"

def estimate_lines(text: str, chars_per_line: int) -> int:
    text = " ".join(text.split())
    if not text:
        return 0
    import math
    return max(1, math.ceil(len(text) / max(10, chars_per_line)))

def compute_fit(
    img_px: Tuple[int, int],
    caption: str,
    hotel_info: str,
    hashtags: str,
    page_w_in: float,
    page_h_in: float,
    ml: float, mr: float, mt: float, mb: float,
    font_pt: float,
) -> Tuple[float, float, bool]:
    """
    Resmi mümkün olan en genişte başlatır (metin sütunu genişliği),
    sığmazsa %5 kademe ile küçültür. Gerekirse small_text=True (9pt).
    Dönen: (width_in, height_in, small_text)
    """
    # satır yüksekliği ~ pt / 72 inch
    line_h_in = (font_pt / 72.0) * 1.05  # %5 güvenlik
    text_w_in = max(3.0, page_w_in - ml - mr)

    def total_text_lines(chars_per_line: int) -> int:
        # tarih 1 satır
        ln = 1
        ln += estimate_lines(caption, chars_per_line)
        if hotel_info:
            ln += estimate_lines(hotel_info, chars_per_line)
        # hashtag tek satır hedef; yine de tahminde 1 al
        ln += 1
        return ln

    # Karakter/satır kaba tahmin: ~12 cpi @10pt; @9pt biraz daha fazla
    cpi = 12.0 * (font_pt / 10.0)  # 9pt ~10.8
    chars_per_line = int(max(40, text_w_in * cpi))
    lines = total_text_lines(chars_per_line)

    # üst blok (tarih ve az boşluk) + altta tampon
    top_block_in = line_h_in * 1.2
    bottom_buf_in = 0.12
    avail_h = max(
        0.5,
        page_h_in - mt - mb - (lines * line_h_in) - top_block_in - bottom_buf_in
    )

    # hedef genişlik ve doğal yükseklik
    img_w_px, img_h_px = img_px
    w_in = text_w_in
    h_in = (img_h_px / img_w_px) * w_in

    # büyüksen kademeli küçült
    min_w_in = 2.2  # son çare
    while (h_in > avail_h) and (w_in > min_w_in):
        w_in *= 0.95
        h_in = (img_h_px / img_w_px) * w_in

    # hâlâ sığmıyorsa küçük yazı ile tekrar hesapla (9pt)
    small_text = False
    if h_in > avail_h:
        small_text = True
        font_pt2 = 9.0
        line_h_in2 = (font_pt2 / 72.0) * 1.05
        cpi2 = 12.0 * (font_pt2 / 10.0)
        chars_per_line2 = int(max(44, text_w_in * cpi2))
        lines2 = 1 + estimate_lines(caption, chars_per_line2) + (estimate_lines(hotel_info, chars_per_line2) if hotel_info else 0) + 1
        avail_h2 = max(0.5, page_h_in - mt - mb - (lines2 * line_h_in2) - top_block_in - bottom_buf_in)
        # görseli tekrar küçültme döngüsü
        w2 = min(w_in, text_w_in)
        h2 = (img_h_px / img_w_px) * w2
        while (h2 > avail_h2) and (w2 > min_w_in):
            w2 *= 0.95
            h2 = (img_h_px / img_w_px) * w2
        w_in, h_in = w2, h2

    return w_in, h_in, small_text

def add_text_paragraph(doc: Document, text: str, bold: bool = False, pt: float = 10.0):
    p = doc.add_paragraph()
    run = p.add_run(text)
    run.bold = bold
    run.font.size = Pt(pt)
    run.font.name = "Calibri"
    fmt = p.paragraph_format
    fmt.space_before = Pt(0)
    fmt.space_after = Pt(1.5)
    fmt.line_spacing = 1.0
    return p

def add_image_resized(doc: Document, img_bytes: bytes, width_in: float, height_in: float):
    img = open_image_safely(img_bytes)
    target_w_px = max(1, int(width_in * 96))
    target_h_px = max(1, int(height_in * 96))
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
      [Görsel]
      [Açıklama (emoji, başlıksız)]
      [Otel iletişim (ayrı satır)]
      [Hashtag (tek satır, 8 tag)]
    Hepsi **tek sayfa**.
    """
    doc = Document()

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

    base_font_pt = 10.0

    for i, raw in enumerate(images):
        share_date = (start_date + timedelta(days=i * every_n_days)).strftime("%d.%m.%Y")
        caption = " ".join(captions[i].split())
        tags = " ".join(tags_list[i].split())
        # hashtagleri 8’e indir
        tag_tokens = [t for t in tags.split() if t.startswith("#")][:8]
        tags = " ".join(tag_tokens) if tag_tokens else tags

        img = open_image_safely(raw)
        pic_w_in, pic_h_in, small_text = compute_fit(
            img_px=img.size,
            caption=caption,
            hotel_info=hotel_info,
            hashtags=tags,
            page_w_in=page_w_in,
            page_h_in=page_h_in,
            ml=ml, mr=mr, mt=mt, mb=mb,
            font_pt=base_font_pt,
        )
        pt_this = 9.0 if small_text else base_font_pt

        # içerik yazımı
        set_default_doc_styling(doc, base_pt=pt_this)
        add_text_paragraph(doc, share_date, bold=True, pt=pt_this)
        add_image_resized(doc, raw, pic_w_in, pic_h_in)
        add_text_paragraph(doc, caption, bold=False, pt=pt_this)
        if hotel_info:
            add_text_paragraph(doc, hotel_info, bold=False, pt=pt_this)
        add_text_paragraph(doc, tags, bold=False, pt=pt_this)

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
            cap, tags = await gemini_caption_and_tags(img)
        else:
            cap, tags = fallback_caption_and_tags(idx)
        if not any(e in cap for e in EMOJIS):
            cap += " " + EMOJIS[idx % len(EMOJIS)]
        # hashtagleri 8’e sabitle
        tag_tokens = [t for t in tags.split() if t.startswith("#")]
        tags = " ".join(tag_tokens[:8]) if tag_tokens else tags
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
