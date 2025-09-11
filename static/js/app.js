// ----- küçük yardımcılar -----
function $(q) { return document.querySelector(q); }
function el(tag, cls) { const e = document.createElement(tag); if (cls) e.className = cls; return e; }

// ----- hızlı ay listesi -----
(function fillMonths() {
  const sel = $("#quick_month");
  if (!sel) return;
  const now = new Date();
  for (let i = 0; i < 12; i++) {
    const d = new Date(now.getFullYear(), i, 1);
    const v = `${d.getFullYear()}-${String(i+1).padStart(2,"0")}-01`;
    const o = document.createElement("option");
    o.value = v;
    o.textContent = d.toLocaleDateString("tr-TR", { month: "long", year: "numeric" });
    sel.appendChild(o);
  }
  sel.addEventListener("change", () => {
    if (sel.value) $("#start_date").value = sel.value;
  });
})();

// ----- sürükle-bırak -----
(function setupDrop() {
  const drop = $("#dropzone");
  const input = $("#images");
  const preview = $("#preview");
  if (!drop || !input || !preview) return;

  function handleFiles(files) {
    if (!files || !files.length) return;
    preview.innerHTML = "";
    Array.from(files).forEach(file => {
      const url = URL.createObjectURL(file);
      const img = el("img", "thumb");
      img.src = url;
      img.onload = () => URL.revokeObjectURL(url);
      preview.appendChild(img);
    });
  }

  input.addEventListener("change", e => handleFiles(e.target.files));

  ;["dragenter","dragover"].forEach(ev => drop.addEventListener(ev, e => {
    e.preventDefault(); e.stopPropagation(); drop.classList.add("hover");
  }));
  ;["dragleave","drop"].forEach(ev => drop.addEventListener(ev, e => {
    e.preventDefault(); e.stopPropagation(); drop.classList.remove("hover");
  }));
  drop.addEventListener("drop", e => {
    const dt = e.dataTransfer;
    if (dt && dt.files && dt.files.length) {
      $("#images").files = dt.files; // input'a yaz
      handleFiles(dt.files);
    }
  });
})();

// ----- plan oluştur -----
(function planCreate() {
  const btn = $("#createBtn");
  if (!btn) return;

  btn.addEventListener("click", async () => {
    const status = $("#status");
    status.textContent = "Oluşturuluyor…";
    btn.disabled = true;

    try {
      const fd = new FormData();
      fd.append("csrf_token", $("#csrf_token").value);
      fd.append("hotel_name", $("#hotel_name").value);
      fd.append("contact_info", $("#contact_info").value);
      fd.append("start_date", $("#start_date").value); // yyyy-mm-dd
      fd.append("interval_days", $("#interval_days").value);
      fd.append("docx_filename", $("#docx_filename").value || "Instagram_Plani.docx");

      const files = $("#images").files;
      if (!files || !files.length) {
        alert("En az bir görsel seçin.");
        status.textContent = "";
        btn.disabled = false;
        return;
      }
      Array.from(files).forEach(f => fd.append("images", f, f.name));

      const res = await fetch("/api/plan/create", { method: "POST", body: fd });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.detail || ("Hata (" + res.status + ")"));
      }

      const blob = await res.blob();
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = ($("#docx_filename").value || "Instagram_Plani.docx").replace(/[^a-zA-Z0-9_.\-ğüşöçıİĞÜŞÖÇ]/g, "_");
      document.body.appendChild(a);
      a.click();
      a.remove();
      status.textContent = "Plan başarıyla oluşturuldu.";
    } catch (err) {
      alert(err.message || "Plan oluşturulamadı.");
      $("#status").textContent = "";
    } finally {
      btn.disabled = false;
    }
  });
})();
