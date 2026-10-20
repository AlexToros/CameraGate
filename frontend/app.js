"use strict";
const root = document.querySelector("#app"), modal = document.querySelector("#modal");
const h = value => String(value ?? "").replace(/[&<>"\x27]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","\x27":"&#39;"}[c]));
let me, csrf, state, page = "wall", socket, socketEpoch = 0, saveTimer, saving = Promise.resolve(), refreshTimer, installPrompt, reconnectTimer;
const mediaStreams = new Map();
const HD_OFFSET=256;
let hdSelection=null;
let movingCameras = new Set();
function showMotion(ids=[]) {
  movingCameras = new Set(ids);
  document.querySelectorAll('.tile-motion').forEach(mark=>{mark.hidden=!movingCameras.has(+mark.dataset.camera);});
}
const cameraAudio = new CameraAudio();
let audioSelection = null;
cameraAudio.onError=message=>{stopCameraAudio();subscribe();toast(message);};
const cameraRatios = new Map();
function cameraRatio(cid) {return (cid && cameraRatios.get(cid+HD_OFFSET)) || 4/3;}
function rememberCameraRatio(sourceId,ratio) {
  if(cameraRatios.get(sourceId)===ratio)return;
  cameraRatios.set(sourceId,ratio);
  if(sourceId<=HD_OFFSET)return;
  const cid=sourceId-HD_OFFSET,tiles=new Map(state.layout.tiles.map(tile=>[tile.id,tile.camera_id]));
  document.querySelectorAll('.tile').forEach(element=>{
    if(tiles.get(element.dataset.tile)===cid)element.style.setProperty('--ratio',ratio);
  });
}
let tileObserver, cameraFullscreen = null;
let stopWallReorder=()=>{};
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
function dialog(title, body, kind="sheet") { modal.className=`dialog-${kind}`;modal.setAttribute("aria-label",title); modal.innerHTML = `<header class="dialog-head"><h2>${h(title)}</h2><button class="close icon-button" aria-label="Закрыть">×</button></header><div class="dialog-content">${body}</div>`; if(kind==="picker")modal.querySelector(".dialog-head").append(modal.querySelector(".camera-filters"));modal.querySelector(".close").onclick = () => modal.close(); modal.showModal(); }
modal.addEventListener("click", event => { if (event.target === modal) { const rect = modal.getBoundingClientRect(); if(event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) modal.close(); } });
function stopVideo() {hdSelection=null;stopArchive();showMotion();stopCameraAudio(true);clearTimeout(reconnectTimer);socketEpoch++;if(socket)socket.close();socket=null;if(cameraFullscreen){cameraFullscreen.classList.remove("video-expanded");if(document.fullscreenElement===cameraFullscreen)document.exitFullscreen().catch(()=>{});cameraFullscreen=null;}for(const stream of mediaStreams.values())stream.destroy();mediaStreams.clear(); }
function signedOut(message) { stopWallReorder(); toggleAdminMenu(false);adminDraft=null;finishAdminPicker();root.classList.remove("editing");routeEpoch++; tileObserver?.disconnect(); me = null; csrf = null; state = null; clearInterval(refreshTimer); clearTimeout(saveTimer); stopVideo(); if(modal.open) modal.close(); authView("login", null, message); }
function authView(mode, invitation, message = "") {
  const setup = mode === "setup", invite = mode === "invite";
  root.innerHTML = `<main class="auth"><div class="brand-mark" aria-hidden="true">▦</div><h1>${setup ? "Первый администратор" : invite ? "Регистрация" : "Домовые камеры"}</h1><p class="muted">${setup ? "" : invite ? `Здравствуйте, ${h(invitation.last_name)} ${h(invitation.first_name)}!<br>Придумайте логин для входа и пароль.` : ""}</p><form class="stack" id="auth-form">${setup ? `<div class="two"><label>Фамилия<input name="last_name" autocomplete="family-name" required maxlength="100"></label><label>Имя<input name="first_name" autocomplete="given-name" required maxlength="100"></label></div>` : ""}<label>Логин<input name="login" autocomplete="username" autocapitalize="none" spellcheck="false" pattern="[A-Za-z0-9._\\-]{3,64}" minlength="3" maxlength="64" required></label><label>Пароль<input name="password" type="password" autocomplete="${mode === "login" ? "current-password" : "new-password"}" ${mode === "login" ? "" : "minlength=6"} maxlength="128" required></label>${mode !== "login" ? `<label>Повторите пароль<input name="password_confirmation" type="password" autocomplete="new-password" minlength="6" maxlength="128" required></label><small>От 6 символов.</small>` : ""}<div class="error" role="alert">${h(message)}</div><button class="primary" type="submit">${setup ? "Создать администратора" : invite ? "Зарегистрироваться" : "Войти"}</button></form><div class="install-help">${mode === "login" ? "Вход по приглашению. " : ""}${showInstallButton() ? `<button id="install" type="button">Установить</button>` : ""}</div></main>`;
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
function passwordResetView(token) {
  root.innerHTML=`<main class="auth"><h1>Новый пароль</h1><form class="stack"><label>Пароль<input name="password" type="password" autocomplete="new-password" minlength="6" maxlength="128" required></label><label>Повторите пароль<input name="confirmation" type="password" autocomplete="new-password" minlength="6" maxlength="128" required></label><div class="error" role="alert"></div><button class="primary" type="submit">Сохранить</button></form></main>`;
  const epoch=routeEpoch;
  bindForm(root.querySelector('form'),async data=>{
    if(data.password!==data.confirmation)throw new Error('Пароли не совпадают');
    await api('/api/auth/reset','POST',{token,password:data.password});
    if(epoch!==routeEpoch)return;
    history.replaceState(null,'','/');activeLocation=location.hash;
    authView('login',null,'Пароль изменён. Войдите с новым паролем.');
  });
}
async function install() {
  if (installPrompt) { await installPrompt.prompt(); installPrompt = null; }
  else { const ios=/iPhone|iPad|iPod/i.test(navigator.userAgent) || (navigator.platform==="MacIntel" && navigator.maxTouchPoints>1);toast(ios ? "«Поделиться» → «На экран Домой»" : "Меню браузера → «Установить приложение»"); }
}
async function boot() {
  stopWallReorder();
  toggleAdminMenu(false);
  activeLocation=location.hash;
  const epoch = ++routeEpoch;
  tileObserver?.disconnect();
  stopVideo(); clearInterval(refreshTimer); clearTimeout(saveTimer);
  me = null; csrf = null; state = null;
  if(modal.open) modal.close();
  root.innerHTML = `<main class="auth"><div class="brand-mark" aria-hidden="true">▦</div><h1>Домовые камеры</h1><p role="status">Открываем приложение…</p></main>`;
  try {
    if(location.hash.startsWith('#/reset/')){passwordResetView(location.hash.slice(8));return;}
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
    if(changed) {
      if(me.role!=="admin"){page="wall";adminDraft=null;finishAdminPicker();render();}
      else if(!adminDraft && !adminPicker)render();
    }
  } catch(e) { if(e.status !== 401) toast(navigator.onLine ? e.message : "Нет подключения к сети"); }
}
function render() {
  stopWallReorder();
  tileObserver?.disconnect();
  root.classList.remove("editing");
  document.body.classList.remove("admin-menu-open");
  if(!me || !state) return;
  stopVideo();
  root.innerHTML = `<header class="header"><div class="brand"><span class="symbol" aria-hidden="true">▦</span><div><strong>Домовые камеры</strong><div class="identity">${h(me.first_name)} ${h(me.last_name)}</div></div></div><div class="row"><nav class="tabs" aria-label="Разделы"><button data-page="wall" class="${page === "wall" ? "active" : ""}">Прямой эфир</button>${me.role === "admin" ? `<button data-page="admin" class="${page === "admin" ? "active" : ""}">Управление</button>` : ""}</nav><button id="logout">Выйти</button></div></header><main id="content"></main>`;
  document.querySelectorAll("[data-page]").forEach(button => {
    if(button.dataset.page === page) button.setAttribute("aria-current","page");
    button.onclick = () => navigateTo(button.dataset.page === "admin" ? "#/admin/users" : "");
  });
  document.querySelector("#logout").onclick = async () => { if(!mayLeaveAdmin())return; try { await flushSave(); await api("/api/auth/logout","POST",{}); signedOut(); } catch(e) { toast(e.message); } };
  if(page === "wall") wall(); else admin();
}
async function navigateTo(hash) {
  if((hash || "")===location.hash && !adminPicker)return;
  if(!mayLeaveAdmin())return;
  await flushSave();
  finishAdminPicker();adminDraft=null;
  if(modal.open)modal.close();
  history.pushState(null,"",hash || "/");activeLocation=location.hash;
  if(!me || (location.hash.startsWith("#/invite/") || location.hash.startsWith("#/reset/"))){await boot();return;}
  page=me.role==="admin" && location.hash.startsWith("#/admin") ? "admin" : "wall";
  render();window.scrollTo(0,0);
}
async function locationChanged() {
  if(adminPicker && location.hash===activeLocation){finishAdminPicker();return;}
  if(location.hash===activeLocation)return;
  if(!mayLeaveAdmin()){history.pushState(null,"",activeLocation || "/");return;}
  activeLocation=location.hash;adminDraft=null;finishAdminPicker();
  await flushSave();
  if(!me || (location.hash.startsWith("#/invite/") || location.hash.startsWith("#/reset/"))){await boot();return;}
  if(modal.open)modal.close();
  page=me.role==="admin" && location.hash.startsWith("#/admin") ? "admin" : "wall";
  render();window.scrollTo(0,0);
}
const icons = {
  grip: '<path d="M9 5h.01M15 5h.01M9 12h.01M15 12h.01M9 19h.01M15 19h.01" stroke-width="3"/>',
  motion: '<circle cx="14" cy="4" r="2"/><path d="m7 10 4-3 4 2 3 4m-7-6-2 7 5 3 1 5m-6-8-4 7m-1-8h4"/>',
  sound: '<path d="M11 5 6 9H3v6h3l5 4zM15 8a6 6 0 0 1 0 8m3-11a10 10 0 0 1 0 14"/>',
  silent: '<path d="M11 5 6 9H3v6h3l5 4zM16 9l5 6m0-6-5 6"/>',
  copy: '<rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V4H4v12h4"/>',
  check: '<path d="m5 12 4 4L19 6"/>',
  menu: '<path d="M4 6h16M4 12h16M4 18h16"/>',
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
  stopWallReorder();
  if(hdSelection)finishCameraFullscreen();
  const audible=audioSelection && (state.layout.tiles.find(tile=>tile.id===audioSelection.tileId && tile.camera_id===audioSelection.cid) || state.layout.tiles.find(tile=>tile.camera_id===audioSelection.cid));
  if(audioSelection && !audible)stopCameraAudio();
  tileObserver?.disconnect();
  for(const stream of mediaStreams.values())stream.releasePlayers();
  const columns = Math.min(state.layout.columns,Math.max(1,Math.floor((wall.clientWidth-16)/150)));
  wall.style.setProperty("--columns",columns);
  const cameras = new Map(state.cameras.map(c => [c.id,c]));
  wall.innerHTML = state.layout.tiles.length ? state.layout.tiles.map((tile,index) => {
    const name = cameras.get(tile.camera_id)?.name || `Экран ${index+1}`;
    return `<article class="tile" data-tile="${h(tile.id)}" aria-label="Экран ${index+1}"><div class="tile-head"><button type="button" class="tile-drag icon-button" title="Перетащить камеру" aria-label="Переместить камеру: ${h(name)}">${icon("grip")}</button><span class="status-dot" aria-hidden="true"></span><button class="camera-trigger" aria-label="Камера экрана ${index+1}: ${h(name)}" title="${h(name)}">${h(name)}${icon("down")}</button><span class="tile-motion" data-camera="${tile.camera_id || 0}" ${movingCameras.has(tile.camera_id) ? "" : "hidden"} role="img" aria-label="Движение" title="Движение">${icon("motion")}</span><button class="tile-menu icon-button" title="Настройки экрана" aria-label="Настройки экрана ${index+1}">${icon("more")}</button></div><div class="picture"><video muted autoplay playsinline webkit-playsinline disableRemotePlayback preload="auto"></video><canvas class="last-frame" hidden aria-hidden="true"></canvas><span class="stream-state">${tile.camera_id ? "Подключение…" : ""}</span>${!tile.camera_id ? `<button class="empty-camera">${icon("plus")}<span>Камера</span></button>` : ""}<button ${tile.camera_id ? "" : "hidden"} class="tile-audio icon-button" title="Включить звук" aria-label="Включить звук" aria-pressed="false">${icon("silent")}</button><button ${tile.camera_id ? "" : "hidden"} class="tile-expand icon-button" title="На весь экран" aria-label="На весь экран: камера ${index+1}">${icon("expand")}</button></div></article>`;
  }).join("") : `<div class="empty"><p>Добавьте экран или выберите сетку.</p><button class="primary" id="empty-add">Добавить экран</button></div>`;
  if(!state.layout.tiles.length) { document.querySelector("#empty-add").onclick = () => document.querySelector("#add-tile").click(); return; }
  const tilesById = new Map(state.layout.tiles.map(t => [t.id,t]));
  tileObserver = new ResizeObserver(entries => {
    // ResizeObserver provides the measurements; no forced read/write layout loop.
    const gap = parseFloat(getComputedStyle(wall).rowGap) || 4;
    const spans = entries.map(entry => ({element:entry.target,rows:Math.ceil(((entry.borderBoxSize?.[0]?.blockSize ?? entry.contentRect.height)+gap)/(2+gap))}));
    spans.forEach(({element,rows}) => element.style.gridRowEnd = `span ${rows}`);
  });
  let audioChanged=false;
  wall.querySelectorAll(".tile").forEach(element => {
    const tile = tilesById.get(element.dataset.tile);
    if(audible?.id===tile.id){Object.assign(audioSelection,{tileId:tile.id,element});cameraAudio.bindVideo(element.querySelector('video'));}
    element.style.setProperty("--width",Math.min(tile.width,columns));
    element.style.setProperty("--ratio",cameraRatio(tile.camera_id));
    element.querySelector(".camera-trigger").onclick = () => chooseCamera(tile);
    element.querySelector(".tile-menu").onclick = () => tileOptions(tile);
    const empty=element.querySelector(".empty-camera");if(empty)empty.onclick=()=>chooseCamera(tile);
    element.querySelector(".tile-audio").onclick = () => toggleCameraAudio(tile,element);
    element.querySelector(".tile-expand").onclick = () => tile.camera_id ? expandCamera(element) : chooseCamera(tile);
    element.querySelector(".picture").ondblclick = event => { if(tile.camera_id && !event.target.closest("button")) expandCamera(element); };
    tileObserver.observe(element);
    audioChanged=drawCached(tile,element) || audioChanged;
  });
  audioButtons();
  if(audioChanged)subscribe();
  stopWallReorder=enableWallReorder(wall,ids=>{
    state.layout.tiles=ids.map(id=>tilesById.get(id));
    const nodes=new Map([...wall.querySelectorAll('.tile')].map(element=>[element.dataset.tile,element]));
    state.layout.tiles.forEach((tile,index)=>{
      const element=nodes.get(tile.id),name=cameras.get(tile.camera_id)?.name || `Экран ${index+1}`;
      element.setAttribute('aria-label',`Экран ${index+1}`);
      element.querySelector('.camera-trigger').setAttribute('aria-label',`Камера экрана ${index+1}: ${name}`);
      element.querySelector('.tile-menu').setAttribute('aria-label',`Настройки экрана ${index+1}`);
      element.querySelector('.tile-expand').setAttribute('aria-label',`На весь экран: камера ${index+1}`);
    });
    changed();
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
  const widths=new Map([[1,"Обычный"]]);
  if(state.layout.columns>=2)widths.set(2,"Широкий");
  if(state.layout.columns>2)widths.set(state.layout.columns,"Вся строка");
  if(!widths.has(tile.width))widths.set(tile.width,tile.width>=state.layout.columns ? "Вся строка" : `${tile.width} колонки`);
  dialog("Экран",`<form class="stack"><label>Размер<select name="width">${[...widths].map(([value,label])=>`<option value="${value}" ${tile.width===value ? "selected" : ""}>${label}</option>`).join("")}</select></label><div class="stack"><span class="field-label">Порядок</span><input type="hidden" name="position" value="${position}"><span class="order-control"><button type="button" id="move-earlier" aria-label="Переместить раньше">↑</button><span id="tile-position"></span><button type="button" id="move-later" aria-label="Переместить позже">↓</button></span></div><div class="error"></div><div class="tile-options-actions"><button type="button" id="remove-current-tile" class="danger">Удалить</button><button type="submit" class="primary">Готово</button></div></form>`);
  const input=modal.querySelector("[name=position]"),earlier=modal.querySelector("#move-earlier"),later=modal.querySelector("#move-later");
  const update=()=>{modal.querySelector("#tile-position").textContent=`${input.value} из ${state.layout.tiles.length}`;earlier.disabled=+input.value<=1;later.disabled=+input.value>=state.layout.tiles.length;};
  earlier.onclick=()=>{input.value=+input.value-1;update();};later.onclick=()=>{input.value=+input.value+1;update();};update();
  modal.querySelector("#remove-current-tile").onclick = () => { state.layout.tiles = state.layout.tiles.filter(t => t.id !== tile.id); modal.close(); renderTiles(); changed(); subscribe(); };
  bindForm(modal.querySelector("form"), data => { tile.width = +data.width; tile.height = 1; const index = state.layout.tiles.indexOf(tile); state.layout.tiles.splice(index,1); state.layout.tiles.splice(+data.position-1,0,tile); modal.close(); renderTiles(); changed(); });
}
async function expandCamera(element) {
  const picture=element.querySelector(".picture");
  if(document.fullscreenElement===picture) { await document.exitFullscreen();return; }
  if(picture.classList.contains("video-expanded")) { picture.classList.remove("video-expanded");finishCameraFullscreen();return; }
  cameraFullscreen=picture;
  const tile=state.layout.tiles.find(tile=>tile.id===element.dataset.tile);
  hdSelection={cid:tile.camera_id,element};subscribe();
  try {
    if(picture.requestFullscreen) await picture.requestFullscreen({navigationUI:"hide"});
    // iPhone's native video fullscreen owns its media presentation. Keep
    // our picture/controls when replacing MSE sources between SD and Main.
    else picture.classList.add("video-expanded");
  } catch { picture.classList.add("video-expanded"); }
}
function finishCameraFullscreen() {
  const selected=hdSelection,picture=cameraFullscreen;hdSelection=null;cameraFullscreen=null;
  picture?.classList.remove('video-expanded');
  if(picture && document.fullscreenElement===picture)document.exitFullscreen().catch(()=>{});
  if(selected?.element.isConnected){drawCached({camera_id:selected.cid},selected.element);subscribe();}
}
function changed() { clearTimeout(saveTimer); saveTimer = setTimeout(save,500); }
function save() {
  clearTimeout(saveTimer); saveTimer = null; if(!me || !state) return saving;
  const value = structuredClone(state.layout);
  saving = saving.catch(() => {}).then(() => api("/api/layout","PUT",value)).catch(e => toast(e.message));
  return saving;
}
async function flushSave() { if(saveTimer) { clearTimeout(saveTimer); saveTimer = null; return save(); } return saving; }
function audioButtons() {
  document.querySelectorAll(".tile-audio").forEach(button=>{
    const active=audioSelection?.element===button.closest(".tile");
    button.setAttribute("aria-pressed",String(active));button.innerHTML=icon(active ? "sound" : "silent");
    button.title=active ? "Выключить звук" : "Включить звук";button.setAttribute("aria-label",button.title);
  });
}
function stopCameraAudio(close=false) {
  const previous=audioSelection;audioSelection=null;close ? cameraAudio.close() : cameraAudio.stop();audioButtons();
  if(previous && socket?.readyState===WebSocket.OPEN)subscribe();
}
async function toggleCameraAudio(tile,element) {
  if(audioSelection?.element===element){stopCameraAudio();subscribe();return;}
  stopCameraAudio();
  const selection=audioSelection={cid:tile.camera_id,tileId:tile.id,element};audioButtons();
  try {
    await cameraAudio.start(element.querySelector("video"));
    if(audioSelection!==selection)return;
    subscribe();
    setTimeout(()=>{if(audioSelection===selection && !cameraAudio.packets){toast("Звук пока не поступает");}},6000);
  } catch(error){if(audioSelection===selection){stopCameraAudio();subscribe();toast(error.message);}}
}
function reconcileAudioSelection() {
  if(!audioSelection)return;
  const tile=state.layout.tiles.find(tile=>tile.id===audioSelection.tileId);
  if(!tile || tile.camera_id!==audioSelection.cid || !audioSelection.element.isConnected){stopCameraAudio();}
}
function clearTile(element) { if(audioSelection?.element===element)cameraAudio.reset();const saved=freezeCameraFrame(element);if(element.cameraStream)element.cameraStream.detach(element);element.cameraStream=null;element.sourceId=null;const video=element.querySelector("video");if(video){video.pause();video.removeAttribute("src");video.load();video.hidden=true;}element.querySelector(".stream-state").textContent=saved ? "Восстанавливаем эфир" : "Подключение…"; }
function drawCached(tile, element) {
  const high=hdSelection?.element===element ? mediaStreams.get(tile.camera_id+HD_OFFSET) : null;
  const sourceId=high?.fragments.length && Date.now()-high.lastFrame<5000 ? tile.camera_id+HD_OFFSET : tile.camera_id;
  const stream=mediaStreams.get(sourceId);if(!stream)return;
  const changed=element.cameraStream!==stream;
  if(element.cameraStream && element.cameraStream!==stream)clearTile(element);
  element.cameraStream=stream;element.sourceId=sourceId;stream.attach(element);
  if(changed && audioSelection?.element===element){cameraAudio.reset();return true;}
  return false;
}
function subscriptions() { return [...new Set(state.layout.tiles.map(t => t.camera_id).filter(Boolean))]; }
function sourceSubscriptions() {const ids=subscriptions();if(hdSelection)ids.push(hdSelection.cid+HD_OFFSET);return ids;}
function audioSourceId() {return audioSelection ? audioSelection.element.sourceId || audioSelection.cid : null;}
function subscribe() {
  reconcileAudioSelection();
  const camera_ids=subscriptions(),active=new Set(camera_ids);
  if(hdSelection)active.add(hdSelection.cid+HD_OFFSET);
  for(const [cid,stream] of mediaStreams) if(!active.has(cid)) { stream.destroy(); mediaStreams.delete(cid); }
  if(socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify({csrf,camera_ids,hd_camera_id:hdSelection?.cid ?? null,audio_camera_id:audioSelection?.cid ?? null,audio_hd:audioSourceId()>HD_OFFSET}));
}
function connectVideo() {
  stopVideo(); if(!me || page !== "wall" || document.hidden) return;
  const epoch = socketEpoch, ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/api/live`); socket = ws; ws.binaryType = "arraybuffer";
  ws.onopen = subscribe;
  ws.onmessage = event => {
    if(epoch !== socketEpoch || !state) return;
    if(typeof event.data === "string") {
      const message = JSON.parse(event.data);
      if(message.type === "motion") { showMotion(message.available ? message.camera_ids : []);return; }
      if(message.type === "revoked") { const denied = new Set(message.camera_ids); for(const tile of state.layout.tiles) if(denied.has(tile.camera_id)) tile.camera_id = null; renderTiles(); subscribe(); refresh(); toast("Права доступа к камерам изменились"); }
      if(message.type === "status") {
        const tiles = new Map(state.layout.tiles.map(t => [t.id,t]));
        let audioChanged=false;
        document.querySelectorAll(".tile").forEach(element => { const tile=tiles.get(element.dataset.tile),sid=element.sourceId || tile?.camera_id,status=message.cameras[sid]; if(status && status!=="Прямой эфир") { if(sid>HD_OFFSET){mediaStreams.get(sid)?.destroy();audioChanged=drawCached(tile,element) || audioChanged;}else{clearTile(element);element.querySelector(".stream-state").textContent=status;} } });
        if(audioChanged)subscribe();
      }
      return;
    }
    const view = new DataView(event.data), incoming = new Set();
    let active;
    let offset = 0;
    try {
      while(offset+9<=view.byteLength) {
        const cid=view.getUint32(offset),kind=view.getUint8(offset+4),length=view.getUint32(offset+5);offset+=9;
        if(length>2*1024*1024+65536 || offset+length>view.byteLength)throw new Error("Неверный пакет видео");
        // Audio is consumed synchronously: borrow the packet bytes and leave
        // the video wall untouched, including on audio-only socket messages.
        if(kind===4) {
          if(audioSelection && audioSourceId()===cid)cameraAudio.receive(new Uint8Array(event.data,offset,length),mediaStreams.get(cid)?.players.get(audioSelection.element));
        } else if((active ??= new Set(sourceSubscriptions())).has(cid)) {
          const data=new Uint8Array(event.data.slice(offset,offset+length));
          if(kind===1) {
            const size=new DataView(data.buffer).getUint16(0);if(size>256 || size+2>=data.length)throw new Error("Неверный формат видео");
            if(audioSourceId()===cid)cameraAudio.reset();
            const mime=new TextDecoder().decode(data.subarray(2,2+size));mediaStreams.get(cid)?.destroy();
            const Media=window.ManagedMediaSource || window.MediaSource;
            if(cid>HD_OFFSET && !Media?.isTypeSupported(mime)) {
              const selected=hdSelection;hdSelection=null;
              if(selected)drawCached({camera_id:selected.cid},selected.element);
              subscribe();toast("Браузер не поддерживает HD этой камеры");
            } else mediaStreams.set(cid,new CameraStream(mime,data.slice(2+size),ratio=>rememberCameraRatio(cid,ratio),()=>cameraRatio(cid>HD_OFFSET ? cid-HD_OFFSET : cid)));
          } else if(kind===2 || kind===3)mediaStreams.get(cid)?.receive(kind,data);
          if(kind>=1 && kind<=3)incoming.add(cid);
        }
        offset+=length;
      }
    } catch { ws.close();return; }
    if(incoming.size) {
      const tiles = new Map(state.layout.tiles.map(t => [t.id,t]));
      let audioChanged=false;
      document.querySelectorAll(".tile").forEach(element => { const tile = tiles.get(element.dataset.tile); if(incoming.has(tile?.camera_id) || (hdSelection?.element===element && incoming.has(tile.camera_id+HD_OFFSET))) audioChanged=drawCached(tile,element) || audioChanged; });
      if(audioChanged)subscribe();
    }
  };
  ws.onclose = event => { if(epoch !== socketEpoch) return;showMotion();stopCameraAudio(); document.querySelectorAll(".tile").forEach(clearTile); if(event.code === 4401) signedOut("Сессия завершена. Войдите снова"); else if(event.code === 4403) { toast("Права доступа изменились"); refresh().then(() => { reconnectTimer = setTimeout(connectVideo,1500); }); } else reconnectTimer = setTimeout(connectVideo,2000); };
}
window.addEventListener("beforeinstallprompt", event => { event.preventDefault(); installPrompt = event; });
window.addEventListener("hashchange", locationChanged);
window.addEventListener("popstate", locationChanged);
window.addEventListener("beforeunload", event => { if(draftChanged()){event.preventDefault();event.returnValue="";} });
window.addEventListener("offline", () => { toast("Нет сети. Эфир возобновится после подключения."); });
window.addEventListener("online", () => { if(me) { refresh(); connectVideo(); } else boot(); });
window.addEventListener("camera-playback-policy",event=>{const button=document.querySelector("#start-playback");if(button)button.hidden=!event.detail.blocked;});
document.addEventListener("visibilitychange", () => { if(document.hidden) { flushSave(); stopVideo(); } else if(me && page === "wall") { refresh(); connectVideo(); } });
document.addEventListener("fullscreenchange", () => { if(cameraFullscreen && document.fullscreenElement!==cameraFullscreen && !cameraFullscreen.classList.contains("video-expanded"))finishCameraFullscreen(); });
document.addEventListener("keydown",event=>{if(event.key==="Escape" && cameraFullscreen?.classList.contains("video-expanded")){cameraFullscreen.classList.remove("video-expanded");finishCameraFullscreen();}});
let resizeTimer;
window.addEventListener("resize", () => { clearTimeout(resizeTimer);resizeTimer=setTimeout(()=>{const wall=document.querySelector("#wall");if(!me || page!=="wall" || cameraFullscreen?.isConnected || !wall)return;const columns=Math.min(state.layout.columns,Math.max(1,Math.floor((wall.clientWidth-16)/150)));if(columns!==+wall.style.getPropertyValue("--columns"))renderTiles();},150); });
function installPullToRefresh() {
  if(!navigator.standalone && !matchMedia("(display-mode: standalone)").matches) return;
  document.documentElement.classList.add("installed-app");
  let start=null,distance=0,refreshing=false;
  const reset=()=>{start=null;distance=0;};
  document.addEventListener("touchstart",event=>{
    reset();
    if(refreshing || document.body.classList.contains("admin-menu-open") || event.touches.length!==1 || document.scrollingElement.scrollTop>0 || modal.open || cameraFullscreen || document.fullscreenElement || event.target.closest("button,input,textarea,select,a")) return;
    // Leave nested scroll areas to their own gestures, even at their top edge.
    for(let node=event.target;node && node!==document.body;node=node.parentElement) {
      if(node.scrollHeight>node.clientHeight && /auto|scroll/.test(getComputedStyle(node).overflowY)) return;
    }
    start={x:event.touches[0].clientX,y:event.touches[0].clientY};
  },{passive:true});
  document.addEventListener("touchmove",event=>{
    if(!start)return;
    if(document.body.classList.contains("wall-reordering") || event.touches.length!==1 || document.scrollingElement.scrollTop>0){reset();return;}
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
