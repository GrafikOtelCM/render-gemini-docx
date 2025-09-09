import io, os, re, json, base64, datetime, sqlite3, csv, traceback, calendar
from typing import List, Tuple

import requests
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, StreamingResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import TemplateNotFound

from PIL import Image
from docx import Document
from docx.shared import Inches

# ====== Zaman dilimi (Europe/Istanbul) ======
try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("Europe/Istanbul")
except Exception:
    TZ = None  # yoksa naive kullanırız

# ========= Ayarlar =========
APP_NAME = "Plan Otomasyon – Gemini to DOCX"
MAX_IMAGES = 10
MAX_EDGE = 1280
IMG_JPEG_QUALITY = 80
DEFAULT_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

# Fiyatlar (USD / 1M token) - interaktif
RATE_IN_PER_MTOK  = float(os.getenv("RATE_IN_PER_MTOK",  "0.30"))
RATE_OUT_PER_MTOK = float(os.getenv("RATE_OUT_PER_MTOK", "2.50"))

USD_TRY_RATE = float(os.getenv("USD_TRY_RATE", "41.2"))
ASSUME_IN_TOKENS  = int(os.getenv("ASSUME_IN_TOKENS",  "400"))
ASSUME_OUT_TOKENS = int(os.getenv("ASSUME_OUT_TOKENS", "80"))

BANNED_WORDS_RE = re.compile(r"(kaçış|kaçamak|kraliyet)", re.IGNORECASE)
DB_PATH = os.getenv("USAGE_DB_PATH", "usage.db")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "YOUR_API_KEY_HERE")

# ========= FastAPI =========
app = FastAPI(title=APP_NAME)
if os.path.isdir("static"):
    app.mount("/static", StaticFiles(directory="static"), name="static")
else:
    print("Uyarı: 'static/' klasörü bulunamadı; CSS yüklenmeyecek.")
templates = Jinja2Templates(directory="templates")

# ========= DB =========
def ensure_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS usage (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      ts TEXT NOT NULL,
      model TEXT NOT NULL,
      doc_name TEXT NOT NULL,
      images INTEGER NOT NULL,
      in_tokens INTEGER NOT NULL,
      out_tokens INTEGER NOT NULL,
      cost_usd REAL NOT NULL,
      cost_try REAL NOT NULL
    );
    """)
    conn.commit()
    conn.close()

def log_usage(ts: str, model: str, doc_name: str, images: int,
              in_tok: int, out_tok: int, cost_usd: float, cost_try: float):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
      INSERT INTO usage (ts, model, doc_name, images, in_tokens, out_tokens, cost_usd, cost_try)
      VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (ts, model, doc_name, images, in_tok, out_tok, cost_usd, cost_try))
    conn.commit()
    conn.close()

def month_bounds(dt: datetime.datetime):
    first = dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    nxt = (first.replace(year=first.year+1, month=1)
           if first.month == 12 else first.replace(month=first.month+1))
    last = nxt - datetime.timedelta(seconds=1)
    return first, last

# ========= TR tarih formatı =========
MONTHS_TR = ["Ocak","Şubat","Mart","Nisan","Mayıs","Haziran","Temmuz","Ağustos","Eylül","Ekim","Kasım","Aralık"]
WEEKDAYS_TR = ["Pazartesi","Salı","Çarşamba","Perşembe","Cuma","Cumartesi","Pazar"]  # Monday=0

def format_date_tr(d: datetime.date) -> str:
    wd = WEEKDAYS_TR[d.weekday()]
    return f"{d.day:02d} {MONTHS_TR[d.month-1]} {d.year} {wd}"

# ========= Plan takvimi üretimi =========
def generate_schedule(year: int, month: int, every_n_days: int) -> List[datetime.date]:
    # Ayın 1'inden başla, 29'unda bitir (ayın gerçek uzunluğu dikkate alınır)
    last_day = calendar.monthrange(year, month)[1]
    cutoff = min(29, last_day)
    if every_n_days < 1:
        every_n_days = 1
    dates = []
    day = 1
    while day <= cutoff:
        dates.append(datetime.date(year, month, day))
        day += every_n_days
    return dates

# ========= JSON Ayıklayıcı =========
import re as _re
def _extract_json_maybe(text: str):
    if not text:
        return None
    m = _re.search(r"```(?:json)?\s*({[\s\S]*?})\s*```", text, _re.IGNORECASE)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    m = _re.search(r"({[\s\S]*})", text)
    if m:
        raw = m.group(1)
        open_cnt = raw.count("{"); close_cnt = raw.count("}")
        if close_cnt < open_cnt:
            raw = raw + ("}" * (open_cnt - close_cnt))
        try:
            return json.loads(raw)
        except Exception:
            pass
    return None

# ========= DOCX yardımcıları =========
def _set_doc_margins(section, top=0.5, bottom=0.5, left=0.5, right=0.5):
    section.top_margin = Inches(top)
    section.bottom_margin = Inches(bottom)
    section.left_margin = Inches(left)
    section.right_margin = Inches(right)

def make_preview_bytes(upload: UploadFile) -> Tuple[bytes, str]:
    raw = upload.file.read()
    upload.file.seek(0)
    img = Image.open(io.BytesIO(raw)).convert("RGB")
    w, h = img.size
    scale = min(MAX_EDGE / max(w, h), 1.0)
    if scale < 1.0:
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=IMG_JPEG_QUALITY, optimize=True, progressive=True)
    out.seek(0)
    fname = os.path.splitext(upload.filename or f"img_{id(upload)}")[0]
    return out.read(), fname

# ========= Gemini çağrısı =========
def call_gemini_for_caption_and_tags(jpeg_bytes: bytes):
    if not GEMINI_API_KEY or GEMINI_API_KEY == "YOUR_API_KEY_HERE":
        raise RuntimeError("GEMINI_API_KEY tanımlı değil. Render Environment'a ekleyin.")

    base64_img = base64.b64encode(jpeg_bytes).decode("utf-8")

    prompt = (
        "Yalnızca JSON üret. Şema: {\"caption\": string, \"hashtags\": [string, string, string]}.\n"
        "Kurallar: Türkçe, 1-2 cümle kısa pazarlama açıklaması; emoji makul; marka/özel isim verme. "
        "Tam 3 hashtag üret. Şu kelimeleri asla kullanma: kaçış, kaçamak, kraliyet."
    )

    generation_config = { "response_mime_type": "application/json" }

    payload = {
        "contents": [{
            "role": "user",
            "parts": [
                {"text": prompt},
                {"inlineData": {"mimeType": "image/jpeg", "data": base64_img}}
            ]
        }],
        "generationConfig": generation_config
    }

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{DEFAULT_MODEL}:generateContent?key={GEMINI_API_KEY}"
    try:
        res = requests.post(url, json=payload, timeout=60)
    except Exception as e:
        raise RuntimeError(f"Gemini bağlantı hatası: {e}")

    if res.status_code != 200:
        msg = res.text.strip()[:500]
        raise RuntimeError(f"Gemini API hata {res.status_code}: {msg}")

    data = res.json()

    in_tok, out_tok = ASSUME_IN_TOKENS, ASSUME_OUT_TOKENS
    try:
        usage = data.get("usageMetadata") or data.get("candidates", [{}])[0].get("usageMetadata") or {}
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
        if not t:
            continue
        if not t.startswith("#"):
            t = "#" + t.lstrip("#")
        if BANNED_WORDS_RE.search(t):
            continue
        if t not in tags:
            tags.append(t)
        if len(tags) == 3:
            break

    if not caption or len(tags) != 3:
        raise RuntimeError(f"Gemini JSON eksik/boş döndü. caption='{caption}', tags={tags}")

    return caption, tags, in_tok, out_tok

# ========= DOCX üretimi + kullanım toplama =========
def build_docx_and_collect_usage(doc_name: str, contact_info: str,
                                 images: List[Tuple[str, bytes, str]],
                                 plan_dates: List[datetime.date]) -> Tuple[bytes, int, int]:
    document = Document()
    _set_doc_margins(document.sections[0], 0.5, 0.5, 0.5, 0.5)

    total_in, total_out = 0, 0

    for idx, (stub, jpeg_bytes, _) in enumerate(images):
        # 1) Tarih başlığı (görselin üstünde)
        if idx < len(plan_dates):
            date_str = format_date_tr(plan_dates[idx])
        else:
            date_str = "Tarih plan dışı"
        p = document.add_paragraph()
        run = p.add_run(f"Paylaşım Tarihi: {date_str}")
        run.bold = True

        # 2) Görsel
        pic_stream = io.BytesIO(jpeg_bytes)
        try:
            document.add_picture(pic_stream, width=Inches(6))
        except Exception:
            im = Image.open(io.BytesIO(jpeg_bytes)).convert("RGB")
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=IMG_JPEG_QUALITY)
            buf.seek(0)
            document.add_picture(buf, width=Inches(6))

        # 3) Metinler
        caption, tags, in_tok, out_tok = call_gemini_for_caption_and_tags(jpeg_bytes)
        total_in  += in_tok
        total_out += out_tok

        document.add_paragraph(caption)
        document.add_paragraph(contact_info)
        document.add_paragraph(" ".join(tags))

        if idx != len(images) - 1:
            document.add_page_break()

    out = io.BytesIO()
    document.save(out)
    out.seek(0)
    return out.read(), total_in, total_out

# ========= Routes =========
@app.on_event("startup")
def _startup():
    ensure_db()

@app.get("/healthz")
def healthz():
    return {"ok": True}

@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    now = datetime.datetime.now(TZ) if TZ else datetime.datetime.now()
    suggested = now.strftime("%Y%m%d-%H%M")
    default_month = now.strftime("%Y-%m")  # input type=month için
    try:
        return templates.TemplateResponse(
            "index.html",
            {"request": request, "app_name": APP_NAME,
             "suggested_name": f"Instagram_Plani_{suggested}",
             "default_month": default_month}
        )
    except TemplateNotFound:
        return HTMLResponse(f"<h1>{APP_NAME}</h1><p>templates/index.html bulunamadı.</p>", status_code=200)

@app.post("/generate", response_class=HTMLResponse)
async def generate(
    request: Request,
    doc_name: str = Form(...),
    contact_info: str = Form(...),
    plan_month: str = Form(...),         # YYYY-MM
    interval_days: int = Form(...),      # kaç günde bir
    files: List[UploadFile] = File(...)
):
    files = [f for f in files if (f and f.filename)]
    if not files:
        return PlainTextResponse("Görsel yüklenmedi. Lütfen en az 1 görsel seçin.", status_code=400)
    if len(files) > MAX_IMAGES:
        return PlainTextResponse(f"En fazla {MAX_IMAGES} görsel yükleyebilirsiniz.", status_code=400)

    # Plan ayı ayrıştır
    try:
        year_str, month_str = plan_month.split("-")
        year, month = int(year_str), int(month_str)
        if not (1 <= month <= 12):
            raise ValueError()
    except Exception:
        return PlainTextResponse("Plan ayı hatalı. Lütfen YYYY-AA formatında bir ay seçin.", status_code=400)

    # Takvim üret
    dates = generate_schedule(year, month, int(interval_days))
    if len(files) > len(dates):
        return PlainTextResponse(
            f"Seçilen aralıkla {len(dates)} tarih üretiliyor, ancak {len(files)} görsel yüklediniz. "
            f"Aralığı küçültün (örn. 1-2 gün) ya da görsel sayısını azaltın.",
            status_code=400
        )

    # Görselleri işleme
    processed = []
    for f in files:
        try:
            jpeg_bytes, stub = make_preview_bytes(f)
            processed.append((stub, jpeg_bytes, f.filename))
        except Exception:
            return PlainTextResponse(f"{f.filename} okunamadı. Lütfen geçerli bir görsel yükleyin.", status_code=400)

    # DOCX + kullanım
    try:
        content, total_in, total_out = build_docx_and_collect_usage(doc_name, contact_info, processed, dates[:len(processed)])
    except Exception as e:
        print("Üretim hatası:", traceback.format_exc()[:2000])
        return PlainTextResponse(f"Üretim hatası: {str(e)}", status_code=500)

    # Maliyet
    cost_usd = (total_in / 1_000_000.0) * RATE_IN_PER_MTOK + (total_out / 1_000_000.0) * RATE_OUT_PER_MTOK
    cost_try = cost_usd * USD_TRY_RATE

    # Log
    ts = (datetime.datetime.now(TZ) if TZ else datetime.datetime.now()).isoformat(timespec="seconds")
    try:
        log_usage(ts, DEFAULT_MODEL, doc_name, len(processed), total_in, total_out, cost_usd, cost_try)
    except Exception as e:
        print("DB log error:", e)

    # İndirme
    filename = f"{doc_name}.docx"
    headers = {
        "Content-Disposition": f'attachment; filename="{filename}"',
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

@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request):
    now = datetime.datetime.now(TZ) if TZ else datetime.datetime.now()
    start, end = month_bounds(now)
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
      SELECT COUNT(*), COALESCE(SUM(images),0), COALESCE(SUM(in_tokens),0), COALESCE(SUM(out_tokens),0),
             COALESCE(SUM(cost_usd),0), COALESCE(SUM(cost_try),0)
      FROM usage
      WHERE ts >= ? AND ts <= ?
    """, (start.isoformat(), end.isoformat()))
    cnt, img_sum, in_sum, out_sum, usd_sum, try_sum = cur.fetchone() or (0,0,0,0,0.0,0.0)

    cur.execute("""
      SELECT ts, doc_name, images, in_tokens, out_tokens, cost_usd, cost_try
      FROM usage
      WHERE ts >= ? AND ts <= ?
      ORDER BY ts DESC LIMIT 30
    """, (start.isoformat(), end.isoformat()))
    rows = cur.fetchall()
    conn.close()

    try:
        return templates.TemplateResponse("dashboard.html", {
            "request": request,
            "app_name": APP_NAME,
            "month_title": now.strftime("%B %Y"),
            "summary": {"runs": cnt, "images": img_sum, "in_tokens": in_sum, "out_tokens": out_sum,
                        "usd": usd_sum, "try": try_sum},
            "rows": rows,
            "usd_try_rate": USD_TRY_RATE,
            "model": DEFAULT_MODEL,
            "rate_in": RATE_IN_PER_MTOK,
            "rate_out": RATE_OUT_PER_MTOK
        })
    except TemplateNotFound:
        return HTMLResponse("<h1>Dashboard</h1><p>templates/dashboard.html bulunamadı.</p>", status_code=200)

@app.get("/dashboard.csv", response_class=PlainTextResponse)
def dashboard_csv():
    now = datetime.datetime.now(TZ) if TZ else datetime.datetime.now()
    start, end = month_bounds(now)
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
      SELECT ts, model, doc_name, images, in_tokens, out_tokens, cost_usd, cost_try
      FROM usage
      WHERE ts >= ? AND ts <= ?
      ORDER BY ts ASC
    """, (start.isoformat(), end.isoformat()))
    rows = cur.fetchall()
    conn.close()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["ts","model","doc_name","images","in_tokens","out_tokens","cost_usd","cost_try"])
    for r in rows:
        writer.writerow(r)
    return PlainTextResponse(buf.getvalue(), media_type="text/csv")
