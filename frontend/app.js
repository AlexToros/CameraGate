"use strict";
const root = document.querySelector("#app"), modal = document.querySelector("#modal");
const h = value => String(value ?? "").replace(/[&<>"\x27]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","\x27":"&#39;"}[c]));
let me, csrf, state, page = "wall", adminTab = "users", socket, socketEpoch = 0, saveTimer, saving = Promise.resolve(), refreshTimer, installPrompt, reconnectTimer;
const images = new Map();
const cameraRatios = new Map();
let tileObserver, previewCameraId = null;
let dragged;
let routeEpoch = 0;
const roleName = role => role === "admin" ? "Администратор" : "Пользователь";
const uid = () => crypto.randomUUID().replaceAll("-", "");
function showInstallButton() {
  const mobile = navigator.userAgentData?.mobile || /Android|iPhone|iPad|iPod/i.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
  return mobile && !matchMedia("(display-mode: standalone)").matches && navigator.standalone !== true;
}
function toast(message) { const box = document.querySelector("#notice"); box.textContent = message; box.classList.add("visible"); clearTimeout(box.timer); box.timer = setTimeout(() => box.classList.remove("visible"), 4500); }
async function api(path, method = "GET", data) {
  let response;
  try { response = await fetch(path, {method, credentials:"same-origin", cache:"no-store", headers:{"Content-Type":"application/json", ...(csrf ? {"X-CSRF-Token":csrf} : {})}, ...(data === undefined ? {} : {body:JSON.stringify(data)})}); }
  catch { const error = new Error("Нет связи с сервером. Проверьте подключение к сети и повторите попытку."); error.network = true; throw error; }
  let result;
  try { result = await response.json(); } catch { throw new Error("Сервис временно недоступен"); }
  if (!response.ok) { const error = new Error(result.error || "Не удалось выполнить запрос"); error.status = response.status; if (response.status === 401 && me && path !== "/api/auth/login") signedOut(result.error); throw error; }
  return result;
}
function fields(form) { return Object.fromEntries(new FormData(form)); }
function bindForm(form, submit) {
  form.addEventListener("submit", async event => {
    event.preventDefault(); const button = form.querySelector("button[type=submit]"), error = form.querySelector(".error");
    if (error) error.textContent = ""; button.disabled = true;
    try { await submit(fields(form)); } catch (e) { if (error) error.textContent = e.message; else toast(e.message); } finally { button.disabled = false; }
  });
}
function dialog(title, body) { previewCameraId = null; modal.classList.remove("camera-preview"); modal.setAttribute("aria-label",title); modal.innerHTML = `<button class="close" aria-label="Закрыть">×</button><h2>${h(title)}</h2>${body}`; modal.querySelector(".close").onclick = () => modal.close(); modal.showModal(); }
modal.addEventListener("click", event => { if (event.target === modal) { const rect = modal.getBoundingClientRect(); if(event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) modal.close(); } });
modal.addEventListener("close", () => { previewCameraId = null; modal.classList.remove("camera-preview"); });
function stopVideo() { clearTimeout(reconnectTimer); socketEpoch++; if(socket) socket.close(); socket = null; for(const image of images.values()) URL.revokeObjectURL(image.url); images.clear(); }
function signedOut(message) { routeEpoch++; tileObserver?.disconnect(); document.body.classList.remove("fullscreen"); me = null; csrf = null; state = null; clearInterval(refreshTimer); clearTimeout(saveTimer); stopVideo(); if(modal.open) modal.close(); authView("login", null, message); }
function authView(mode, invitation, message = "") {
  const setup = mode === "setup", invite = mode === "invite";
  root.innerHTML = `<main class="auth"><div class="brand-mark" aria-hidden="true">▦</div><h1>${setup ? "Первый администратор" : invite ? "Регистрация" : "Домовые камеры"}</h1><p class="muted">${setup ? "Создайте учётную запись, чтобы настроить доступ к камерам." : invite ? `Здравствуйте, ${h(invitation.last_name)} ${h(invitation.first_name)}!<br>Введите логин, который будете использовать для входа, и задайте пароль.` : "Войдите, чтобы открыть свою видеостену."}</p><form class="stack" id="auth-form">${setup ? `<div class="two"><label>Фамилия<input name="last_name" autocomplete="family-name" required maxlength="100"></label><label>Имя<input name="first_name" autocomplete="given-name" required maxlength="100"></label></div>` : ""}<label>Логин<input name="login" autocomplete="username" autocapitalize="none" spellcheck="false" pattern="[A-Za-z0-9._\\-]{3,64}" minlength="3" maxlength="64" required></label><label>Пароль<input name="password" type="password" autocomplete="${mode === "login" ? "current-password" : "new-password"}" ${mode === "login" ? "" : "minlength=6"} maxlength="128" required></label>${mode !== "login" ? `<label>Повторите пароль<input name="password_confirmation" type="password" autocomplete="new-password" minlength="6" maxlength="128" required></label><small>От 6 символов. Сохраните пароль в менеджере паролей.</small>` : ""}<div class="error" role="alert">${h(message)}</div><button class="primary" type="submit">${setup ? "Создать администратора" : invite ? "Создать учётную запись" : "Войти"}</button></form><div class="install-help">Доступ выдаёт администратор по приглашению. ${showInstallButton() ? `<button id="install" type="button">Установить приложение</button>` : ""}</div></main>`;
  const epoch = routeEpoch;
  bindForm(document.querySelector("#auth-form"), async data => {
    if(mode !== "login" && data.password !== data.password_confirmation) throw new Error("Пароли не совпадают");
    await api(`/api/auth/${setup ? "setup" : invite ? "register" : "login"}`, "POST", {...data, ...(invite ? {token:invitation.token} : {})});
    if(epoch !== routeEpoch) return;
    history.replaceState(null, "", "/"); await enter(epoch);
  });
  const installButton = document.querySelector("#install");
  if (installButton) installButton.onclick = install;
}
async function install() {
  if (installPrompt) { await installPrompt.prompt(); installPrompt = null; }
  else toast("iPhone: «Поделиться» → «На экран Домой». Android: меню браузера → «Установить приложение».");
}
async function boot() {
  const epoch = ++routeEpoch;
  tileObserver?.disconnect(); document.body.classList.remove("fullscreen");
  stopVideo(); clearInterval(refreshTimer); clearTimeout(saveTimer);
  me = null; csrf = null; state = null;
  if(modal.open) modal.close();
  root.innerHTML = `<main class="auth"><div class="brand-mark" aria-hidden="true">▦</div><h1>Домовые камеры</h1><p role="status">Открываем приложение…</p></main>`;
  try {
    const token = location.hash.startsWith("#/invite/") ? location.hash.slice(9) : null;
    if(token !== null) { const preview = await api("/api/invite/preview", "POST", {token}); if(epoch === routeEpoch) authView("invite", {...preview,token}); return; }
    try { await enter(epoch); return; }
    catch(e) { if(epoch !== routeEpoch) return; if(e.status !== 401) throw e; }
    const bootstrap = await api("/api/bootstrap");
    if(epoch !== routeEpoch) return;
    authView(bootstrap.setup_required ? "setup" : "login");
  } catch(e) { if(epoch !== routeEpoch) return; const offline = e.network || !navigator.onLine; root.innerHTML = `<main class="auth"><div class="brand-mark">▦</div><h1>${offline ? "Нет связи с сервером" : "Не удалось открыть приложение"}</h1><p>${h(offline ? "Для входа и просмотра камер нужен интернет и доступ к серверу. Проверьте соединение. Ваши настройки сохранены на сервере." : e.message)}</p><button id="retry" class="primary">Повторить</button><p class="install-help"><a href="/">Перейти ко входу</a></p></main>`; document.querySelector("#retry").onclick = boot; }
}
async function enter(epoch = routeEpoch) { const result = await api("/api/me"), next = await api("/api/state"); if(epoch !== routeEpoch) return; me = result.user; csrf = result.csrf; state = next; page = "wall"; render(); clearInterval(refreshTimer); refreshTimer = setInterval(refresh, 15000); }
async function refresh() {
  if(!me || document.hidden) return;
  const epoch = routeEpoch;
  try {
    const current = await api("/api/me");
    if(epoch !== routeEpoch || !state) return;
    const next = await api("/api/state");
    if(epoch !== routeEpoch || !state) return;
    me = current.user; csrf = current.csrf;
    const changed = JSON.stringify(next.cameras) !== JSON.stringify(state.cameras) || next.configured !== state.configured || (page === "admin" && me.role !== "admin");
    state.groups = next.groups; state.cameras = next.cameras; state.configured = next.configured; state.connection_state = next.connection_state;
    const allowed = new Set(state.cameras.map(c => c.id));
    for(const tile of state.layout.tiles) if(tile.camera_id && !allowed.has(tile.camera_id)) tile.camera_id = null;
    if(changed) { if(me.role !== "admin") page = "wall"; render(); }
  } catch(e) { if(e.status !== 401) toast(navigator.onLine ? e.message : "Нет подключения к сети"); }
}
function render() {
  tileObserver?.disconnect();
  if(!me || !state) return;
  stopVideo();
  root.innerHTML = `<header class="header"><div class="brand"><span class="symbol" aria-hidden="true">▦</span><div><strong>Домовые камеры</strong><div class="identity">${h(me.first_name)} ${h(me.last_name)}</div></div></div><div class="row"><nav class="tabs" aria-label="Разделы"><button data-page="wall" class="${page === "wall" ? "active" : ""}">Прямой эфир</button>${me.role === "admin" ? `<button data-page="admin" class="${page === "admin" ? "active" : ""}">Управление</button>` : ""}</nav><button id="logout">Выйти</button></div></header><main id="content"></main>`;
  document.querySelectorAll("[data-page]").forEach(button => {
    if(button.dataset.page === page) button.setAttribute("aria-current","page");
    button.onclick = async () => { if(button.dataset.page === page) return; const epoch = routeEpoch; try { await flushSave(); if(epoch !== routeEpoch || !me || !button.isConnected) return; if(modal.open) modal.close(); page = button.dataset.page; render(); } catch(e) { toast(e.message); } };
  });
  document.querySelector("#logout").onclick = async () => { try { await flushSave(); await api("/api/auth/logout","POST",{}); signedOut(); } catch(e) { toast(e.message); } };
  if(page === "wall") wall(); else admin();
}
const icons = {
  more: '<circle cx="5" cy="12" r="1.5"/><circle cx="12" cy="12" r="1.5"/><circle cx="19" cy="12" r="1.5"/>',
  expand: '<path d="M8 3H3v5m13-5h5v5M3 16v5h5m13-5v5h-5"/>',
  grid: '<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/>',
  down: '<path d="m8 10 4 4 4-4"/>',
  plus: '<path d="M12 5v14M5 12h14"/>'
};
const icon = name => `<svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">${icons[name]}</svg>`;
const screenCount = n => `${n} ${n === 1 ? "экран" : n < 5 ? "экрана" : "экранов"}`;
function wall() {
  const content = document.querySelector("#content");
  content.innerHTML = `<div class="toolbar"><div class="row"><button id="preset">${icon("grid")}<span>Сетка</span></button><button id="add-tile">${icon("plus")}<span>Экран</span></button></div><button id="fullscreen" class="icon-button" title="Во весь экран" aria-label="Во весь экран">${icon("expand")}</button></div>${!state.configured ? `<div class="banner">Подключение к камерам не настроено. ${me.role === "admin" ? "Введите параметры в разделе «Управление → Подключение»." : "Обратитесь к администратору."}</div>` : !state.cameras.length ? `<div class="banner">${me.role === "admin" ? h(state.connection_state) : "Доступных камер пока нет. Обратитесь к администратору."}</div>` : ""}<div class="wall" id="wall" aria-label="Камеры"></div><button class="exit-fullscreen" aria-label="Выйти из полноэкранного режима">Закрыть</button>`;
  document.querySelector("#add-tile").onclick = () => {
    if(state.layout.tiles.length >= 64) return toast("Можно добавить до 64 экранов");
    const tile = {id:uid(),camera_id:null,width:1,height:1};
    state.layout.tiles.push(tile); renderTiles(); changed(); chooseCamera(tile);
  };
  document.querySelector("#preset").onclick = gridOptions;
  document.querySelector("#fullscreen").onclick = async () => { document.body.classList.add("fullscreen"); try { await document.documentElement.requestFullscreen(); } catch {} };
  document.querySelector(".exit-fullscreen").onclick = () => { document.body.classList.remove("fullscreen"); if(document.fullscreenElement) document.exitFullscreen(); };
  renderTiles(); connectVideo();
}
function preferredCameras() {
  const group = me.role === "admin" ? state.groups.find(g => g.builtin) : null;
  if(!group) return state.cameras;
  const members = new Set(group.camera_ids);
  return state.cameras.filter(camera => members.has(camera.id));
}
function applyPreset(count) {
  const existing = state.layout.tiles;
  const selected = new Set(existing.map(t => t.camera_id).filter(Boolean));
  const available = preferredCameras().filter(c => !selected.has(c.id));
  let cursor = 0;
  state.layout = {columns:count === 6 ? 3 : Math.sqrt(count),tiles:Array.from({length:count},(_,i) => ({id:existing[i]?.id || uid(),camera_id:existing[i]?.camera_id ?? available[cursor++]?.id ?? null,width:1,height:1}))};
  modal.close(); renderTiles(); changed(); subscribe();
}
function gridOptions() {
  dialog("Сетка",`<div class="presets">${[1,4,6,9,16].map(n => `<button data-preset="${n}" aria-label="${screenCount(n)}"><span class="preset-icon preset-${n}" aria-hidden="true">${Array.from({length:n},() => "<i></i>").join("")}</span><span>${n}</span></button>`).join("")}</div><form class="stack"><label>Колонки<select name="columns">${Array.from({length:12},(_,i) => `<option ${state.layout.columns === i+1 ? "selected" : ""}>${i+1}</option>`).join("")}</select></label><div class="error"></div><div class="actions"><button type="submit" class="primary">Применить</button></div></form>`);
  modal.querySelectorAll("[data-preset]").forEach(button => button.onclick = () => applyPreset(+button.dataset.preset));
  bindForm(modal.querySelector("form"),data => { state.layout.columns = +data.columns; modal.close(); renderTiles(); changed(); });
}
function renderTiles() {
  const wall = document.querySelector("#wall"); if(!wall) return;
  tileObserver?.disconnect();
  const columns = Math.min(state.layout.columns,Math.max(1,Math.floor((wall.clientWidth-16)/150)));
  wall.style.setProperty("--columns",columns);
  const cameras = new Map(state.cameras.map(c => [c.id,c]));
  wall.innerHTML = state.layout.tiles.length ? state.layout.tiles.map((tile,index) => {
    const name = cameras.get(tile.camera_id)?.name || "Выбрать камеру";
    return `<article class="tile" data-tile="${h(tile.id)}" aria-label="Экран ${index+1}"><div class="tile-head" draggable="true"><span class="status-dot" aria-hidden="true"></span><button class="camera-trigger" aria-label="Камера экрана ${index+1}: ${h(name)}" title="${h(name)}">${h(name)}${icon("down")}</button><button class="tile-menu icon-button" title="Настройки экрана" aria-label="Настройки экрана ${index+1}">${icon("more")}</button></div><div class="picture"><img alt="" hidden><span class="stream-state">${tile.camera_id ? "Подключение…" : "Выбрать камеру"}</span><button class="tile-expand icon-button" title="Увеличить" aria-label="Увеличить экран ${index+1}">${icon("expand")}</button></div></article>`;
  }).join("") : `<div class="empty"><p>Добавьте экран или выберите сетку.</p><button class="primary" id="empty-add">Добавить экран</button></div>`;
  if(!state.layout.tiles.length) { document.querySelector("#empty-add").onclick = () => document.querySelector("#add-tile").click(); return; }
  const tilesById = new Map(state.layout.tiles.map(t => [t.id,t]));
  tileObserver = new ResizeObserver(entries => {
    // ResizeObserver provides the measurements; no forced read/write layout loop.
    const gap = parseFloat(getComputedStyle(wall).rowGap) || 4;
    const spans = entries.map(entry => ({element:entry.target,rows:Math.ceil(((entry.borderBoxSize?.[0]?.blockSize ?? entry.contentRect.height)+gap)/(2+gap))}));
    spans.forEach(({element,rows}) => element.style.gridRowEnd = `span ${rows}`);
  });
  wall.querySelectorAll(".tile").forEach(element => {
    const tile = tilesById.get(element.dataset.tile);
    element.style.setProperty("--width",Math.min(tile.width,columns));
    element.style.setProperty("--ratio",cameraRatios.get(tile.camera_id) || 16/9);
    element.querySelector(".camera-trigger").onclick = () => { if(!dragged) chooseCamera(tile); };
    element.querySelector(".tile-menu").onclick = () => tileOptions(tile);
    element.querySelector(".tile-expand").onclick = () => tile.camera_id ? previewCamera(tile) : chooseCamera(tile);
    element.querySelector(".picture").ondblclick = () => { if(tile.camera_id) previewCamera(tile); };
    const header = element.querySelector(".tile-head");
    header.ondragstart = event => { dragged = tile.id; event.dataTransfer.setData("text/plain",tile.id); element.classList.add("dragging"); };
    header.ondragend = () => { dragged = null; wall.querySelectorAll(".tile").forEach(t => t.classList.remove("dragging","drop")); };
    element.ondragover = event => { if(dragged && dragged !== tile.id) { event.preventDefault(); element.classList.add("drop"); } };
    element.ondragleave = () => element.classList.remove("drop");
    element.ondrop = event => { event.preventDefault(); const from = state.layout.tiles.findIndex(t => t.id === dragged), to = state.layout.tiles.findIndex(t => t.id === tile.id); if(from>=0 && to>=0) { state.layout.tiles.splice(to,0,state.layout.tiles.splice(from,1)[0]); dragged = null; renderTiles(); changed(); } };
    tileObserver.observe(element);
    drawCached(tile,element);
  });
}
function chooseCamera(tile) {
  dialog("Выбрать камеру",`<div class="camera-filters"><input type="search" id="camera-search" placeholder="Найти камеру" aria-label="Найти камеру" autocomplete="off"><select id="camera-group" aria-label="Группа камер"><option value="0">Все доступные</option>${state.groups.map(g => `<option value="${g.id}" ${me.role === "admin" && g.builtin ? "selected" : ""}>${h(g.name)}</option>`).join("")}</select></div><div class="camera-choices" id="camera-choices"></div>`);
  const groups = new Map(state.groups.map(g => [g.id,new Set(g.camera_ids)]));
  const onWall = new Set(state.layout.tiles.map(t => t.camera_id).filter(Boolean));
  function filter() {
    const query = modal.querySelector("#camera-search").value.trim().toLocaleLowerCase();
    const gid = +modal.querySelector("#camera-group").value, members = groups.get(gid);
    const unused = [], used = [];
    for(const camera of state.cameras) {
      if((gid && !members?.has(camera.id)) || !camera.name.toLocaleLowerCase().includes(query)) continue;
      (onWall.has(camera.id) ? used : unused).push(camera);
    }
    const cameras = [...unused,...used];
    const list = modal.querySelector("#camera-choices");
    list.innerHTML = cameras.map(c => `<button data-camera="${c.id}" class="camera-choice ${c.id === tile.camera_id ? "selected" : ""}" aria-pressed="${c.id === tile.camera_id}"${onWall.has(c.id) ? ' aria-label="'+h(c.name)+' — на экране"' : ''}><span>${h(c.name)}</span>${onWall.has(c.id) ? '<span class="camera-on-wall" title="На экране" aria-hidden="true"></span>' : '<span aria-hidden="true">+</span>'}</button>`).join("") || `<p class="empty">Камеры не найдены</p>`;
    list.querySelectorAll("[data-camera]").forEach(button => button.onclick = () => { tile.camera_id = +button.dataset.camera; modal.close(); renderTiles(); changed(); subscribe(); });
  }
  modal.querySelector("#camera-search").oninput = filter;
  modal.querySelector("#camera-group").onchange = filter;
  filter();
}
function tileOptions(tile) {
  const position = state.layout.tiles.indexOf(tile)+1;
  dialog("Экран",`<form class="stack"><div class="two"><label>Ширина, колонок<select name="width">${Array.from({length:12},(_,i) => `<option ${tile.width === i+1 ? "selected" : ""}>${i+1}</option>`).join("")}</select></label><label>Позиция<select name="position">${state.layout.tiles.map((_,i) => `<option ${position === i+1 ? "selected" : ""}>${i+1}</option>`).join("")}</select></label></div><div class="error"></div><div class="actions"><button type="button" id="remove-current-tile" class="danger">Удалить экран</button><button type="submit" class="primary">Применить</button></div></form>`);
  modal.querySelector("#remove-current-tile").onclick = () => { state.layout.tiles = state.layout.tiles.filter(t => t.id !== tile.id); modal.close(); renderTiles(); changed(); subscribe(); };
  bindForm(modal.querySelector("form"), data => { tile.width = +data.width; tile.height = 1; const index = state.layout.tiles.indexOf(tile); state.layout.tiles.splice(index,1); state.layout.tiles.splice(+data.position-1,0,tile); modal.close(); renderTiles(); changed(); });
}
function previewCamera(tile) {
  const camera = state.cameras.find(c => c.id === tile.camera_id); if(!camera) return;
  dialog(camera.name,`<div class="preview tile"><div class="picture"><img alt="" hidden><span class="stream-state">Подключение…</span></div></div>`);
  modal.classList.add("camera-preview"); previewCameraId = tile.camera_id;
  drawCached({camera_id:previewCameraId},modal.querySelector(".preview"));
}
function changed() { clearTimeout(saveTimer); saveTimer = setTimeout(save,500); }
function save() {
  clearTimeout(saveTimer); saveTimer = null; if(!me || !state) return saving;
  const value = structuredClone(state.layout);
  saving = saving.catch(() => {}).then(() => api("/api/layout","PUT",value)).catch(e => toast(e.message));
  return saving;
}
async function flushSave() { if(saveTimer) { clearTimeout(saveTimer); saveTimer = null; return save(); } return saving; }
function clearTile(element) { element.classList.remove("live"); const image = element.querySelector("img"), replacement = new Image(); replacement.alt = ""; replacement.hidden = true; image.onload = null; image.removeAttribute("src"); image.replaceWith(replacement); element.querySelector(".stream-state").textContent = "Подключение…"; }
function drawCached(tile, element) {
  const cached = images.get(tile.camera_id);
  if(!cached || Date.now()-cached.at>5000) return;
  const image = element.querySelector("img"), expectedCamera = tile.camera_id, expectedURL = cached.url;
  image.onload = () => { if(tile.camera_id === expectedCamera && image.src === expectedURL && element.isConnected && element.querySelector("img") === image) { cameraRatios.set(expectedCamera,image.naturalWidth/image.naturalHeight); element.style.setProperty("--ratio",image.naturalWidth/image.naturalHeight); image.hidden = false; element.classList.add("live"); } };
  image.src = expectedURL;
}
function subscriptions() { return [...new Set(state.layout.tiles.map(t => t.camera_id).filter(Boolean))]; }
function subscribe() {
  const active = new Set(subscriptions());
  for(const [cid,value] of images) if(!active.has(cid)) { URL.revokeObjectURL(value.url); images.delete(cid); }
  if(socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify({csrf,camera_ids:[...active]}));
}
function connectVideo() {
  stopVideo(); if(!me || page !== "wall" || document.hidden) return;
  const epoch = socketEpoch, ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/api/live`); socket = ws; ws.binaryType = "arraybuffer";
  ws.onopen = subscribe;
  ws.onmessage = event => {
    if(epoch !== socketEpoch || !state) return;
    if(typeof event.data === "string") {
      const message = JSON.parse(event.data);
      if(message.type === "revoked") { const denied = new Set(message.camera_ids); for(const tile of state.layout.tiles) if(denied.has(tile.camera_id)) tile.camera_id = null; if(denied.has(previewCameraId)) modal.close(); renderTiles(); subscribe(); refresh(); toast("Права доступа к камерам изменились"); }
      if(message.type === "status") {
        const tiles = new Map(state.layout.tiles.map(t => [t.id,t]));
        document.querySelectorAll(".tile").forEach(element => { const cid = tiles.get(element.dataset.tile)?.camera_id, status = message.cameras[cid]; if(status && status !== "Прямой эфир") { clearTile(element); element.querySelector(".stream-state").textContent = status; } });
      }
      if(message.type === "status" && previewCameraId && message.cameras[previewCameraId] && message.cameras[previewCameraId] !== "Прямой эфир") { const preview = modal.querySelector(".preview"); if(preview) { clearTile(preview); preview.querySelector(".stream-state").textContent = message.cameras[previewCameraId]; } }
      return;
    }
    const view = new DataView(event.data), active = new Set(subscriptions()), tiles = new Map(state.layout.tiles.map(t => [t.id,t])), incoming = new Map();
    let offset = 0;
    while(offset+8<=view.byteLength) { const cid = view.getUint32(offset), length = view.getUint32(offset+4); offset += 8; if(length>8*1024*1024 || offset+length>view.byteLength) break; if(active.has(cid)) { const old = images.get(cid); const value = {url:URL.createObjectURL(new Blob([event.data.slice(offset,offset+length)],{type:"image/jpeg"})),at:Date.now()}; images.set(cid,value); incoming.set(cid,value); if(old) setTimeout(() => URL.revokeObjectURL(old.url),1500); } offset += length; }
    document.querySelectorAll(".tile").forEach(element => { const tile = tiles.get(element.dataset.tile); if(incoming.has(tile?.camera_id)) drawCached(tile,element); });
    if(previewCameraId && incoming.has(previewCameraId)) { const preview = modal.querySelector(".preview"); if(preview) drawCached({camera_id:previewCameraId},preview); }
  };
  ws.onclose = event => { if(epoch !== socketEpoch) return; document.querySelectorAll(".tile").forEach(clearTile); if(event.code === 4401) signedOut("Сессия завершена. Войдите снова"); else if(event.code === 4403) { toast("Права доступа изменились"); refresh().then(() => { reconnectTimer = setTimeout(connectVideo,1500); }); } else reconnectTimer = setTimeout(connectVideo,2000); };
}
function accessFields(value = {}) {
  return `<div class="access"><p class="access-note">Администраторы всегда видят все камеры. Для пользователя доступ складывается из выбранных групп и отдельных камер.</p><h3>Группы камер</h3><div class="checks">${state.groups.map(g => `<label><input type="checkbox" name="group_ids" value="${g.id}" ${(value.group_ids || []).includes(g.id) ? "checked" : ""}>${h(g.name)}</label>`).join("") || "Групп пока нет"}</div><h3>Отдельные камеры</h3><div class="checks">${state.cameras.map(c => `<label><input type="checkbox" name="camera_ids" value="${c.id}" ${(value.camera_ids || []).includes(c.id) ? "checked" : ""}>${h(c.name)}</label>`).join("") || "Камер пока нет"}</div></div>`;
}
function accessValue(form) { return {camera_ids:[...form.querySelectorAll("[name=camera_ids]:checked")].map(e => +e.value), group_ids:[...form.querySelectorAll("[name=group_ids]:checked")].map(e => +e.value)}; }
async function admin(refreshState = false) {
  if(!me || me.role !== "admin" || page !== "admin") return;
  const content = document.querySelector("#content");
  content.innerHTML = `<section class="admin"><nav class="admin-nav" aria-label="Управление">${Object.entries({users:"Пользователи",invites:"Приглашения",groups:"Группы",cameras:"Камеры",connection:"Подключение"}).map(([key,label]) => `<button data-tab="${key}" class="${key === adminTab ? "active" : ""}">${label}</button>`).join("")}</nav><div id="admin-body" class="surface"><p class="muted">Загрузка…</p></div></section>`;
  content.querySelectorAll("[data-tab]").forEach(button => {
    if(button.dataset.tab === adminTab) button.setAttribute("aria-current","page");
    button.onclick = () => { if(button.dataset.tab === adminTab) return; adminTab = button.dataset.tab; admin(true); };
  });
  const body = document.querySelector("#admin-body"), tab = adminTab;
  try {
    if(refreshState) {
      const next = await api("/api/state");
      if(!body.isConnected || tab !== adminTab || !state) return;
      state = {...state,...next,layout:state.layout};
    }
    if(tab === "users") {
      const data = await api("/api/admin/users"); if(!body.isConnected || tab !== adminTab) return;
      body.innerHTML = `<div class="row between"><h2>Пользователи</h2><button id="new-invite" class="primary">Пригласить</button></div><div class="table-wrap"><table><thead><tr><th>Имя и логин</th><th>Роль</th><th>Доступ</th><th></th></tr></thead><tbody>${data.users.map(u => `<tr><td>${h(u.last_name)} ${h(u.first_name)}<span class="muted">${h(u.login)}</span></td><td><span class="pill ${u.blocked ? "blocked" : ""}">${u.blocked ? "Заблокирован" : roleName(u.role)}</span></td><td>${u.role === "admin" ? "Все камеры" : `${u.group_ids.length} групп, ${u.camera_ids.length} камер`}</td><td><button data-user="${u.id}">Изменить</button></td></tr>`).join("")}</tbody></table></div>`;
      body.querySelector("#new-invite").onclick = inviteForm; const users = new Map(data.users.map(u => [u.id,u])); body.querySelectorAll("[data-user]").forEach(button => button.onclick = () => userForm(users.get(+button.dataset.user)));
    } else if(tab === "invites") {
      const data = await api("/api/admin/invites"); if(!body.isConnected || tab !== adminTab) return;
      body.innerHTML = `<div class="row between"><h2>Приглашения</h2><button id="new-invite" class="primary">Создать приглашение</button></div><p class="muted">Ссылка действует семь дней и позволяет создать только одну учётную запись. Скопировать её можно сразу после создания.</p>${data.invitations.length ? `<div class="table-wrap"><table><thead><tr><th>Получатель</th><th>Роль</th><th></th></tr></thead><tbody>${data.invitations.map(i => `<tr><td>${h(i.last_name)} ${h(i.first_name)}</td><td>${roleName(i.role)}</td><td><button data-revoke="${i.id}" class="danger">Отозвать</button></td></tr>`).join("")}</tbody></table></div>` : `<p class="empty">Приглашений пока нет</p>`}`;
      body.querySelector("#new-invite").onclick = inviteForm; body.querySelectorAll("[data-revoke]").forEach(button => button.onclick = async () => { try { await api(`/api/admin/invites/${button.dataset.revoke}`,"DELETE",{}); admin(); } catch(e) { toast(e.message); } });
    } else if(tab === "groups") {
      body.innerHTML = `<div class="row between"><h2>Группы камер</h2><button id="new-group" class="primary">Создать группу</button></div><p class="muted">Доступ к группе включает камеры, выбранные в её составе.</p><div class="table-wrap"><table><thead><tr><th>Название</th><th>Камер</th><th></th></tr></thead><tbody>${state.groups.map(g => `<tr><td>${h(g.name)}${g.builtin ? `<span class="muted">Основной список для просмотра</span>` : ""}</td><td>${g.camera_ids.length}</td><td><button data-group="${g.id}">Изменить</button></td></tr>`).join("")}</tbody></table></div>`;
      body.querySelector("#new-group").onclick = () => groupForm(); const groups = new Map(state.groups.map(g => [g.id,g])); body.querySelectorAll("[data-group]").forEach(button => button.onclick = () => groupForm(groups.get(+button.dataset.group)));
    } else if(tab === "cameras") {
      body.innerHTML = `<div class="row between"><h2>Названия камер</h2><button id="sync">Обновить список камер</button></div><p class="muted">Эти названия будут видны всем пользователям.</p><form class="stack form-limit"><div class="camera-names">${state.cameras.map(c => `<label class="camera-name"><span>Канал ${c.id}</span><input name="camera_${c.id}" value="${h(c.name)}" required maxlength="120" aria-label="Имя камеры ${c.id}"></label>`).join("") || `<p>${h(state.connection_state)}</p>`}</div><div class="error"></div><button type="submit" class="primary">Сохранить названия</button></form>`;
      body.querySelector("#sync").onclick = async () => { try { await api("/api/admin/sync","POST",{}); toast("Обновляем список. Это может занять пару минут."); } catch(e) { toast(e.message); } };
      bindForm(body.querySelector("form"), async data => { await api("/api/admin/cameras","PUT",{cameras:state.cameras.map(c => ({id:c.id,name:data[`camera_${c.id}`]}))}); state.cameras = (await api("/api/state")).cameras; toast("Названия сохранены"); });
    } else if(tab === "connection") {
      const connection = await api("/api/admin/connection"); if(!body.isConnected || tab !== adminTab) return;
      body.innerHTML = `<div class="form-limit"><h2>Подключение к камерам</h2><p><span class="pill">${connection.configured ? "Параметры заданы" : "Не настроено"}</span></p><p class="muted">${h(connection.state)}</p><p>Чтобы изменить подключение, введите все три параметра заново. Сохранённые значения не показываются.</p><form class="stack" autocomplete="off"><label>ID регистратора<input name="ID" required maxlength="128" autocomplete="off" spellcheck="false"></label><label>UserName<input name="UserName" required maxlength="128" autocomplete="off" spellcheck="false"></label><label>Password<input name="Password" type="password" required maxlength="256" autocomplete="new-password"></label><div class="error"></div><button type="submit" class="primary">Заменить параметры подключения</button></form><p class="install-help">Просмотр временно переподключится. Параметры используются только сервером для связи с регистратором.</p></div>`;
      bindForm(body.querySelector("form"), async data => { await api("/api/admin/connection","PUT",data); body.querySelector("form").reset(); state.configured = true; toast("Параметры сохранены. Подключаемся к регистратору."); admin(); });
    }
  } catch(e) { if(body.isConnected) { body.innerHTML = `<p class="error" role="alert">${h(e.message)}</p><button id="retry-admin">Повторить</button>`; body.querySelector("#retry-admin").onclick = () => admin(true); } }
}
function personFields(user = {}) { return `<div class="two"><label>Фамилия<input name="last_name" required maxlength="100" value="${h(user.last_name)}"></label><label>Имя<input name="first_name" required maxlength="100" value="${h(user.first_name)}"></label></div><label>Роль<select name="role"><option value="user" ${user.role !== "admin" ? "selected" : ""}>Пользователь</option><option value="admin" ${user.role === "admin" ? "selected" : ""}>Администратор</option></select></label>`; }
function inviteForm() {
  dialog("Пригласить пользователя",`<form class="stack">${personFields()}${accessFields()}<div class="error"></div><button class="primary" type="submit">Создать ссылку</button></form>`);
  const form = modal.querySelector("form"); bindForm(form,async data => { const invite = await api("/api/admin/invites","POST",{...data,...accessValue(form)}); const link = `${location.origin}/#/invite/${invite.token}`; modal.innerHTML = `<button class="close" aria-label="Закрыть">×</button><h2>Приглашение готово</h2><p>Передайте ссылку ${h(data.first_name)}. Она действует семь дней и сработает один раз.</p><a class="invite-link" href="${h(link)}" target="_blank" rel="noopener noreferrer">${h(link)}</a><p class="muted">Ссылка больше не будет показана. Если потеряете её, создайте новое приглашение.</p><button class="primary" id="copy-link">Скопировать ссылку</button>`; modal.querySelector(".close").onclick = () => { modal.close(); admin(); }; modal.querySelector("#copy-link").onclick = async () => { try { await navigator.clipboard.writeText(link); toast("Ссылка скопирована"); } catch { toast("Выделите и скопируйте ссылку вручную"); } }; });
}
function userForm(user) {
  dialog("Права пользователя",`<p class="muted">Логин: ${h(user.login)}</p><form class="stack">${personFields(user)}<label class="row"><input type="checkbox" name="blocked" ${user.blocked ? "checked" : ""}>Заблокировать пользователя</label>${accessFields(user)}<div class="error"></div><button type="submit" class="primary">Сохранить права</button></form>`);
  const form = modal.querySelector("form"); bindForm(form,async data => { await api(`/api/admin/users/${user.id}`,"PUT",{...data,...accessValue(form),blocked:form.elements.blocked.checked}); modal.close(); toast("Права сохранены"); await refresh(); if(me) admin(); });
}
function groupForm(group = {}) {
  const selected = new Set(group.camera_ids || []);
  dialog(group.id ? "Изменить группу" : "Новая группа",`<form class="stack"><label>Название группы<input name="name" value="${h(group.name)}" maxlength="100" required ${group.builtin ? "readonly" : ""}></label>${group.builtin ? `<p class="muted">Исключите ненужные камеры из основного списка. Доступ через другие группы и отдельные разрешения сохранится.</p>` : ""}<h3>Камеры</h3><div class="checks">${state.cameras.map(c => `<label><input type="checkbox" name="camera_ids" value="${c.id}" ${selected.has(c.id) ? "checked" : ""}>${h(c.name)}</label>`).join("") || "Камер пока нет"}</div><div class="error"></div><div class="actions">${group.id && !group.builtin ? `<button type="button" class="danger" id="delete-group">Удалить группу</button>` : ""}<button type="submit" class="primary">Сохранить группу</button></div></form>`);
  const form = modal.querySelector("form"); bindForm(form,async data => { await api(`/api/admin/groups${group.id ? `/${group.id}` : ""}`,group.id ? "PUT" : "POST",{name:data.name,camera_ids:accessValue(form).camera_ids}); modal.close(); state.groups = (await api("/api/state")).groups; admin(); toast("Группа сохранена"); });
  const remove = modal.querySelector("#delete-group"); if(remove) remove.onclick = async () => { if(!confirm("Удалить группу? Доступ, выданный через неё, прекратится.")) return; try { await api(`/api/admin/groups/${group.id}`,"DELETE",{}); modal.close(); state.groups = (await api("/api/state")).groups; admin(); } catch(e) { toast(e.message); } };
}
window.addEventListener("beforeinstallprompt", event => { event.preventDefault(); installPrompt = event; });
window.addEventListener("hashchange", () => { flushSave().finally(boot); });
window.addEventListener("offline", () => { toast("Нет сети. Эфир возобновится после подключения."); });
window.addEventListener("online", () => { if(me) { refresh(); connectVideo(); } else boot(); });
document.addEventListener("visibilitychange", () => { if(document.hidden) { flushSave(); stopVideo(); } else if(me && page === "wall") { refresh(); connectVideo(); } });
document.addEventListener("fullscreenchange", () => { if(!document.fullscreenElement) document.body.classList.remove("fullscreen"); });
let resizeTimer;
window.addEventListener("resize", () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(() => { if(me && page === "wall") renderTiles(); },150); });
if("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
boot();
