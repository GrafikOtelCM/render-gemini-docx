(function () {
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
  const toast = (msg, type = "info") => {
    const el = $("#toast");
    el.textContent = msg;
    el.className = "toast " + (type === "success" ? "alert-success" : type === "danger" ? "alert-danger" : "");
    el.hidden = false;
    setTimeout(() => (el.hidden = true), 3500);
  };

  // --- Dropzone & thumbs ---
  const dz = $("#dropzone");
  const pick = $("#pick-images");
  const input = $("#images");
  const thumbs = $("#thumbs");
  let files = [];

  function renderThumbs() {
    thumbs.innerHTML = "";
    if (!files.length) {
      thumbs.innerHTML = `<p class="muted small">Seçili görsel yok.</p>`;
      return;
    }
    files.forEach((f, idx) => {
      const url = URL.createObjectURL(f);
      const item = document.createElement("div");
      item.className = "thumb";
      item.innerHTML = `<img src="${url}" alt="${f.name}"><button class="x" data-i="${idx}">×</button>`;
      thumbs.appendChild(item);
    });
  }
  function addFiles(list) {
    for (const f of list) if (f && f.type.startsWith("image/")) files.push(f);
    renderThumbs();
  }
  thumbs.addEventListener("click", (e) => {
    if (e.target.matches(".x")) {
      const i = +e.target.dataset.i;
      files.splice(i, 1);
      renderThumbs();
    }
  });
  if (dz) {
    ["dragenter","dragover"].forEach(ev => dz.addEventListener(ev, e => { e.preventDefault(); dz.classList.add("drag"); }));
    ["dragleave","drop"].forEach(ev => dz.addEventListener(ev, e => { e.preventDefault(); dz.classList.remove("drag"); }));
    dz.addEventListener("drop", (e) => addFiles(e.dataTransfer.files));
  }
  if (pick && input) {
    pick.addEventListener("click", () => input.click());
    input.addEventListener("change", () => addFiles(input.files));
  }
  renderThumbs();

  // --- Plan form submit (JSON API varsa /api/plan’ı dener, yoksa /plan’a POST eder) ---
  const planForm = $("#plan-form");
  if (planForm) {
    planForm.addEventListener("submit", async (e) => {
      e.preventDefault();

      const data = new FormData();
      $$("input, select, textarea", planForm).forEach(el => {
        if (!el.name) return;
        if (el.type === "file") return;
        data.append(el.name, el.value);
      });
      files.forEach(f => data.append("images", f, f.name));

      const results = $("#results");
      results.innerHTML = `<p class="muted">İşleniyor…</p>`;

      // Önce /api/plan’ı dene
      let ok = false;
      if (window.__PLAN_ENDPOINTS__?.prefer) {
        try {
          const r = await fetch(window.__PLAN_ENDPOINTS__.prefer, { method: "POST", body: data, credentials: "include" });
          if (r.ok) {
            const j = await r.json();
            ok = true;
            paintResult(j);
            toast("Plan hazırlandı.", "success");
          }
        } catch {}
      }
      // Olmadıysa /plan’a form-post dene
      if (!ok && window.__PLAN_ENDPOINTS__?.fallback) {
        try {
          const r = await fetch(window.__PLAN_ENDPOINTS__.fallback, { method: "POST", body: data, credentials: "include" });
          if (r.ok) {
            const j = await r.json().catch(() => null);
            paintResult(j || { message: "Plan hazırlandı." });
            toast("Plan hazırlandı.", "success");
            ok = true;
          }
        } catch {}
      }
      if (!ok) {
        results.innerHTML = `<p class="muted">Bir hata oluştu.</p>`;
        toast("Hata: Plan oluşturulamadı.", "danger");
      }
    });

    function paintResult(j) {
      const results = $("#results");
      const links = [];
      if (j?.docx_url) links.push(`<a class="btn btn-primary" href="${j.docx_url}">DOCX indir</a>`);
      if (j?.xlsx_url) links.push(`<a class="btn" href="${j.xlsx_url}">Excel indir</a>`);
      if (!links.length) {
        results.innerHTML = `<div class="card card-padded"><pre>${escapeHTML(JSON.stringify(j, null, 2))}</pre></div>`;
      } else {
        results.innerHTML = `<div class="stack gap-2">
          <p class="muted">Çıktılar hazır:</p>
          <div class="flex gap-2">${links.join("")}</div>
        </div>`;
      }
    }
  }

  // --- Admin sayfası: listele / ekle / sil ---
  const userRows = $("#user-rows");
  async function loadUsers() {
    if (!userRows) return;
    userRows.innerHTML = `<tr><td colspan="5" class="muted">Yükleniyor…</td></tr>`;
    try {
      const r = await fetch(window.__ADMIN_ENDPOINTS__?.list || "/admin/users", { credentials: "include" });
      const j = await r.json();
      if (!Array.isArray(j) || !j.length) {
        userRows.innerHTML = `<tr><td colspan="5" class="muted">Kayıt yok.</td></tr>`;
        return;
      }
      userRows.innerHTML = j.map(u => `
        <tr>
          <td>${u.id}</td>
          <td>${escapeHTML(u.username)}</td>
          <td>${u.role}</td>
          <td>${u.created_at || "-"}</td>
          <td><button class="btn small" data-del="${u.id}">Sil</button></td>
        </tr>
      `).join("");
    } catch {
      userRows.innerHTML = `<tr><td colspan="5" class="muted">Liste alınamadı.</td></tr>`;
    }
  }
  $("#refresh-users")?.addEventListener("click", loadUsers);
  document.addEventListener("click", async (e) => {
    const id = e.target?.dataset?.del;
    if (!id) return;
    if (!confirm("Silmek istediğinize emin misiniz?")) return;
    try {
      const r = await fetch((window.__ADMIN_ENDPOINTS__?.remove?.(id)) || `/admin/users/${id}`, { method: "DELETE", credentials: "include" });
      if (r.ok) { toast("Silindi", "success"); loadUsers(); } else toast("Silinemedi", "danger");
    } catch { toast("Silinemedi", "danger"); }
  });
  $("#user-form")?.addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd = new FormData(e.currentTarget);
    const payload = Object.fromEntries(fd.entries());
    try {
      const r = await fetch(window.__ADMIN_ENDPOINTS__?.create || "/admin/users", {
        method: "POST",
        headers: {"Content-Type":"application/json"},
        body: JSON.stringify(payload),
        credentials: "include"
      });
      if (r.ok) {
        e.currentTarget.reset();
        toast("Kullanıcı eklendi", "success");
        loadUsers();
      } else toast("Eklenemedi", "danger");
    } catch { toast("Eklenemedi", "danger"); }
  });
  loadUsers();

  // Utils
  function escapeHTML(s){return String(s).replace(/[&<>"'`=\/]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;','/':'&#x2F;','`':'&#x60;','=':'&#x3D;'}[c]))}
})();
