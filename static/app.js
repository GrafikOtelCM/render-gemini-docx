(function () {
  const MAX_FILES = 10;

  function compressImage(file, maxEdge = 1280, quality = 0.8) {
    return new Promise((resolve, reject) => {
      if (!/^image\//.test(file.type)) return resolve(null);
      const img = new Image();
      img.onload = () => {
        const canvas = document.createElement('canvas');
        let { width: w, height: h } = img;
        const scale = Math.min(maxEdge / Math.max(w, h), 1);
        w = Math.round(w * scale);
        h = Math.round(h * scale);
        canvas.width = w; canvas.height = h;
        const ctx = canvas.getContext('2d');
        ctx.drawImage(img, 0, 0, w, h);
        canvas.toBlob(blob => {
          if (!blob) return reject(new Error('Sıkıştırma başarısız'));
          const name = (file.name || 'image').replace(/\.\w+$/, '') + '.jpg';
          const out = new File([blob], name, { type: 'image/jpeg' });
          resolve(out);
        }, 'image/jpeg', quality);
      };
      img.onerror = () => resolve(null);
      const reader = new FileReader();
      reader.onload = e => { img.src = e.target.result; };
      reader.readAsDataURL(file);
    });
  }

  async function compressBatch(files) {
    const out = [];
    for (const f of files) { out.push(await compressImage(f, 1280, 0.8) || f); }
    return out;
  }

  const form = document.getElementById('planForm');
  const fileInput = document.getElementById('fileInput');
  const dropzone = document.getElementById('dropzone');
  const fileCount = document.getElementById('fileCount');
  const totalSize = document.getElementById('totalSize');
  const previewGrid = document.getElementById('previewGrid');
  const scheduleInfo = document.getElementById('scheduleInfo');
  const submitBtn = document.getElementById('submitBtn');
  const clearBtn = document.getElementById('clearBtn');
  const errorBox = document.getElementById('errorBox');
  const docNameEl = document.getElementById('doc_name');
  const contactEl = document.getElementById('contact_info');
  const planMonthEl = document.getElementById('plan_month');
  const intervalEl = document.getElementById('interval_days');
  const projectTagEl = document.getElementById('project_tag');

  let currentFiles = [];
  function bytesToMB(b) { return (b / (1024 * 1024)).toFixed(2); }
  function showError(msg) { errorBox.textContent = msg; errorBox.classList.remove('hidden'); }
  function clearError() { errorBox.textContent = ''; errorBox.classList.add('hidden'); }
  function clearPreviews() { previewGrid.innerHTML = ''; }
  function updateStatus() {
    fileCount.textContent = `${currentFiles.length}/${MAX_FILES} görsel seçildi`;
    const totalBytes = currentFiles.reduce((a, f) => a + (f.size || 0), 0);
    totalSize.textContent = 'Toplam: ' + bytesToMB(totalBytes) + ' MB';
    submitBtn.disabled = currentFiles.length === 0 || currentFiles.length > MAX_FILES;
  }

  function calcPlanCount() {
    const ym = planMonthEl.value; const n = Math.max(1, parseInt(intervalEl.value || '1', 10));
    if (!/^\d{4}-\d{2}$/.test(ym)) { scheduleInfo.textContent=''; return 0; }
    const [y,m] = ym.split('-').map(Number);
    const lastDay = new Date(y, m, 0).getDate();
    const cutoff = Math.min(29, lastDay);
    let cnt = 0; for (let d=1; d<=cutoff; d+=n) cnt++;
    scheduleInfo.textContent = `Bu ay ${cnt} tarih planlanacak (1 → ${cutoff}, ${n} günde bir).`;
    return cnt;
  }

  function makePreview(file, idx) {
    const card = document.createElement('div'); card.className = 'file-card';
    const img = document.createElement('img'); img.alt = file.name; img.loading = 'lazy';
    const reader = new FileReader(); reader.onload = e => { img.src = e.target.result; };
    reader.readAsDataURL(file);
    const meta = document.createElement('div'); meta.className = 'file-meta';
    meta.innerHTML = `<div class="name" title="${file.name}">${file.name}</div><div class="size">${bytesToMB(file.size)} MB</div>`;
    const removeBtn = document.createElement('button'); removeBtn.type='button'; removeBtn.className='chip'; removeBtn.textContent='Kaldır';
    removeBtn.addEventListener('click', () => { currentFiles.splice(idx, 1); renderPreviews(); });
    card.appendChild(img); card.appendChild(meta); card.appendChild(removeBtn); return card;
  }

  function renderPreviews() {
    clearPreviews(); clearError();
    const planCnt = calcPlanCount();
    if (currentFiles.length > MAX_FILES) showError(`En fazla ${MAX_FILES} görsel yükleyebilirsiniz.`);
    if (planCnt > 0 && currentFiles.length > planCnt) {
      showError(`Seçilen aralıkla ${planCnt} tarih çıkıyor; ${currentFiles.length} görsel fazla. Aralığı düşürün veya görsel sayısını azaltın.`);
    }
    currentFiles.forEach((f, i) => previewGrid.appendChild(makePreview(f, i)));
    updateStatus();
  }

  async function addFiles(fileList) {
    const imgs = Array.from(fileList || []).filter(f => /^image\//.test(f.type));
    if (!imgs.length) return;
    submitBtn.textContent = 'Görseller hazırlanıyor…'; submitBtn.disabled = true;
    try {
      const compressed = await compressBatch(imgs);
      currentFiles = currentFiles.concat(compressed).slice(0, MAX_FILES);
    } finally {
      submitBtn.textContent = 'Oluştur ve indir (.docx)';
      submitBtn.disabled = currentFiles.length === 0;
    }
    renderPreviews();
  }

  ['dragenter','dragover','dragleave','drop'].forEach(evt => {
    dropzone.addEventListener(evt, e => { e.preventDefault(); e.stopPropagation(); }, false);
  });
  ['dragenter','dragover'].forEach(evt => dropzone.addEventListener(evt, () => dropzone.classList.add('hover'), false));
  ['dragleave','drop','blur'].forEach(evt => dropzone.addEventListener(evt, () => dropzone.classList.remove('hover'), false));
  dropzone.addEventListener('click', () => fileInput.click());
  dropzone.addEventListener('drop', e => addFiles(e.dataTransfer.files));
  fileInput.addEventListener('change', e => addFiles(e.target.files));
  planMonthEl.addEventListener('change', renderPreviews);
  intervalEl.addEventListener('input', renderPreviews);
  clearBtn.addEventListener('click', () => { currentFiles = []; fileInput.value = ''; renderPreviews(); });

  form.addEventListener('submit', (e) => {
    e.preventDefault();
    const planCnt = calcPlanCount();
    if (currentFiles.length === 0) { showError('Lütfen en az 1 görsel yükleyin.'); return; }
    if (currentFiles.length > MAX_FILES) { showError(`En fazla ${MAX_FILES} görsel yükleyebilirsiniz.`); return; }
    if (planCnt > 0 && currentFiles.length > planCnt) {
      showError(`Seçilen aralıkla ${planCnt} tarih çıkıyor; ${currentFiles.length} görsel fazla.`); return;
    }
    submitBtn.disabled = true; submitBtn.textContent = 'Oluşturuluyor…'; clearError();

    const fd = new FormData(form);  // CSRF + tüm alanlar otomatik eklenir
    currentFiles.forEach(f => fd.append('files', f, f.name));

    fetch('/generate', { method: 'POST', body: fd })
      .then(async resp => {
        if (!resp.ok) {
          const text = await resp.text();
          if (resp.status === 413) throw new Error('Yükleme çok büyük (413).');
          if (resp.status === 502 || resp.status === 504) throw new Error('Zaman aşımı (502/504).');
          throw new Error('Sunucu hatası: ' + text.slice(0, 300));
        }
        const blob = await resp.blob();
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a'); a.href = url;
        const name = (document.getElementById('doc_name').value || 'Instagram_Plani') + '.docx';
        a.download = name; document.body.appendChild(a); a.click(); a.remove();
        URL.revokeObjectURL(url);

        submitBtn.textContent = 'Oluştur ve indir (.docx)'; submitBtn.disabled = false;
        currentFiles = []; fileInput.value = ''; renderPreviews();
      })
      .catch(err => {
        showError(err.message || 'Bilinmeyen hata');
        submitBtn.textContent = 'Oluştur ve indir (.docx)'; submitBtn.disabled = false;
      });
  });

  renderPreviews();
})();
