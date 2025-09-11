// Drag & drop küçük önizleme
(function(){
  const input = document.getElementById("fileInput");
  const thumbs = document.getElementById("thumbs");
  if (!input || !thumbs) return;

  input.addEventListener("change", () => {
    thumbs.innerHTML = "";
    [...input.files].forEach(f => {
      const url = URL.createObjectURL(f);
      const d = document.createElement("div");
      d.className = "thumb";
      const img = document.createElement("img");
      img.src = url;
      d.appendChild(img);
      thumbs.appendChild(d);
    });
  });

  // form submit -> docx indir
  const form = document.getElementById("planForm");
  if (!form) return;

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd = new FormData(form);
    const files = document.getElementById("fileInput").files;
    if (!files || !files.length) {
      alert("Lütfen en az bir görsel seçin.");
      return;
    }
    // Çoklu dosya ekle
    fd.delete("files");
    [...files].forEach(f => fd.append("files", f));

    const btn = form.querySelector(".btn");
    const oldTxt = btn.textContent;
    btn.disabled = true; btn.textContent = "Oluşturuluyor…";

    try {
      const res = await fetch("/api/plan", {
        method: "POST",
        body: fd
      });
      if (!res.ok) {
        const txt = await res.text();
        throw new Error(txt || "İşlem başarısız.");
      }
      const blob = await res.blob();
      const cd = res.headers.get("Content-Disposition") || "";
      const m = cd.match(/filename="(.+?)"/);
      const name = m ? m[1] : "Instagram_Plani.docx";

      const link = document.createElement("a");
      link.href = URL.createObjectURL(blob);
      link.download = name;
      document.body.appendChild(link);
      link.click();
      link.remove();
    } catch (err) {
      alert("Hata: " + err.message);
    } finally {
      btn.disabled = false; btn.textContent = oldTxt;
    }
  });
})();
