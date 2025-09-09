import io, os, re, json, base64, datetime, sqlite3, csv
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

# ========= Ayarlar =========
APP_NAME = "Plan Otomasyon – Gemini to DOCX"
MAX_IMAGES = 10                          # Plan başına görsel limiti
MAX_EDGE = 1280                          # Gemini'ye giden önizleme (token azaltır)
IMG_JPEG_QUALITY = 80
DEFAULT_MODEL = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "AIzaSyDdUV3SuQ1bbhqILvR_70wGRSMdDGkOoNI")

# Fiyatlar (USD / 1M token)
RATE_IN_PER_MTOK  = float(os.getenv("RATE_IN_PER_MTOK",  "0.075"))
RATE_OUT_PER_MTOK = float(os.getenv("RATE_OUT_PER_MTOK", "0.30"))

# Kur (USD→TRY)
USD_TRY_RATE = float(os.getenv("USD_TRY_RATE", "41.2"))

# usageMetadata yoksa güvenli varsayımlar (görsel başına)
ASSUME_IN_TOKENS  = int(os.getenv("ASSUME_IN_TOKENS",  "400"))
ASSUME_OUT_TOKENS = int(os.getenv("ASSUME_OUT_TOKENS", "80"))

BANNED_WORDS_RE = re.compile(r"(kaçış|kaçamak|kraliyet)", re.IGNORECASE)
DB_PATH = os.getenv("USAGE_DB_PATH", "usage.db")

# ========= FastAPI =========
app = FastAPI(title=APP_NAME)
# static/ klasörü yoksa uyarı yaz, ama mount etme (beyaz ekranın önüne geçer)
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
    """
    Dönüş: (caption:str, tags:list[str], in_tokens:int, out_tokens:int)
    usageMetadata yoksa ASSUME_* değerleriyle hesap yapılır.
    """
    if not GEMINI_API_KEY or GEMINI_API_KEY == "YOUR_API_KEY_HERE":
        raise RuntimeError("GEMINI_API_KEY tanımlı değil. Render Environment'a ekleyin.")

    base64_img = base64.b64encode(jpeg_bytes).decode("utf-8")
    prompt = (
        "Aşağıdaki görsel için Instagram odaklı, satışa götüren, KISA ve TÜRKÇE bir açıklama üret. "
        "1-2 cümle, emojiyi az ve yerinde kullan. "
        "Ayrıca bu görsele uygun TAM 3 adet hashtag üret. "
        "Marka/özel isim verme. "
        "‘kaçış’, ‘kaçamak’, ‘kraliyet’ kelimelerini asla kullanma. "
        "Sadece aşağıdaki JSON ile cevap ver."
    )
    generation_config = {"response_mime_type": "application/json"}
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
    res = requests.post(url, json=payload, timeout=60)

    in_tok, out_tok = ASSUME_IN_TOKENS, ASSUME_OUT_TOKENS

    if res.status_code != 200:
        print("Gemini error:", res.status_code, res.text[:300])
        return ("Zarif konforla tanışın; tatilin özü burada. ✨", ["#tatil", "#otel", "#holiday"], in_tok, out_tok)

    data = res.json()

    # usageMetadata
    try:
        usage = data.get("usageMetadata") or data.get("candidates", [{}])[0].get("usageMetadata") or {}
        in_tok  = int(usage.get("promptTokenCount", in_tok))
        out_tok = int(usage.get("candidatesTokenCount", usage.get("outputTokenCount", out_tok)))
    except Exception as e:
        print("usageMetadata parse err:", e)

    # JSON içerik
    text = ""
    try:
        parts = data.get("candidates", [])[0].get("content", {}).get("parts", [])
        text = "".join(p.get("text", "") for p in parts).strip()
        obj = json.loads(text)
        caption = obj.get("caption", "").strip()
        hashtags = obj.get("hashtags", [])

        caption = re.sub(r"\s+", " ", caption)
        caption = BANNED_WORDS_RE.sub("", caption)
        caption = re.sub(r"https?://\S+", "", caption).strip()

        tags = []
        for t in hashtags:
            t = t.strip()
            if not t.startswith("#"):
                t = "#" + t.lstrip("#")
            if BANNED_WORDS_RE.search(t):
                continue
            if t not in tags:
                tags.append(t)
            if len(tags) == 3:
                break

        if not caption:
            caption = "Zarif konforla tanışın; tatilin özü burada. ✨"
        if len(tags) != 3:
            tags = ["#tatil", "#otel", "#holiday"]

        return caption, tags, in_tok, out_tok
    except Exception as e:
        print("Parsing error:", e, text[:300])
        return ("Zarif konforla tanışın; tatilin özü burada. ✨", ["#tatil", "#otel", "#holiday"], in_tok, out_tok)


# ========= DOCX üretimi + kullanım toplama =========
def build_docx_and_collect_usage(doc_name: str, contact_info: str,
                                 images: List[Tuple[str, bytes, str]]) -> Tuple[bytes, int, int]:
    document = Document()
    _set_doc_margins(document.sections[0], 0.5, 0.5, 0.5, 0.5)

    total_in, total_out = 0, 0

    for idx, (stub, jpeg_bytes, _) in enumerate(images):
        # Görsel
        pic_stream = io.BytesIO(jpeg_bytes)
        try:
            document.add_picture(pic_stream, width=Inches(6))
        except Exception:
            im = Image.open(io.BytesIO(jpeg_bytes)).convert("RGB")
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=IMG_JPEG_QUALITY)
            buf.seek(0)
            document.add_picture(buf, width=Inches(6))

        # Gemini
        caption, tags, in_tok, out_tok = call_gemini_for_caption_and_tags(jpeg_bytes)
        total_in  += in_tok
        total_out += out_tok

        # Metinler
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
    today = datetime.datetime.now().strftime("%Y%m%d-%H%M")
    suggested = f"Instagram_Plani_{today}"
    try:
        return templates.TemplateResponse(
            "index.html",
            {"request": request, "app_name": APP_NAME, "suggested_name": suggested}
        )
    except TemplateNotFound:
        return HTMLResponse(f"<h1>{APP_NAME}</h1><p>templates/index.html bulunamadı.</p>", status_code=200)

@app.post("/generate", response_class=HTMLResponse)
async def generate(
    request: Request,
    doc_name: str = Form(...),
    contact_info: str = Form(...),
    files: List[UploadFile] = File(...)
):
    files = [f for f in files if (f and f.filename)]
    if not files:
        return PlainTextResponse("Görsel yüklenmedi. Lütfen en az 1 görsel seçin.", status_code=400)
    if len(files) > MAX_IMAGES:
        return PlainTextResponse(f"En fazla {MAX_IMAGES} görsel yükleyebilirsiniz.", status_code=400)

    processed = []
    for f in files:
        try:
            jpeg_bytes, stub = make_preview_bytes(f)
            processed.append((stub, jpeg_bytes, f.filename))
        except Exception:
            return PlainTextResponse(f"{f.filename} okunamadı. Lütfen geçerli bir görsel yükleyin.", status_code=400)

    # DOCX + kullanım
    try:
        content, total_in, total_out = build_docx_and_collect_usage(doc_name, contact_info, processed)
    except Exception as e:
        return PlainTextResponse(f"Üretim hatası: {str(e)}", status_code=500)

    # Maliyet
    cost_usd = (total_in / 1_000_000.0) * RATE_IN_PER_MTOK + (total_out / 1_000_000.0) * RATE_OUT_PER_MTOK
    cost_try = cost_usd * USD_TRY_RATE

    # Log
    ts = datetime.datetime.now().isoformat(timespec="seconds")
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
    now = datetime.datetime.now()
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
    now = datetime.datetime.now()
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
