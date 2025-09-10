// Basit sürükle-bırak ve küçük önizleme
(function () {
  const fileInput = document.getElementById("images");
  const thumbs = document.getElementById("thumbs");
  const form = document.getElementById("plan-form");
  const result = document.getElementById("result");

  if (fileInput && thumbs) {
    const renderThumbs = (files) => {
      thumbs.innerHTML = "";
      [...files].forEach((f) => {
        if (!f.type.startsWith("image/")) return;
        const url = URL.createObjectURL(f);
        const wrap = document.createElement("div");
        wrap.className = "thumb";
        const img = document.createElement("img");
        img.src = url;
        img.onload = () => URL.revokeObjectURL(url);
        wrap.appendChild(img);
        const cap = document.createElement("div");
        cap.className = "thumb-cap";
        cap.textContent = f.name;
        wrap.appendChild(cap);
        thumbs.appendChild(wrap);
      });
    };

    fileInput.addEventListener("change", (e) => {
      renderThumbs(e.target.files || []);
    });

    // Drop area (input label'ına bırakılabilir)
    const uploader = fileInput.closest(".uploader");
    if (uploader) {
      ["dragenter", "dragover"].forEach((ev) =>
        uploader.addEventListener(ev, (e) => {
          e.preventDefault(); e.stopPropagation();
          uploader.classList.add("dragging");
        })
      );
      ["dragleave", "drop"].forEach((ev) =>
        uploader.addEventListener(ev, (e) => {
          e.preventDefault(); e.stopPropagation();
          uploader.classList.remove("dragging");
        })
      );
      uploader.addEventListener("drop", (e) => {
        const dt = e.dataTransfer;
        if (!dt || !dt.files) return;
        fileInput.files = dt.files;
        renderThumbs(dt.files);
      });
    }
  }

  // AJAX submit (JSON cevap göster)
  if (form && result) {
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const fd = new FormData(form);
      result.style.display = "block";
      result.textContent = "Oluşturuluyor…";

      try {
        const resp = await fetch(form.action, {
          method: "POST",
          body: fd,
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok || !data.ok) {
          const msg = (data && (data.error || data.detail)) || ("Hata: " + resp.status);
          result.className = "card alert-error";
          result.textContent = msg;
          return;
        }
        result.className = "card";
        result.innerHTML = `
          <b>Plan hazır.</b><br>
          Dosya adı: ${data.received.file_name}<br>
          Ay: ${data.received.month}<br>
          Periyot (gün): ${data.received.every_days}<br>
          İletişim: ${data.received.hotel_contact || "-"}<br>
          Görsel sayısı: ${data.received.images_count}
        `;
      } catch (err) {
        result.className = "card alert-error";
        result.textContent = "Beklenmeyen bir hata oluştu.";
      }
    });
  }
})();
