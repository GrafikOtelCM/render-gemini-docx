(function () {
  const byId = (id) => document.getElementById(id);
  const dropzone = byId('dropzone');
  const fileInput = byId('file-input');
  const preview = byId('preview');
  const fileCount = byId('file-count');
  const planForm = byId('plan-form');
  const submitBtn = byId('submit-btn');

  if (dropzone && fileInput) {
    const updatePreview = (files) => {
      preview.innerHTML = '';
      let count = 0;
      Array.from(files).forEach((f) => {
        if (!f.type.startsWith('image/')) return;
        const url = URL.createObjectURL(f);
        const item = document.createElement('div');
        item.className = 'thumb';
        const img = document.createElement('img');
        img.src = url;
        img.onload = () => URL.revokeObjectURL(url);
        item.appendChild(img);
        const cap = document.createElement('div');
        cap.className = 'thumb-cap';
        cap.textContent = f.name;
        item.appendChild(cap);
        preview.appendChild(item);
        count++;
      });
      if (fileCount) fileCount.textContent = `Seçili dosya: ${count}`;
    };

    // Sürükle-bırak
    ['dragenter','dragover'].forEach(ev =>
      dropzone.addEventListener(ev, (e)=>{ e.preventDefault(); dropzone.classList.add('hover'); })
    );
    ;['dragleave','drop'].forEach(ev =>
      dropzone.addEventListener(ev, (e)=>{ e.preventDefault(); dropzone.classList.remove('hover'); })
    );
    dropzone.addEventListener('drop', (e) => {
      const dt = e.dataTransfer;
      if (!dt) return;
      const files = dt.files;
      if (!files || !files.length) return;
      fileInput.files = files;
      updatePreview(files);
    });

    // Dosya seç
    dropzone.addEventListener('click', () => fileInput.click());
    fileInput.addEventListener('change', (e) => updatePreview(e.target.files));
  }

  // Basit submit koruması
  if (planForm && submitBtn) {
    planForm.addEventListener('submit', () => {
      submitBtn.disabled = true;
      submitBtn.textContent = 'Oluşturuluyor...';
      setTimeout(() => {
        submitBtn.disabled = false;
        submitBtn.textContent = 'Oluştur & İndir';
      }, 10000);
    });
  }
})();
