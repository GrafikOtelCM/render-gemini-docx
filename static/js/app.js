// Hızlı ay seçimi: açılır listede içinde bulunduğumuz ay + 5 ay daha
(function setupMonthQuick() {
  const sel = document.querySelector('#month-quick');
  if (!sel) return;
  const targetSel = sel.getAttribute('data-target');
  const target = document.querySelector(targetSel);
  if (!target) return;

  const today = new Date();
  for (let i = -1; i <= 6; i++) {
    const d = new Date(today.getFullYear(), today.getMonth() + i, 1);
    const val = d.toISOString().slice(0,10); // YYYY-MM-01
    const name = d.toLocaleDateString('tr-TR', { month:'long', year:'numeric' });
    const opt = document.createElement('option');
    opt.value = val; opt.textContent = name.charAt(0).toUpperCase() + name.slice(1);
    sel.appendChild(opt);
  }
  sel.addEventListener('change', () => {
    if (sel.value) target.value = sel.value;
  });
})();

// Sürükle-bırak çoklu görsel önizleme (küçük kutular)
(function setupUploader(){
  const zone = document.querySelector('[data-uploader]');
  if (!zone) return;

  const fileInput = zone.querySelector('input[type="file"]');
  const drop = zone.querySelector('[data-drop]');
  const thumbs = zone.querySelector('[data-thumbs]');
  const browseBtn = zone.querySelector('[data-browse]');

  const dataTransfer = new DataTransfer();

  function renderThumb(file, idx){
    const url = URL.createObjectURL(file);
    const item = document.createElement('div');
    item.className = 'thumb';
    item.innerHTML = `<img src="${url}" alt="">
      <button type="button" class="del">Sil</button>`;
    const btn = item.querySelector('.del');
    btn.addEventListener('click', () => {
      // listeden çıkar
      const files = Array.from(dataTransfer.files);
      files.splice(idx,1);
      const dt = new DataTransfer();
      files.forEach(f => dt.items.add(f));
      fileInput.files = dt.files;
      thumbs.innerHTML = '';
      Array.from(fileInput.files).forEach((f,i)=> renderThumb(f,i));
    });
    thumbs.appendChild(item);
  }

  function appendFiles(list){
    const current = Array.from(dataTransfer.files);
    Array.from(list).forEach(f => {
      if (!f.type.startsWith('image/')) return;
      current.push(f);
    });
    const dt = new DataTransfer();
    current.forEach(f => dt.items.add(f));
    fileInput.files = dt.files;
    thumbs.innerHTML = '';
    Array.from(fileInput.files).forEach((f,i)=> renderThumb(f,i));
  }

  browseBtn?.addEventListener('click', () => fileInput.click());
  fileInput.addEventListener('change', e => appendFiles(e.target.files));

  ;['dragenter','dragover'].forEach(ev => {
    drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.add('drag'); });
  });
  ;['dragleave','drop'].forEach(ev => {
    drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.remove('drag'); });
  });
  drop.addEventListener('drop', e => {
    const files = e.dataTransfer?.files;
    if (files && files.length) appendFiles(files);
  });
})();

// Form gönderiminde butonu kilitle (çoklu tıklama önle)
(function protectSubmit(){
  const form = document.getElementById('plan-form');
  if (!form) return;
  const btn = document.getElementById('submit-btn');
  form.addEventListener('submit', () => {
    if (btn){ btn.disabled = true; btn.textContent = 'Oluşturuluyor…'; }
  });
})();
