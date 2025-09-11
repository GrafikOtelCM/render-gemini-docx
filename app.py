import os
import io
import json
import sqlite3
import datetime as dt
from typing import List, Optional, Tuple

from fastapi import FastAPI, Request, UploadFile, Form, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.base import BaseHTTPMiddleware

from passlib.hash import bcrypt
from PIL import Image
from docx import Document
from docx.shared import Inches, Pt
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn

from dotenv import load_dotenv

# --- ENV ---
load_dotenv()

APP_NAME = os.getenv("APP_NAME", "Otel Planlama Stüdyosu")
SECRET_KEY = os.getenv("SECRET_KEY", "change-me")
USAGE_DB = os.getenv("USAGE_DB_PATH", "/tmp/usage.db")

# Gemini API key'i iki farklı isimde arıyoruz: GEMINI_API_KEY veya GeminiAPI
def _get_first_env(keys: List[str]) -> Optional[str]:
    for k in keys:
        v = os.getenv(k)
        if v and v.strip():
            return v.strip()
    return None

GEMINI_API_KEY = _get_first_env(["GEMINI_API_KEY", "GeminiAPI"])
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_MAX_CONCURRENCY = int(os.getenv("GEMINI_MAX_CONCURRENCY", "1"))

# Google Generative AI (Gemini) SDK (isteğe bağlı hata toleransı ile import)
_gemini_available = False
try:
    import google.generativeai as genai
    if GEMINI_API_KEY:
        genai.configure(api_key=GEMINI_API_KEY)
        _gemini_available = True
except Exception:
    _gemini_available = False


# --- APP ---
app = FastAPI(title=APP_NAME)

# Session
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, session_cookie="ops_session", max_age=60 * 60 * 8)

# Basit auth context göstermek için
class AuthContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request.state.user = request.session.get("user") if "session" in request.scope else None
        return await call_next(request)

app.add_middleware(AuthContextMiddleware)

# Static & Templates (varsa)
if os.path.isdir("static"):
    app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates" if os.path.isdir("templates") else ".")


# --- Basit kullanıcı deposu (sqlite /tmp) ---
def db_init():
    os.makedirs("/tmp", exist_ok=True)
    con = sqlite3.connect(USAGE_DB)
    cur = con.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE,
        password_hash TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'user',
        created_at TEXT NOT NULL
    )
    """)
    con.commit()
    # Admin tohumlama
    cur.execute("SELECT 1 FROM users WHERE username=?", (os.getenv("ADMIN_CODE_USER", "admin"),))
    if not cur.fetchone():
        u = os.getenv("ADMIN_CODE_USER", "admin")
        p = os.getenv("ADMIN_CODE_PASS", "admin123")
        cur.execute(
            "INSERT INTO users (username, password_hash, role, created_at) VALUES (?,?,?,?)",
            (u, bcrypt.hash(p), "admin", dt.datetime.utcnow().isoformat()),
        )
        con.commit()
    con.close()

@app.on_event("startup")
def _startup():
    db_init()


# --- Yardımcılar ---
def require_login(request: Request):
    user = request.session.get("user") if "session" in request.scope else None
    if not user:
        raise HTTPException(status_code=302, detail="login required")
    return user

def require_admin(request: Request):
    user = require_login(request)
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="forbidden")
    return user

def pil_from_upload(f: UploadFile) -> Tuple[Image.Image, bytes, str]:
    raw = f.file.read()
    mime = f.content_type or "image/jpeg"
    img = Image.open(io.BytesIO(raw))
    # RGB'ye çevir (docx bazı modlarda sorun çıkartır)
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGB")
    return img, raw, mime

def mm_from_inches(inch: float) -> float:
    return inch * 25.4

# --- Gemini ile görsele uygun açıklama+hashtag üretimi ---
def gemini_for_image(raw: bytes, mime: str, otel_adi: str, iletisim: str, paylasim_tarihi: str) -> dict:
    """
    Gemini'den JSON dönmesi beklenir:
    { "caption": "...", "hashtags": ["#otel", "#tatil", ...] }
    """
    # Eğer SDK yoksa ya da anahtar yoksa fallback
    if not _gemini_available or not GEMINI_API_KEY:
        return {
            "caption": f"{otel_adi} için paylaşıma hazır bir görsel. {paylasim_tarihi} tarihinde yayınlanmak üzere kısa ve sıcak bir tanıtım metni.",
            "hashtags": ["#otel", "#tatil", "#rezervasyon", "#istanbul", "#keşfet"]
        }

    prompt = (
        "Sen bir sosyal medya içerik editörüsün. Aşağıdaki otel görseli için **Türkçe**, sıcak ve satışa dönük ama samimi bir Instagram açıklaması üret.\n"
        "- Metin 2-4 cümle olsun, emoji aşırıya kaçmadan 1-3 tane kullanılabilir.\n"
        "- Otel adı mutlaka bir kez geçsin.\n"
        "- Tarih: {date} (metinde istersen çağrışım olarak kullanabilirsin, zorunlu değil).\n"
        "- Sonra 8-12 adet etkileşim odaklı **Türkçe hashtag** üret (konum/tema bazlı, spam olmayan, görsele uygun).\n"
        "- ÇIKTIYI SADECE JSON VER: {{\"caption\":\"...\",\"hashtags\":[\"#...\"]}}\n"
        "- Hashtag'lerde # işareti kullan.\n"
        f"- Otel iletişim bilgisi (bilgi amaçlı): {iletisim}\n"
    ).format(date=paylasim_tarihi)

    # SDK: image + text istemi
    model = genai.GenerativeModel(GEMINI_MODEL)
    try:
        resp = model.generate_content(
            [
                {"mime_type": mime, "data": raw},
                prompt
            ],
            request_options={"timeout": 60}
        )
        txt = (resp.text or "").strip()
        # Bazı modeller ```json blokları döndürür; temizleyelim
        if txt.startswith("```"):
            txt = txt.strip("`")
            # olası "json\n{...}"
            parts = txt.split("\n", 1)
            if len(parts) == 2 and parts[0].lower().strip() == "json":
                txt = parts[1].strip()
        data = json.loads(txt)
        caption = str(data.get("caption", "")).strip()
        hashtags = data.get("hashtags") or []
        if isinstance(hashtags, str):
            hashtags = [h.strip() for h in hashtags.split() if h.strip().startswith("#")]
        hashtags = [h if h.startswith("#") else f"#{h}" for h in hashtags][:15]
        if not caption:
            caption = f"{otel_adi} ile konforlu bir kaçamak. Rezervasyon için bize ulaşın!"
        if not hashtags:
            hashtags = ["#otel", "#tatil", "#rezervasyon"]
        return {"caption": caption, "hashtags": hashtags}
    except Exception:
        # Fallback
        return {
            "caption": f"{otel_adi}’de keyif dolu bir konaklama için seni bekliyoruz. Erken rezervasyon fırsatlarını kaçırma!",
            "hashtags": ["#otel", "#tatil", "#rezervasyon", "#haftasonu", "#keşfet"]
        }


# --- DOCX inşa ---
def build_docx(
    otel_adi: str,
    iletisim: str,
    baslangic_tarihi: str,
    tekrar_gunu: int,
    dosya_adi: str,
    images: List[Tuple[Image.Image, bytes, str]]
) -> io.BytesIO:

    doc = Document()

    # Varsayılan font
    style = doc.styles['Normal']
    style.font.name = 'Calibri'
    style._element.rPr.rFonts.set(qn('w:eastAsia'), 'Calibri')
    style.font.size = Pt(10.5)

    # Tarih üretici
    try:
        start = dt.datetime.strptime(baslangic_tarihi, "%Y-%m-%d")
    except ValueError:
        start = dt.datetime.utcnow()

    current_date = start

    for idx, (img, raw, mime) in enumerate(images, start=1):
        # Sayfa başına düzen
        # Üstte tarih
        p_date = doc.add_paragraph(current_date.strftime("%d.%m.%Y"))
        p_date.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p_date.runs[0].bold = True

        # Görsel: sayfayı taşırmadan tek görsel
        # A4 genişlik ~6.5 inç iç kenar; 5.5 inç hedefleyelim
        max_width_in = 5.5
        bio = io.BytesIO()
        # DOCX için jpg daha stabil
        img_to_save = img.convert("RGB")
        img_to_save.save(bio, format="JPEG", quality=90)
        bio.seek(0)
        doc.add_picture(bio, width=Inches(max_width_in))

        # Gemini'den açıklama + hashtag
        ai = gemini_for_image(raw, mime, otel_adi, iletisim, current_date.strftime("%d.%m.%Y"))

        # Açıklama
        p_caption = doc.add_paragraph(ai["caption"])
        p_caption.alignment = WD_ALIGN_PARAGRAPH.LEFT

        # Otel iletişim (her sayfada)
        if iletisim.strip():
            p_contact = doc.add_paragraph(iletisim.strip())
            p_contact.alignment = WD_ALIGN_PARAGRAPH.LEFT

        # Hashtagler
        if ai["hashtags"]:
            p_hash = doc.add_paragraph(" ".join(ai["hashtags"]))
            p_hash.alignment = WD_ALIGN_PARAGRAPH.LEFT

        # Sayfa sonu
        doc.add_page_break()

        # Bir sonraki tarih
        current_date = current_date + dt.timedelta(days=max(1, int(tekrar_gunu)))

    # Son sayfadaki boş page break'ı yumuşak bırakalım (zorunlu değil)
    # (docx kütüphanesi kolay kaldırmaya izin vermiyor; sorun değil.)

    buf = io.BytesIO()
    doc.save(buf)
    buf.seek(0)
    return buf


# --- ROUTES ---

@app.get("/health", response_class=PlainTextResponse)
def health():
    return "ok"

@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    # login yoksa login sayfasına
    if not request.session.get("user"):
        return RedirectResponse("/login")
    return RedirectResponse("/plan")

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    # Basit HTML (eğer templates yoksa)
    if os.path.isdir("templates"):
        return templates.TemplateResponse("login.html", {"request": request, "app_name": APP_NAME})
    html = f"""
    <html><head><title>{APP_NAME} - Giriş</title></head>
    <body style="font-family: system-ui; max-width: 420px; margin:40px auto;">
      <h2>{APP_NAME}</h2>
      <form method="post" action="/login">
        <label>Kullanıcı adı</label><br/>
        <input name="username" required style="width:100%;padding:8px;margin:6px 0;"/><br/>
        <label>Şifre</label><br/>
        <input name="password" type="password" required style="width:100%;padding:8px;margin:6px 0;"/><br/>
        <button style="padding:10px 14px;">Giriş</button>
      </form>
    </body></html>
    """
    return HTMLResponse(html)

@app.post("/login")
async def login(request: Request, username: str = Form(...), password: str = Form(...)):
    con = sqlite3.connect(USAGE_DB)
    cur = con.cursor()
    cur.execute("SELECT id, username, password_hash, role FROM users WHERE username=?", (username,))
    row = cur.fetchone()
    con.close()
    if not row or not bcrypt.verify(password, row[2]):
        return HTMLResponse("<h3>Hatalı giriş</h3><a href='/login'>Geri dön</a>", status_code=401)
    request.session["user"] = {"id": row[0], "username": row[1], "role": row[3]}
    return RedirectResponse("/plan", status_code=302)

@app.post("/logout")
def logout(request: Request):
    request.session.pop("user", None)
    return RedirectResponse("/login", status_code=302)

@app.get("/plan", response_class=HTMLResponse)
def plan_page(request: Request):
    if not request.session.get("user"):
        return RedirectResponse("/login")
    # Basit form (templates yoksa)
    if os.path.isdir("templates"):
        return templates.TemplateResponse("plan.html", {"request": request, "app_name": APP_NAME})
    html = f"""
    <html><head><title>{APP_NAME} - Plan Oluştur</title></head>
    <body style="font-family: system-ui; max-width: 900px; margin:24px auto;">
      <h2>Plan Oluştur</h2>
      <form method="post" action="/generate" enctype="multipart/form-data">
        <div style="display:grid;grid-template-columns:1fr 1fr; gap:12px;">
          <div>
            <label>Otel adı</label><br/>
            <input name="otel_adi" required style="width:100%;padding:8px;"/>
          </div>
          <div>
            <label>Otel iletişim bilgisi</label><br/>
            <input name="iletisim" placeholder="Telefon, e-posta, adres..." style="width:100%;padding:8px;"/>
          </div>
          <div>
            <label>Başlangıç tarihi</label><br/>
            <input name="baslangic_tarihi" type="date" required style="width:100%;padding:8px;"/>
          </div>
          <div>
            <label>Kaç günde bir paylaşılsın?</label><br/>
            <input name="tekrar_gunu" type="number" value="1" min="1" style="width:100%;padding:8px;"/>
          </div>
          <div>
            <label>Çıkacak Word dosya adı</label><br/>
            <input name="dosya_adi" placeholder="Instagram_Plani.docx" style="width:100%;padding:8px;"/>
          </div>
          <div>
            <label>Görseller (çoklu seçim)</label><br/>
            <input name="images" type="file" accept="image/*" multiple required />
          </div>
        </div>
        <div style="margin-top:14px;">
          <button style="padding:10px 14px;">Planı Oluştur</button>
        </div>
      </form>
      <form method="post" action="/logout" style="margin-top:16px;">
        <button>Çıkış</button>
      </form>
    </body></html>
    """
    return HTMLResponse(html)

@app.post("/generate")
async def generate_plan(
    request: Request,
    otel_adi: str = Form(...),
    iletisim: str = Form(""),
    baslangic_tarihi: str = Form(...),
    tekrar_gunu: int = Form(1),
    dosya_adi: str = Form("Instagram_Plani.docx"),
    images: List[UploadFile] = []
):
    # Güvenlik
    if not request.session.get("user"):
        return RedirectResponse("/login")

    if not images:
        return HTMLResponse("<h3>Görsel yüklenmedi</h3><a href='/plan'>Geri dön</a>", status_code=400)

    parsed_images: List[Tuple[Image.Image, bytes, str]] = []
    for f in images:
        try:
            img, raw, mime = pil_from_upload(f)
            parsed_images.append((img, raw, mime))
        except Exception:
            continue

    if not parsed_images:
        return HTMLResponse("<h3>Geçerli görsel bulunamadı</h3><a href='/plan'>Geri dön</a>", status_code=400)

    docx_buf = build_docx(
        otel_adi=otel_adi.strip(),
        iletisim=iletisim.strip(),
        baslangic_tarihi=baslangic_tarihi.strip(),
        tekrar_gunu=int(tekrar_gunu),
        dosya_adi=dosya_adi.strip(),
        images=parsed_images
    )

    filename = dosya_adi.strip() or "Instagram_Plani.docx"
    if not filename.lower().endswith(".docx"):
        filename += ".docx"

    return StreamingResponse(
        docx_buf,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )


# --- Basit admin (kullanıcı listesi) ---
@app.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request):
    try:
        user = require_admin(request)
    except HTTPException:
        return RedirectResponse("/login")

    con = sqlite3.connect(USAGE_DB)
    cur = con.cursor()
    cur.execute("SELECT id, username, role, created_at FROM users ORDER BY id DESC")
    rows = cur.fetchall()
    con.close()

    if os.path.isdir("templates"):
        return templates.TemplateResponse("admin.html", {"request": request, "user": user, "rows": rows, "app_name": APP_NAME})

    # Basit tablo (templates yoksa)
    trs = "".join(
        f"<tr><td>{r[0]}</td><td>{r[1]}</td><td>{r[2]}</td><td>{r[3]}</td></tr>"
        for r in rows
    )
    html = f"""
    <html><head><title>Admin - {APP_NAME}</title></head>
    <body style="font-family: system-ui; max-width: 900px; margin:24px auto;">
      <h2>Admin Panel</h2>
      <a href="/plan">Plan oluştur</a>
      <table border="1" cellpadding="6" cellspacing="0" style="margin-top:12px;width:100%;">
        <thead><tr><th>ID</th><th>Kullanıcı</th><th>Rol</th><th>Oluşturma</th></tr></thead>
        <tbody>{trs}</tbody>
      </table>
    </body></html>
    """
    return HTMLResponse(html)


# HEAD isteklerinde 200 dönelim (Render port kontrolü vs.)
@app.head("/")
def head_root():
    return PlainTextResponse("", status_code=200)
