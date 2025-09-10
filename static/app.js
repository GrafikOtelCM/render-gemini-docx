(function () {
  const dz = document.getElementById("dropzone");
  const pickBtn = document.getElementById("pickBtn");
  const fileInput = document.getElementById("imageInput");
  const thumbs = document.getElementById("thumbs");
  const planForm = document.getElementById("planForm");

  if (!dz || !fileInput) return;

  function renderThumbs(files) {
    thumbs.innerHTML = "";
    Array.from(files).forEach((f) => {
      const url = URL.createObjectURL(f);
      const item = document.createElement("div");
      item.className = "thumb";
      const img = document.createElement("img");
      img.src = url;
      img.onload = () => URL.revokeObjectURL(url);
      const cap = document.createElement("div");
      cap.className = "caption";
      cap.textContent = f.name;
      item.appendChild(img);
      item.appendChild(cap);
      thumbs.appendChild(item);
    });
  }

  function addFiles(files) {
    const dt = new DataTransfer();
    // mevcutları koru
    Array.from(fileInput.files).forEach(f => dt.items.add(f));
    Array.from(files).forEach(f => dt.items.add(f));
    fileInput.files = dt.files;
    renderThumbs(fileInput.files);
  }

  pickBtn?.addEventListener("click", () => fileInput.click());
  fileInput.addEventListener("change", (e) => renderThumbs(e.target.files));

  dz.addEventListener("dragover", (e) => {
    e.preventDefault();
    dz.classList.add("dragover");
  });
  dz.addEventListener("dragleave", () => dz.classList.remove("dragover"));
  dz.addEventListener("drop", (e) => {
    e.preventDefault();
    dz.classList.remove("dragover");
    addFiles(e.dataTransfer.files);
  });

  // Form submit: normal POST (fetch değil) => sunucu DOCX döndürür, tarayıcı indirir.
  planForm?.addEventListener("submit", () => {
    // butona küçük bir loading durumu
    const btn = planForm.querySelector("button[type=submit]");
    if (btn) {
      btn.disabled = true;
      btn.textContent = "Oluşturuluyor...";
      setTimeout(() => (btn.disabled = false, btn.textContent = "Planı Oluştur (DOCX indir)"), 8000);
    }
  });
})();
