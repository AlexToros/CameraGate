"use strict";
const root = document.querySelector("#app"), modal = document.querySelector("#modal");
const h = value => String(value ?? "").replace(/[&<>"\x27]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","\x27":"&#39;"}[c]));
let me, csrf, state, page = "wall", socket, socketEpoch = 0, saveTimer, saving = Promise.resolve(), refreshTimer, installPrompt, reconnectTimer;
const mediaStreams = new Map();
const cameraRatios = new Map();
let tileObserver, cameraFullscreen = null;
let dragged;
let routeEpoch = 0;
let activeLocation = location.hash;
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
function dialog(title, body, kind="sheet") { modal.className=`dialog-${kind}`;modal.setAttribute("aria-label",title); modal.innerHTML = `<header class="dialog-head"><h2>${h(title)}</h2><button class="close icon-button" aria-label="Закрыть">×</button></header><div class="dialog-content">${body}</div>`; modal.querySelector(".close").onclick = () => modal.close(); modal.showModal(); }
modal.addEventListener("click", event => { if (event.target === modal) { const rect = modal.getBoundingClientRect(); if(event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) modal.close(); } });
function stopVideo() { clearTimeout(reconnectTimer);socketEpoch++;if(socket)socket.close();socket=null;if(cameraFullscreen){cameraFullscreen.classList.remove("video-expanded");if(document.fullscreenElement===cameraFullscreen)document.exitFullscreen().catch(()=>{});cameraFullscreen=null;}for(const stream of mediaStreams.values())stream.destroy();mediaStreams.clear(); }
function signedOut(message) { adminDraft=null;finishAdminPicker();root.classList.remove("editing");routeEpoch++; tileObserver?.disconnect(); me = null; csrf = null; state = null; clearInterval(refreshTimer); clearTimeout(saveTimer); stopVideo(); if(modal.open) modal.close(); authView("login", null, message); }
function authView(mode, invitation, message = "") {
  const setup = mode === "setup", invite = mode === "invite";
  root.innerHTML = `<main class="auth"><div class="brand-mark" aria-hidden="true">▦</div><h1>${setup ? "Первый администратор" : invite ? "Регистрация" : "Домовые камеры"}</h1><p class="muted">${setup ? "" : invite ? `Здравствуйте, ${h(invitation.last_name)} ${h(invitation.first_name)}!<br>Выберите логин для входа и пароль.` : ""}</p><form class="stack" id="auth-form">${setup ? `<div class="two"><label>Фамилия<input name="last_name" autocomplete="family-name" required maxlength="100"></label><label>Имя<input name="first_name" autocomplete="given-name" required maxlength="100"></label></div>` : ""}<label>Логин<input name="login" autocomplete="username" autocapitalize="none" spellcheck="false" pattern="[A-Za-z0-9._\\-]{3,64}" minlength="3" maxlength="64" required></label><label>Пароль<input name="password" type="password" autocomplete="${mode === "login" ? "current-password" : "new-password"}" ${mode === "login" ? "" : "minlength=6"} maxlength="128" required></label>${mode !== "login" ? `<label>Повторите пароль<input name="password_confirmation" type="password" autocomplete="new-password" minlength="6" maxlength="128" required></label><small>От 6 символов.</small>` : ""}<div class="error" role="alert">${h(message)}</div><button class="primary" type="submit">${setup ? "Создать администратора" : invite ? "Зарегистрироваться" : "Войти"}</button></form><div class="install-help">${mode === "login" ? "Вход по приглашению. " : ""}${showInstallButton() ? `<button id="install" type="button">Установить</button>` : ""}</div></main>`;
  const epoch = routeEpoch;
  bindForm(document.querySelector("#auth-form"), async data => {
    if(mode !== "login" && data.password !== data.password_confirmation) throw new Error("Пароли не совпадают");
    await api(`/api/auth/${setup ? "setup" : invite ? "register" : "login"}`, "POST", {...data, ...(invite ? {token:invitation.token} : {})});
    if(epoch !== routeEpoch) return;
    history.replaceState(null, "", "/"); activeLocation=location.hash;await enter(epoch);
  });
  const installButton = document.querySelector("#install");
  if (installButton) installButton.onclick = install;
}
async function install() {
  if (installPrompt) { await installPrompt.prompt(); installPrompt = null; }
  else { const ios=/iPhone|iPad|iPod/i.test(navigator.userAgent) || (navigator.platform==="MacIntel" && navigator.maxTouchPoints>1);toast(ios ? "«Поделиться» → «На экран Домой»" : "Меню браузера → «Установить приложение»"); }
}
async function boot() {
  activeLocation=location.hash;
  const epoch = ++routeEpoch;
  tileObserver?.disconnect();
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
async function enter(epoch = routeEpoch) { const result = await api("/api/me"), next = await api("/api/state"); if(epoch !== routeEpoch) return; me = result.user; csrf = result.csrf; state = next; page = me.role === "admin" && location.hash.startsWith("#/admin") ? "admin" : "wall"; render(); clearInterval(refreshTimer); refreshTimer = setInterval(refresh, 15000); }
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
  root.classList.remove("editing");
  if(!me || !state) return;
  stopVideo();
  root.innerHTML = `<header class="header"><div class="brand"><span class="symbol" aria-hidden="true">▦</span><div><strong>Домовые камеры</strong><div class="identity">${h(me.first_name)} ${h(me.last_name)}</div></div></div><div class="row"><nav class="tabs" aria-label="Разделы"><button data-page="wall" class="${page === "wall" ? "active" : ""}">Прямой эфир</button>${me.role === "admin" ? `<button data-page="admin" class="${page === "admin" ? "active" : ""}">Управление</button>` : ""}</nav><button id="logout">Выйти</button></div></header><main id="content"></main>`;
  document.querySelectorAll("[data-page]").forEach(button => {
    if(button.dataset.page === page) button.setAttribute("aria-current","page");
    button.onclick = () => navigateTo(button.dataset.page === "admin" ? "#/admin" : "");
  });
  document.querySelector("#logout").onclick = async () => { if(!mayLeaveAdmin())return; try { await flushSave(); await api("/api/auth/logout","POST",{}); signedOut(); } catch(e) { toast(e.message); } };
  if(page === "wall") wall(); else admin();
}
async function navigateTo(hash) {
  if(!mayLeaveAdmin())return;
  await flushSave();
  finishAdminPicker();adminDraft=null;
  if(modal.open)modal.close();
  history.pushState(null,"",hash || "/");activeLocation=location.hash;
  if(!me || location.hash.startsWith("#/invite/")){await boot();return;}
  page=me.role==="admin" && location.hash.startsWith("#/admin") ? "admin" : "wall";
  render();window.scrollTo(0,0);
}
async function locationChanged() {
  if(adminPicker && location.hash===activeLocation){finishAdminPicker();return;}
  if(location.hash===activeLocation)return;
  if(!mayLeaveAdmin()){history.pushState(null,"",activeLocation || "/");return;}
  activeLocation=location.hash;adminDraft=null;finishAdminPicker();
  await flushSave();
  if(!me || location.hash.startsWith("#/invite/")){await boot();return;}
  if(modal.open)modal.close();
  page=me.role==="admin" && location.hash.startsWith("#/admin") ? "admin" : "wall";
  render();window.scrollTo(0,0);
}
const icons = {
  back: '<path d="m14 6-6 6 6 6"/>',
  chevron: '<path d="m9 6 6 6-6 6"/>',
  refresh: '<path d="M20 7v5h-5M4 17v-5h5"/><path d="M6 7a7 7 0 0 1 12-1l2 3M4 15l2 3a7 7 0 0 0 12-1"/>',

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
  content.innerHTML = `<div class="toolbar"><div class="row"><button id="preset">${icon("grid")}<span>Сетка</span></button><button id="add-tile">${icon("plus")}<span>Камера</span></button><button id="start-playback" hidden>Начать просмотр</button></div></div>${!state.configured ? `<div class="banner">Подключение к камерам не настроено. ${me.role === "admin" ? "Введите параметры в разделе «Управление → Подключение»." : "Обратитесь к администратору."}</div>` : !state.cameras.length ? `<div class="banner">${me.role === "admin" ? h(state.connection_state) : "Доступных камер пока нет. Обратитесь к администратору."}</div>` : ""}<div class="wall" id="wall" aria-label="Камеры"></div>`;
  document.querySelector("#add-tile").onclick = () => {
    if(state.layout.tiles.length >= 64) return toast("Можно добавить до 64 экранов");
    const tile = {id:uid(),camera_id:null,width:1,height:1};
    chooseCamera(tile, () => state.layout.tiles.push(tile));
  };
  document.querySelector("#preset").onclick = gridOptions;
  document.querySelector("#start-playback").onclick=resumeCameraPlayback;
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
  dialog("Сетка",`<div class="presets">${[1,4,6,9,16].map(n => `<button data-preset="${n}" aria-label="${screenCount(n)}"><span class="preset-icon preset-${n}" aria-hidden="true">${Array.from({length:n},() => "<i></i>").join("")}</span><span>${n}</span></button>`).join("")}</div><details><summary>Настроить</summary><form class="stack"><label>Колонок<select name="columns">${Array.from({length:12},(_,i) => `<option ${state.layout.columns === i+1 ? "selected" : ""}>${i+1}</option>`).join("")}</select></label><div class="error"></div><button type="submit" class="primary">Сохранить</button></form></details>`);
  modal.querySelectorAll("[data-preset]").forEach(button => button.onclick = () => applyPreset(+button.dataset.preset));
  bindForm(modal.querySelector("form"),data => { state.layout.columns = +data.columns; modal.close(); renderTiles(); changed(); });
}
function renderTiles() {
  const wall = document.querySelector("#wall"); if(!wall) return;
  tileObserver?.disconnect();
  for(const stream of mediaStreams.values())stream.releasePlayers();
  const columns = Math.min(state.layout.columns,Math.max(1,Math.floor((wall.clientWidth-16)/150)));
  wall.style.setProperty("--columns",columns);
  const cameras = new Map(state.cameras.map(c => [c.id,c]));
  wall.innerHTML = state.layout.tiles.length ? state.layout.tiles.map((tile,index) => {
    const name = cameras.get(tile.camera_id)?.name || `Экран ${index+1}`;
    return `<article class="tile" data-tile="${h(tile.id)}" aria-label="Экран ${index+1}"><div class="tile-head" draggable="true"><span class="status-dot" aria-hidden="true"></span><button class="camera-trigger" aria-label="Камера экрана ${index+1}: ${h(name)}" title="${h(name)}">${h(name)}${icon("down")}</button><button class="tile-menu icon-button" title="Настройки экрана" aria-label="Настройки экрана ${index+1}">${icon("more")}</button></div><div class="picture"><video muted autoplay playsinline webkit-playsinline disableRemotePlayback preload="auto"></video><canvas class="last-frame" hidden aria-hidden="true"></canvas><span class="stream-state">${tile.camera_id ? "Подключение…" : ""}</span>${!tile.camera_id ? `<button class="empty-camera">${icon("plus")}<span>Камера</span></button>` : ""}<button ${tile.camera_id ? "" : "hidden"} class="tile-expand icon-button" title="Увеличить" aria-label="Увеличить экран ${index+1}">${icon("expand")}</button></div></article>`;
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
    const empty=element.querySelector(".empty-camera");if(empty)empty.onclick=()=>chooseCamera(tile);
    element.querySelector(".tile-expand").onclick = () => tile.camera_id ? expandCamera(element) : chooseCamera(tile);
    element.querySelector(".picture").ondblclick = event => { if(tile.camera_id && !event.target.closest("button")) expandCamera(element); };
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
function chooseCamera(tile, onSelect) {
  dialog(onSelect ? "Добавить камеру" : "Камера",`<div class="camera-filters"><input type="search" id="camera-search" placeholder="Найти камеру" aria-label="Найти камеру" autocomplete="off"><select id="camera-group" aria-label="Группа камер"><option value="0">Все доступные</option>${state.groups.map(g => `<option value="${g.id}" ${me.role === "admin" && g.builtin ? "selected" : ""}>${h(g.name)}</option>`).join("")}</select></div><div class="camera-choices" id="camera-choices"></div>`,"picker");
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
    list.innerHTML = cameras.map(c => `<button data-camera="${c.id}" class="camera-choice ${c.id === tile.camera_id ? "selected" : ""}" aria-pressed="${c.id === tile.camera_id}"${onWall.has(c.id) ? ' aria-label="'+h(c.name)+' — на экране"' : ''}><span>${h(c.name)}</span>${onWall.has(c.id) ? '<span class="camera-on-wall" title="На экране" aria-hidden="true"></span>' : ''}</button>`).join("") || `<p class="empty">Камеры не найдены</p>`;
    list.querySelectorAll("[data-camera]").forEach(button => button.onclick = () => { tile.camera_id = +button.dataset.camera; onSelect?.(); modal.close(); renderTiles(); changed(); subscribe(); });
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
async function expandCamera(element) {
  const picture=element.querySelector(".picture"),video=picture.querySelector("video");
  if(document.fullscreenElement===picture) { await document.exitFullscreen();return; }
  if(picture.classList.contains("video-expanded")) { picture.classList.remove("video-expanded");cameraFullscreen=null;return; }
  cameraFullscreen=picture;
  try {
    if(picture.requestFullscreen) await picture.requestFullscreen({navigationUI:"hide"});
    else if(video.webkitEnterFullscreen) {
      video.addEventListener("webkitendfullscreen",()=>{cameraFullscreen=null;},{once:true});video.webkitEnterFullscreen();
    } else picture.classList.add("video-expanded");
  } catch { picture.classList.add("video-expanded"); }
}
function changed() { clearTimeout(saveTimer); saveTimer = setTimeout(save,500); }
function save() {
  clearTimeout(saveTimer); saveTimer = null; if(!me || !state) return saving;
  const value = structuredClone(state.layout);
  saving = saving.catch(() => {}).then(() => api("/api/layout","PUT",value)).catch(e => toast(e.message));
  return saving;
}
async function flushSave() { if(saveTimer) { clearTimeout(saveTimer); saveTimer = null; return save(); } return saving; }
function clearTile(element) { const saved=freezeCameraFrame(element);if(element.cameraStream)element.cameraStream.detach(element);element.cameraStream=null;const video=element.querySelector("video");if(video){video.pause();video.removeAttribute("src");video.load();video.hidden=true;}element.querySelector(".stream-state").textContent=saved ? "Восстанавливаем эфир" : "Подключение…"; }
function drawCached(tile, element) {
  const stream=mediaStreams.get(tile.camera_id);if(!stream)return;
  if(element.cameraStream && element.cameraStream!==stream)clearTile(element);
  element.cameraStream=stream;stream.attach(element);
}
function subscriptions() { return [...new Set(state.layout.tiles.map(t => t.camera_id).filter(Boolean))]; }
function subscribe() {
  const active = new Set(subscriptions());
  for(const [cid,stream] of mediaStreams) if(!active.has(cid)) { stream.destroy(); mediaStreams.delete(cid); }
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
      if(message.type === "revoked") { const denied = new Set(message.camera_ids); for(const tile of state.layout.tiles) if(denied.has(tile.camera_id)) tile.camera_id = null; renderTiles(); subscribe(); refresh(); toast("Права доступа к камерам изменились"); }
      if(message.type === "status") {
        const tiles = new Map(state.layout.tiles.map(t => [t.id,t]));
        document.querySelectorAll(".tile").forEach(element => { const cid = tiles.get(element.dataset.tile)?.camera_id, status = message.cameras[cid]; if(status && status !== "Прямой эфир") { clearTile(element); element.querySelector(".stream-state").textContent = status; } });
      }
      return;
    }
    const view = new DataView(event.data), active = new Set(subscriptions()), tiles = new Map(state.layout.tiles.map(t => [t.id,t])), incoming = new Map();
    let offset = 0;
    try {
      while(offset+9<=view.byteLength) {
        const cid=view.getUint32(offset),kind=view.getUint8(offset+4),length=view.getUint32(offset+5);offset+=9;
        if(length>2*1024*1024+65536 || offset+length>view.byteLength)throw new Error("Неверный пакет видео");
        if(active.has(cid)) {
          const data=new Uint8Array(event.data.slice(offset,offset+length));
          if(kind===1) {
            const size=new DataView(data.buffer).getUint16(0);if(size>256 || size+2>=data.length)throw new Error("Неверный формат видео");
            const mime=new TextDecoder().decode(data.subarray(2,2+size));mediaStreams.get(cid)?.destroy();
            mediaStreams.set(cid,new CameraStream(mime,data.slice(2+size),ratio=>cameraRatios.set(cid,ratio)));
          } else if(kind===2 || kind===3)mediaStreams.get(cid)?.receive(kind,data);
          incoming.set(cid,true);
        }
        offset+=length;
      }
    } catch { ws.close();return; }
    document.querySelectorAll(".tile").forEach(element => { const tile = tiles.get(element.dataset.tile); if(incoming.has(tile?.camera_id)) drawCached(tile,element); });
  };
  ws.onclose = event => { if(epoch !== socketEpoch) return; document.querySelectorAll(".tile").forEach(clearTile); if(event.code === 4401) signedOut("Сессия завершена. Войдите снова"); else if(event.code === 4403) { toast("Права доступа изменились"); refresh().then(() => { reconnectTimer = setTimeout(connectVideo,1500); }); } else reconnectTimer = setTimeout(connectVideo,2000); };
}
window.addEventListener("beforeinstallprompt", event => { event.preventDefault(); installPrompt = event; });
window.addEventListener("hashchange", locationChanged);
window.addEventListener("popstate", locationChanged);
window.addEventListener("beforeunload", event => { if(draftChanged()){event.preventDefault();event.returnValue="";} });
window.addEventListener("offline", () => { toast("Нет сети. Эфир возобновится после подключения."); });
window.addEventListener("online", () => { if(me) { refresh(); connectVideo(); } else boot(); });
window.addEventListener("camera-playback-policy",event=>{const button=document.querySelector("#start-playback");if(button)button.hidden=!event.detail.blocked;});
document.addEventListener("visibilitychange", () => { if(document.hidden) { flushSave(); stopVideo(); } else if(me && page === "wall") { refresh(); connectVideo(); } });
document.addEventListener("fullscreenchange", () => { if(cameraFullscreen && document.fullscreenElement!==cameraFullscreen && !cameraFullscreen.classList.contains("video-expanded"))cameraFullscreen=null; });
document.addEventListener("keydown",event=>{if(event.key==="Escape" && cameraFullscreen?.classList.contains("video-expanded")){cameraFullscreen.classList.remove("video-expanded");cameraFullscreen=null;}});
let resizeTimer;
window.addEventListener("resize", () => { clearTimeout(resizeTimer);resizeTimer=setTimeout(()=>{const wall=document.querySelector("#wall");if(!me || page!=="wall" || cameraFullscreen?.isConnected || !wall)return;const columns=Math.min(state.layout.columns,Math.max(1,Math.floor((wall.clientWidth-16)/150)));if(columns!==+wall.style.getPropertyValue("--columns"))renderTiles();},150); });
function installPullToRefresh() {
  if(!navigator.standalone && !matchMedia("(display-mode: standalone)").matches) return;
  document.documentElement.classList.add("installed-app");
  let start=null,distance=0,refreshing=false;
  const reset=()=>{start=null;distance=0;};
  document.addEventListener("touchstart",event=>{
    reset();
    if(refreshing || event.touches.length!==1 || document.scrollingElement.scrollTop>0 || modal.open || cameraFullscreen || document.fullscreenElement || event.target.closest("button,input,textarea,select,a")) return;
    // Leave nested scroll areas to their own gestures, even at their top edge.
    for(let node=event.target;node && node!==document.body;node=node.parentElement) {
      if(node.scrollHeight>node.clientHeight && /auto|scroll/.test(getComputedStyle(node).overflowY)) return;
    }
    start={x:event.touches[0].clientX,y:event.touches[0].clientY};
  },{passive:true});
  document.addEventListener("touchmove",event=>{
    if(!start)return;
    if(event.touches.length!==1 || document.scrollingElement.scrollTop>0){reset();return;}
    const dy=event.touches[0].clientY-start.y,dx=Math.abs(event.touches[0].clientX-start.x);
    if(dy<0 || dx>Math.max(12,dy)){reset();return;}
    distance=dy;
    if(dy<12)return;
    if(!event.cancelable){reset();return;}
    event.preventDefault();
  },{passive:false});
  document.addEventListener("touchend",()=>{
    const ready=start && distance>=100;
    reset();
    if(!ready)return;
    refreshing=true;flushSave().finally(()=>location.reload());
  },{passive:true});
  document.addEventListener("touchcancel",reset,{passive:true});
}
installPullToRefresh();
if("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
boot();
