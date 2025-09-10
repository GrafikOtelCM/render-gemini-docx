(function(){
  const dz = document.getElementById("dropzone");
  const fi = document.getElementById("file-input");
  const thumbs = document.getElementById("thumbs");
  const form = document.getElementById("upload-form");
  const btn = document.getElementById("btn-upload");

  let files = [];

  function renderThumbs(list){
    thumbs.innerHTML = "";
    list.forEach(file => {
      const div = document.createElement("div");
      div.className = "thumb";
      const img = document.createElement("img");
      div.appendChild(img);
      thumbs.appendChild(div);

      const reader = new FileReader();
      reader.onload = e => { img.src = e.target.result; };
      reader.readAsDataURL(file);
    });
  }

  dz.addEventListener("click", () => fi.click());

  dz.addEventListener("dragover", (e) => {
    e.preventDefault();
    dz.classList.add("dragover");
  });
  dz.addEventListener("dragleave", () => dz.classList.remove("dragover"));
  dz.addEventListener("drop", (e) => {
    e.preventDefault();
    dz.classList.remove("dragover");
    const dropped = Array.from(e.dataTransfer.files || []);
    if (dropped.length){
      files = files.concat(dropped);
      renderThumbs(files);
    }
  });

  fi.addEventListener("change", () => {
    const picked = Array.from(fi.files || []);
    if (picked.length){
      files = files.concat(picked);
      renderThumbs(files);
    }
  });

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!files.length){ alert("Önce görsel ekleyin."); return; }
    btn.disabled = true;

    const data = new FormData();
    data.append("csrf_token", window.CSRF_TOKEN || "");
    files.forEach(f => data.append("files", f));

    try{
      const res = await fetch("/upload", { method:"POST", body:data });
      const js = await res.json();
      if (!js.ok) throw new Error("Yükleme başarısız");
      alert(`Yüklendi: ${js.saved.length} dosya`);
      files = [];
      renderThumbs(files);
    }catch(err){
      console.error(err);
      alert("Hata: " + (err.message || "bilinmeyen"));
    }finally{
      btn.disabled = false;
    }
  });
})();
