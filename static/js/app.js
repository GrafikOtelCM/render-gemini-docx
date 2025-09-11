(() => {
  const form = document.getElementById("planForm");
  const drop = document.getElementById("drop");
  const fileInput = document.getElementById("images");
  const thumbs = document.getElementById("thumbs");
  const submitBtn = document.getElementById("submitBtn");
  const bar = document.getElementById("bar");
  const statusEl = document.getElementById("status");

  // Drop bölgesi tıklayınca dosya seçici
  drop.addEventListener("click", () => fileInput.click());

  // Sürükle-bırak
  ["dragenter", "dragover"].forEach(ev =>
    drop.addEventListener(ev, e => { e.preventDefault(); e.stopPropagation(); drop.style.borderColor = "#22c55e"; })
  );
  ["dragleave", "drop"].forEach(ev =>
    drop.addEventListener(ev, e => { e.preventDefault(); e.stopPropagation(); drop.style.borderColor = "#334155"; })
  );
  drop.addEventListener("drop", e => {
    const files = Array.from(e.dataTransfer.files || []).filter(f => f.type.startsWith("image/"));
    if (files.length) {
      // input’a ekle
      const dt = new DataTransfer();
      Array.from(fileInput.files || []).forEach(f => dt.items.add(f));
      files.forEach(f => dt.items.add(f));
      fileInput.files = dt.files;
      renderThumbs();
    }
  });

  fileInput.addEventListener("change", renderThumbs);

  function renderThumbs() {
    thumbs.innerHTML = "";
    Array.from(fileInput.files || []).forEach(f => {
      const url = URL.createObjectURL(f);
      const div = document.createElement("div");
      div.className = "thumb";
      div.innerHTML = `<img src="${url}" alt="">`;
      thumbs.appendChild(div);
    });
  }

  function setProgress(p, text) {
    bar.style.width = `${p}%`;
    statusEl.textContent = text || "";
  }

  async function downloadBlobAsFile(blob, filename) {
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename || "plan.docx";
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  }

  submitBtn.addEventListener("click", async () => {
    // Zorunlu alan kontrolü
    if (!fileInput.files || fileInput.files.length === 0) {
      alert("Lütfen en az bir görsel seçin / yükleyin.");
      return;
    }
    const planMonth = document.getElementById("plan_month").value;
    if (!planMonth) {
      alert("Lütfen ay seçin.");
      return;
    }

    const fd = new FormData();
    // GÖRSELLER: her biri aynı anahtar ile -> images
    Array.from(fileInput.files).forEach(f => fd.append("images", f, f.name));
    // DİĞER ALANLAR
    fd.append("plan_month", planMonth);
    fd.append("every_n_days", document.getElementById("every_n_days").value || "2");
    fd.append("plan_name", document.getElementById("plan_name").value || "Instagram_Plani");
    fd.append("hotel_info", document.getElementById("hotel_info").value || "");

    submitBtn.disabled = true;
    setProgress(10, "Hazırlanıyor…");

    try {
      // fetch: FormData gönder — Content-Type otomatik ayarlanır (multipart/form-data; boundary=…)
      const res = await fetch("/api/plan", {
        method: "POST",
        body: fd,
      });

      if (!res.ok) {
        // JSON hata gövdesi varsa göster
        let msg = `Sunucu hatası (${res.status})`;
        try {
          const j = await res.json();
          msg = JSON.stringify(j);
        } catch (_) {}
        throw new Error(msg);
      }

      setProgress(70, "Word oluşturuluyor…");
      const blob = await res.blob();

      // Sunucunun Content-Disposition’ından isim ayıkla
      let filename = "Instagram_Plani.docx";
      try {
        const cd = res.headers.get("Content-Disposition") || "";
        const m = /filename="([^"]+)"/i.exec(cd);
        if (m && m[1]) filename = m[1];
      } catch (_) {}

      setProgress(90, "İndiriliyor…");
      await downloadBlobAsFile(blob, filename);
      setProgress(100, "Tamamlandı ✅");
    } catch (err) {
      console.error(err);
      alert("Hata: " + (err && err.message ? err.message : err));
      setProgress(0, "Hata");
    } finally {
      submitBtn.disabled = false;
      setTimeout(() => setProgress(0, "Hazır"), 1500);
    }
  });
})();
