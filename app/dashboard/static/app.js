// Общие fetch-хелперы: все формы шлют JSON/POST в /admin/* с Bearer из cookie.
function toast(t) {
  const el = document.getElementById("toast");
  el.textContent = t; el.style.display = "block";
  setTimeout(() => el.style.display = "none", 2600);
}
function token() {
  const m = document.cookie.match(/(?:^|; )admin_token=([^;]+)/);
  return m ? decodeURIComponent(m[1]) : "";
}
async function api(path, opts = {}) {
  const r = await fetch(path, {
    ...opts,
    headers: { "Content-Type": "application/json", "Authorization": "Bearer " + token(), ...(opts.headers || {}) },
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) { toast(data.detail || ("Ошибка " + r.status)); throw new Error(data.detail || r.status); }
  return data;
}
async function apiForm(path, form) {
  const fd = new FormData(form);
  const r = await fetch(path, {
    method: "POST", body: fd,
    headers: { "Authorization": "Bearer " + token() },
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) { toast(data.detail || ("Ошибка " + r.status)); throw new Error(data.detail || r.status); }
  toast("Сохранено");
  return data;
}
async function post(path) { const d = await api(path, { method: "POST" }); toast("Готово"); return d; }
