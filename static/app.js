// Basit yardımcılar
const $ = (sel, ctx=document) => ctx.querySelector(sel);
const $$ = (sel, ctx=document) => Array.from(ctx.querySelectorAll(sel));

function toast(msg, ms=2600){
  const t = $("#toast");
  if(!t) return;
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(t._timer);
  t._timer = setTimeout(()=>{ t.hidden = true; }, ms);
}

// Drag & Drop + önizleme
(function initDropzone(){
  const dz = $("#dropzone");
  const input = $("#images");
  const pickBtn = $("#pickBtn");
  const thumbs = $("#thumbs");
  if(!dz || !input || !thumbs) return;

  pickBtn?.addEventListener("click", ()=> input.click());

  const filesState = []; // File listesi (çıkarılabilir)

  function renderThumbs(){
    thumbs.innerHTML = "";
    filesState.forEach((file, idx)=>{
      const url = URL.createObjectURL(file);
      const div = document.createElement("div");
      div.className = "thumb";
      div.innerHTML = `<img src="${url}" alt="">
        <button type="button" class="x" aria-label="Kaldır">✕</button>`;
      div.querySelector(".x").addEventListener("click", ()=>{
        filesState.splice(idx, 1);
        renderThumbs();
      });
      thumbs.appendChild(div);
    });
  }

  function addFiles(list){
    for(const f of list){
      if(!f.type.startsWith("image/")) continue;
      filesState.push(f);
    }
    renderThumbs();
  }

  input.addEventListener("change", (e)=> addFiles(e.target.files || []));

  ["dragenter","dragover"].forEach(ev=> dz.addEventListener(ev, (e)=>{
    e.preventDefault(); e.stopPropagation(); dz.classList.add("drag");
  }));
  ["dragleave","drop"].forEach(ev=> dz.addEventListener(ev, (e)=>{
    e.preventDefault(); e.stopPropagation(); dz.classList.remove("drag");
  }));
  dz.addEventListener("drop", (e)=> addFiles(e.dataTransfer.files || []));

  // Form submit öncesi FileList'i yeniden yükle
  const form = $("#plan-form");
  if(form){
    form.addEventListener("submit", async (e)=>{
      e.preventDefault();
      const btn = $("#submitBtn");
      btn.disabled = true;
      try{
        const fd = new FormData(form);
        // input#images'i temizle ve state'deki dosyaları ekle
        // (Render için aynı alan adını koruyoruz)
        fd.delete("images");
        filesState.forEach(f => fd.append("images", f, f.name));

        const res = await fetch("/planla", { method:"POST", body: fd });
        const data = await res.json();
        if(!res.ok || !data.ok){
          toast(data.error || "Hata oluştu.");
        }else{
          toast("Plan başarıyla hazırlandı (örnek JSON döndü).");
          console.debug("Plan sonucu:", data.received);
        }
      }catch(err){
        console.error(err);
        toast("Ağ hatası.");
      }finally{
        btn.disabled = false;
      }
    });

    $("#resetBtn")?.addEventListener("click", ()=>{
      filesState.splice(0, filesState.length);
      renderThumbs();
      toast("Form temizlendi.");
    });
  }
})();
