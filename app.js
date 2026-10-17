"use strict";
const root = document.querySelector("#app"), modal = document.querySelector("#modal");
const h = value => String(value ?? "").replace(/[&<>"\x27]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","\x27":"&#39;"}[c]));
let me, csrf, state, page = "wall", adminTab = "users", socket, socketEpoch = 0, saveTimer, saving = Promise.resolve(), draftVersion = 0, refreshTimer, installPrompt, reconnectTimer;
const images = new Map();
let dragged;
const roleName = role => role === "admin" ? "Администратор" : "Пользователь";
const uid = () => crypto.randomUUID().replaceAll("-", "");
function toast(message) { const box = document.querySelector("#notice"); box.textContent = message; box.classList.add("visible"); clearTimeout(box.timer); box.timer = setTimeout(() => box.classList.remove("visible"), 4500); }
async function api(path, method = "GET", data) {
  const response = await fetch(path, {method, credentials:"same-origin", cache:"no-store", headers:{"Content-Type":"application/json", ...(csrf ? {"X-CSRF-Token":csrf} : {})}, ...(data === undefined ? {} : {body:JSON.stringify(data)})});
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
function dialog(title, body) { modal.innerHTML = `<button class="close" aria-label="Закрыть">×</button><h2>${h(title)}</h2>${body}`; modal.querySelector(".close").onclick = () => modal.close(); modal.showModal(); }
modal.addEventListener("click", event => { if (event.target === modal) { const rect = modal.getBoundingClientRect(); if(event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) modal.close(); } });
function stopVideo() { clearTimeout(reconnectTimer); socketEpoch++; if(socket) socket.close(); socket = null; for(const image of images.values()) URL.revokeObjectURL(image.url); images.clear(); }
function signedOut(message) { me = null; csrf = null; state = null; clearInterval(refreshTimer); clearTimeout(saveTimer); stopVideo(); if(modal.open) modal.close(); authView("login", null, message); }
function authView(mode, invitation, message = "") {
  const setup = mode === "setup", invite = mode === "invite";
  root.innerHTML = `<main class="auth"><div class="brand-mark" aria-hidden="true">▦</div><h1>${setup ? "Первый администратор" : invite ? "Вас пригласили" : "Домовые камеры"}</h1><p class="muted">${setup ? "Создайте учётную запись, чтобы настроить доступ к камерам." : invite ? `${h(invitation.last_name)} ${h(invitation.first_name)}<br>${roleName(invitation.role)}. Осталось выбрать логин и пароль.` : "Войдите, чтобы открыть свою видеостену."}</p><form class="stack" id="auth-form">${setup ? `<div class="two"><label>Фамилия<input name="last_name" autocomplete="family-name" required maxlength="100"></label><label>Имя<input name="first_name" autocomplete="given-name" required maxlength="100"></label></div>` : ""}<label>Логин<input name="login" autocomplete="username" autocapitalize="none" spellcheck="false" pattern="[A-Za-z0-9._-]{3,64}" minlength="3" maxlength="64" required></label><label>Пароль<input name="password" type="password" autocomplete="${mode === "login" ? "current-password" : "new-password"}" ${mode === "login" ? "" : "minlength=12"} maxlength="128" required></label>${mode !== "login" ? "<small>От 12 символов. Сохраните пароль в менеджере паролей.</small>" : ""}<div class="error" role="alert">${h(message)}</div><button class="primary" type="submit">${setup ? "Создать администратора" : invite ? "Создать учётную запись" : "Войти"}</button></form><div class="install-help">Доступ выдаёт администратор по приглашению. <button id="install" type="button">Установить приложение</button></div></main>`;
  bindForm(document.querySelector("#auth-form"), async data => {
    await api(`/api/auth/${setup ? "setup" : invite ? "register" : "login"}`, "POST", {...data, ...(invite ? {token:invitation.token} : {})});
    history.replaceState(null, "", "/"); await enter();
  });
  document.querySelector("#install").onclick = install;
}
async function install() {
  if (installPrompt) { await installPrompt.prompt(); installPrompt = null; }
  else toast("iPhone: «Поделиться» → «На экран Домой». Android: меню браузера → «Установить приложение».");
}
async function boot() {
  try {
    const token = location.hash.startsWith("#/invite/") ? location.hash.slice(9) : null;
    if(token) { const preview = await api("/api/invite/preview", "POST", {token}); authView("invite", {...preview,token}); return; }
    const bootstrap = await api("/api/bootstrap");
    if(bootstrap.setup_required) authView("setup");
    else { try { await enter(); } catch(e) { if(e.status === 401) authView("login"); else throw e; } }
  } catch(e) { root.innerHTML = `<main class="auth"><div class="brand-mark">▦</div><h1>${navigator.onLine ? "Не удалось открыть приложение" : "Нет подключения к сети"}</h1><p>${h(navigator.onLine ? e.message : "Для входа и просмотра камер нужен интернет. Ваши настройки сохранены на сервере.")}</p><button id="retry" class="primary">Повторить</button><p class="install-help"><a href="/">Перейти ко входу</a></p></main>`; document.querySelector("#retry").onclick = boot; }
}
async function enter() { const result = await api("/api/me"); me = result.user; csrf = result.csrf; state = await api("/api/state"); page = "wall"; render(); clearInterval(refreshTimer); refreshTimer = setInterval(refresh, 15000); }
async function refresh() {
  if(!me || document.hidden) return;
  try {
    const current = await api("/api/me"); me = current.user; csrf = current.csrf;
    const next = await api("/api/state");
    const changed = JSON.stringify(next.cameras) !== JSON.stringify(state.cameras) || next.configured !== state.configured || (page === "admin" && me.role !== "admin");
    state.groups = next.groups; state.cameras = next.cameras; state.configured = next.configured; state.connection_state = next.connection_state;
    const allowed = new Set(state.cameras.map(c => c.id));
    for(const tile of state.layout.tiles) if(tile.camera_id && !allowed.has(tile.camera_id)) tile.camera_id = null;
    if(changed) { if(me.role !== "admin") page = "wall"; render(); }
  } catch(e) { if(e.status !== 401) toast(navigator.onLine ? e.message : "Нет подключения к сети"); }
}
function render() {
  if(!me || !state) return;
  stopVideo();
  root.innerHTML = `<header class="header"><div class="brand"><span class="symbol" aria-hidden="true">▦</span><div><strong>Домовые камеры</strong><div class="identity">${h(me.first_name)} ${h(me.last_name)}</div></div></div><div class="row"><nav class="tabs" aria-label="Разделы"><button data-page="wall" class="${page === "wall" ? "active" : ""}">Прямой эфир</button>${me.role === "admin" ? `<button data-page="admin" class="${page === "admin" ? "active" : ""}">Управление</button>` : ""}</nav><button id="logout">Выйти</button></div></header><main id="content"></main>`;
  document.querySelectorAll("[data-page]").forEach(button => button.onclick = async () => { await flushSave(); page = button.dataset.page; render(); });
  document.querySelector("#logout").onclick = async () => { try { await flushSave(); await api("/api/auth/logout","POST",{}); signedOut(); } catch(e) { toast(e.message); } };
  if(page === "wall") wall(); else admin();
}
function wall() {
  const content = document.querySelector("#content");
  content.innerHTML = `<div class="toolbar"><div><h1>Моя видеостена</h1><span class="save-status" id="save-state">Настройки сохраняются автоматически</span></div><div class="row"><label>Колонки<select id="columns" aria-label="Количество колонок">${Array.from({length:12},(_,i) => `<option ${state.layout.columns === i+1 ? "selected" : ""}>${i+1}</option>`).join("")}</select></label><button id="preset">Сетка</button><button id="add-tile" class="primary">+ Экран</button><button id="fullscreen" class="screen-only">Во весь экран</button></div></div>${!state.configured ? `<div class="banner">Подключение к камерам пока не настроено. ${me.role === "admin" ? "Откройте «Управление → Подключение» и введите ID, UserName и Password." : "Обратитесь к администратору."}</div>` : !state.cameras.length ? `<div class="banner">Вам пока не назначены камеры. ${me.role === "admin" ? h(state.connection_state) : "Обратитесь к администратору."}</div>` : ""}<div class="wall" id="wall"></div><button class="exit-fullscreen">Вернуться</button>`;
  document.querySelector("#columns").onchange = e => { state.layout.columns = +e.target.value; renderTiles(); changed(); };
  document.querySelector("#add-tile").onclick = () => { if(state.layout.tiles.length>=64) return toast("Можно добавить до 64 экранов"); state.layout.tiles.push({id:uid(),camera_id:null,width:1,height:1}); renderTiles(); changed(); };
  document.querySelector("#preset").onclick = () => {
    dialog("Быстрая сетка",`<p class="muted">Заменить текущую сетку. Камеры расставятся по порядку.</p><div class="row">${[1,4,9,16].map(n => `<button data-preset="${n}">${n} ${n === 1 ? "экран" : "экранов"}</button>`).join("")}</div>`);
    modal.querySelectorAll("[data-preset]").forEach(button => button.onclick = () => { const count = +button.dataset.preset; state.layout = {columns:Math.sqrt(count),tiles:Array.from({length:count},(_,i) => ({id:uid(),camera_id:state.cameras[i]?.id ?? null,width:1,height:1}))}; modal.close(); wall(); changed(); });
  };
  document.querySelector("#fullscreen").onclick = async () => { document.body.classList.add("fullscreen"); try { await document.documentElement.requestFullscreen(); } catch {} };
  document.querySelector(".exit-fullscreen").onclick = () => { document.body.classList.remove("fullscreen"); if(document.fullscreenElement) document.exitFullscreen(); };
  renderTiles(); connectVideo();
}
function renderTiles() {
  const wall = document.querySelector("#wall"); if(!wall) return;
  wall.style.setProperty("--columns",state.layout.columns);
  wall.innerHTML = state.layout.tiles.length ? state.layout.tiles.map((tile,index) => `<article class="tile" data-tile="${h(tile.id)}"><div class="tile-head"><span class="grip" draggable="true" title="Перетащить экран">⠿</span><select aria-label="Камера экрана ${index+1}"><option value="">Выбрать камеру</option>${state.cameras.map(c => `<option value="${c.id}" ${tile.camera_id === c.id ? "selected" : ""}>${h(c.name)}</option>`).join("")}</select><button data-size title="Размер и положение экрана" aria-label="Размер и положение экрана ${index+1}">⤢</button><button data-remove title="Удалить экран" aria-label="Удалить экран ${index+1}">×</button></div><div class="picture"><img alt="" hidden><span class="stream-state">${tile.camera_id ? "Подключение…" : "Выберите камеру для этого экрана"}</span><span class="live-label">Прямой эфир</span></div></article>`).join("") : `<div class="empty">Добавьте экран или выберите быструю сетку.</div>`;
  const tilesById = new Map(state.layout.tiles.map(t => [t.id,t]));
  wall.querySelectorAll(".tile").forEach(element => {
    const tile = tilesById.get(element.dataset.tile);
    element.style.setProperty("--width",Math.min(tile.width,state.layout.columns)); element.style.setProperty("--height",tile.height);
    element.querySelector("select").onchange = event => { tile.camera_id = +event.target.value || null; clearTile(element); changed(); subscribe(); drawCached(tile,element); };
    element.querySelector("[data-remove]").onclick = () => { state.layout.tiles = state.layout.tiles.filter(t => t.id !== tile.id); renderTiles(); changed(); subscribe(); };
    element.querySelector("[data-size]").onclick = () => tileOptions(tile);
    element.querySelector(".grip").ondragstart = event => { dragged = tile.id; event.dataTransfer.setData("text/plain",tile.id); element.classList.add("dragging"); };
    element.ondragend = () => { dragged = null; wall.querySelectorAll(".tile").forEach(t => t.classList.remove("dragging","drop")); };
    element.ondragover = event => { if(dragged && dragged !== tile.id) { event.preventDefault(); element.classList.add("drop"); } };
    element.ondragleave = () => element.classList.remove("drop");
    element.ondrop = event => { event.preventDefault(); const from = state.layout.tiles.findIndex(t => t.id === dragged), to = state.layout.tiles.findIndex(t => t.id === tile.id); if(from>=0 && to>=0) { state.layout.tiles.splice(to,0,state.layout.tiles.splice(from,1)[0]); renderTiles(); changed(); } };
    drawCached(tile,element);
  });
}
function tileOptions(tile) {
  const position = state.layout.tiles.indexOf(tile)+1;
  dialog("Размер и положение",`<form class="stack"><div class="two"><label>Ширина, колонок<select name="width">${Array.from({length:12},(_,i) => `<option ${tile.width === i+1 ? "selected" : ""}>${i+1}</option>`).join("")}</select></label><label>Высота, строк<select name="height">${Array.from({length:6},(_,i) => `<option ${tile.height === i+1 ? "selected" : ""}>${i+1}</option>`).join("")}</select></label></div><label>Позиция на стене<select name="position">${state.layout.tiles.map((_,i) => `<option ${position === i+1 ? "selected" : ""}>${i+1}</option>`).join("")}</select></label><p class="muted">На узком экране ширина ограничивается числом колонок.</p><div class="error"></div><button type="submit" class="primary">Применить</button></form>`);
  bindForm(modal.querySelector("form"), data => { tile.width = +data.width; tile.height = +data.height; const index = state.layout.tiles.indexOf(tile); state.layout.tiles.splice(index,1); state.layout.tiles.splice(+data.position-1,0,tile); modal.close(); renderTiles(); changed(); });
}
function changed() { draftVersion++; const label = document.querySelector("#save-state"); if(label) label.textContent = "Сохраняем…"; clearTimeout(saveTimer); saveTimer = setTimeout(save,500); }
function save() {
  clearTimeout(saveTimer); if(!me || !state) return saving;
  const value = structuredClone(state.layout), version = draftVersion;
  saving = saving.catch(() => {}).then(async () => { await api("/api/layout","PUT",value); if(version === draftVersion) { const label = document.querySelector("#save-state"); if(label) label.textContent = "Сетка сохранена"; } }).catch(e => { const label = document.querySelector("#save-state"); if(label) label.textContent = "Не сохранено. Проверьте соединение"; toast(e.message); });
  return saving;
}
async function flushSave() { if(saveTimer) { clearTimeout(saveTimer); saveTimer = null; return save(); } return saving; }
function clearTile(element) { element.classList.remove("live"); const image = element.querySelector("img"); image.onload = null; image.hidden = true; image.removeAttribute("src"); element.querySelector(".stream-state").textContent = "Подключение…"; }
function drawCached(tile, element) {
  const cached = images.get(tile.camera_id);
  if(!cached || Date.now()-cached.at>5000) return;
  const image = element.querySelector("img"), expectedCamera = tile.camera_id, expectedURL = cached.url;
  image.onload = () => { if(tile.camera_id === expectedCamera && images.get(expectedCamera)?.url === expectedURL && element.isConnected) { image.hidden = false; element.classList.add("live"); } };
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
      if(message.type === "revoked") { const denied = new Set(message.camera_ids); for(const tile of state.layout.tiles) if(denied.has(tile.camera_id)) tile.camera_id = null; renderTiles(); subscribe(); refresh(); toast("Права доступа к камерам изменились"); }
      if(message.type === "status") {
        const tiles = new Map(state.layout.tiles.map(t => [t.id,t]));
        document.querySelectorAll(".tile").forEach(element => { const cid = tiles.get(element.dataset.tile)?.camera_id, status = message.cameras[cid]; if(status && status !== "Прямой эфир") { clearTile(element); element.querySelector(".stream-state").textContent = status; } });
      }
      return;
    }
    const view = new DataView(event.data), active = new Set(subscriptions()), tiles = new Map(state.layout.tiles.map(t => [t.id,t])), incoming = new Map();
    let offset = 0;
    while(offset+8<=view.byteLength) { const cid = view.getUint32(offset), length = view.getUint32(offset+4); offset += 8; if(length>8*1024*1024 || offset+length>view.byteLength) break; if(active.has(cid)) { const old = images.get(cid); const value = {url:URL.createObjectURL(new Blob([event.data.slice(offset,offset+length)],{type:"image/jpeg"})),at:Date.now()}; images.set(cid,value); incoming.set(cid,value); if(old) setTimeout(() => URL.revokeObjectURL(old.url),1500); } offset += length; }
    document.querySelectorAll(".tile").forEach(element => { const tile = tiles.get(element.dataset.tile); if(incoming.has(tile?.camera_id)) drawCached(tile,element); });
  };
  ws.onclose = event => { if(epoch !== socketEpoch) return; document.querySelectorAll(".tile").forEach(clearTile); if(event.code === 4401) signedOut("Сессия завершена. Войдите снова"); else if(event.code === 4403) { toast("Права доступа изменились"); refresh().then(() => { reconnectTimer = setTimeout(connectVideo,1500); }); } else reconnectTimer = setTimeout(connectVideo,2000); };
}
function accessFields(value = {}) {
  return `<div class="access"><p class="access-note">Администраторы всегда видят все камеры. Для пользователя доступ складывается из выбранных групп и отдельных камер.</p><h3>Группы камер</h3><div class="checks">${state.groups.map(g => `<label><input type="checkbox" name="group_ids" value="${g.id}" ${(value.group_ids || []).includes(g.id) ? "checked" : ""}>${h(g.name)}</label>`).join("") || "Групп пока нет"}</div><h3>Отдельные камеры</h3><div class="checks">${state.cameras.map(c => `<label><input type="checkbox" name="camera_ids" value="${c.id}" ${(value.camera_ids || []).includes(c.id) ? "checked" : ""}>${h(c.name)}</label>`).join("") || "Камер пока нет"}</div></div>`;
}
function accessValue(form) { return {camera_ids:[...form.querySelectorAll("[name=camera_ids]:checked")].map(e => +e.value), group_ids:[...form.querySelectorAll("[name=group_ids]:checked")].map(e => +e.value)}; }
async function admin() {
  const content = document.querySelector("#content");
  content.innerHTML = `<section class="admin"><nav class="admin-nav" aria-label="Управление">${Object.entries({users:"Пользователи",invites:"Приглашения",groups:"Группы",cameras:"Камеры",connection:"Подключение"}).map(([key,label]) => `<button data-tab="${key}" class="${key === adminTab ? "active" : ""}">${label}</button>`).join("")}</nav><div id="admin-body" class="surface"><p class="muted">Загрузка…</p></div></section>`;
  content.querySelectorAll("[data-tab]").forEach(button => button.onclick = async () => { adminTab = button.dataset.tab; state = {...state,...await api("/api/state"),layout:state.layout}; admin(); });
  const body = document.querySelector("#admin-body"), tab = adminTab;
  try {
    if(tab === "users") {
      const data = await api("/api/admin/users"); if(!body.isConnected || tab !== adminTab) return;
      body.innerHTML = `<div class="row between"><h2>Пользователи</h2><button id="new-invite" class="primary">Пригласить</button></div><div class="table-wrap"><table><thead><tr><th>Имя и логин</th><th>Роль</th><th>Доступ</th><th></th></tr></thead><tbody>${data.users.map(u => `<tr><td>${h(u.last_name)} ${h(u.first_name)}<span class="muted">${h(u.login)}</span></td><td><span class="pill ${u.blocked ? "blocked" : ""}">${u.blocked ? "Заблокирован" : roleName(u.role)}</span></td><td>${u.role === "admin" ? "Все камеры" : `${u.group_ids.length} групп, ${u.camera_ids.length} камер`}</td><td><button data-user="${u.id}">Изменить</button></td></tr>`).join("")}</tbody></table></div>`;
      body.querySelector("#new-invite").onclick = inviteForm; const users = new Map(data.users.map(u => [u.id,u])); body.querySelectorAll("[data-user]").forEach(button => button.onclick = () => userForm(users.get(+button.dataset.user)));
    } else if(tab === "invites") {
      const data = await api("/api/admin/invites"); if(!body.isConnected || tab !== adminTab) return;
      body.innerHTML = `<div class="row between"><h2>Приглашения</h2><button id="new-invite" class="primary">Создать приглашение</button></div><p class="muted">Ссылка действует семь дней и позволяет создать только одну учётную запись. Скопировать её можно сразу после создания.</p>${data.invitations.length ? `<div class="table-wrap"><table><thead><tr><th>Получатель</th><th>Роль</th><th>Статус</th><th></th></tr></thead><tbody>${data.invitations.map(i => `<tr><td>${h(i.last_name)} ${h(i.first_name)}</td><td>${roleName(i.role)}</td><td>${i.used_by ? "Использовано" : i.revoked ? "Отозвано" : new Date(i.expires_at)<new Date() ? "Истекло" : "Ожидает регистрации"}</td><td>${!i.used_by && !i.revoked ? `<button data-revoke="${i.id}" class="danger">Отозвать</button>` : ""}</td></tr>`).join("")}</tbody></table></div>` : `<p class="empty">Приглашений пока нет</p>`}`;
      body.querySelector("#new-invite").onclick = inviteForm; body.querySelectorAll("[data-revoke]").forEach(button => button.onclick = async () => { try { await api(`/api/admin/invites/${button.dataset.revoke}`,"DELETE",{}); admin(); } catch(e) { toast(e.message); } });
    } else if(tab === "groups") {
      body.innerHTML = `<div class="row between"><h2>Группы камер</h2><button id="new-group" class="primary">Создать группу</button></div><p class="muted">Выдавайте доступ к группе — новые камеры в ней станут доступны автоматически.</p><div class="table-wrap"><table><thead><tr><th>Название</th><th>Камер</th><th></th></tr></thead><tbody>${state.groups.map(g => `<tr><td>${h(g.name)}${g.builtin ? `<span class="muted">Системная группа, включает все камеры</span>` : ""}</td><td>${g.builtin ? state.cameras.length : g.camera_ids.length}</td><td>${!g.builtin ? `<button data-group="${g.id}">Изменить</button>` : ""}</td></tr>`).join("")}</tbody></table></div>`;
      body.querySelector("#new-group").onclick = () => groupForm(); const groups = new Map(state.groups.map(g => [g.id,g])); body.querySelectorAll("[data-group]").forEach(button => button.onclick = () => groupForm(groups.get(+button.dataset.group)));
    } else if(tab === "cameras") {
      body.innerHTML = `<div class="row between"><h2>Названия камер</h2><button id="sync">Обновить список камер</button></div><p class="muted">Эти названия будут видны всем пользователям.</p><form class="stack form-limit"><div class="camera-names">${state.cameras.map(c => `<label class="camera-name"><span>Канал ${c.id}</span><input name="camera_${c.id}" value="${h(c.name)}" required maxlength="120" aria-label="Имя камеры ${c.id}"></label>`).join("") || `<p>${h(state.connection_state)}</p>`}</div><div class="error"></div><button type="submit" class="primary">Сохранить названия</button></form>`;
      body.querySelector("#sync").onclick = async () => { try { await api("/api/admin/sync","POST",{}); toast("Обновляем список. Это может занять до 45 секунд."); } catch(e) { toast(e.message); } };
      bindForm(body.querySelector("form"), async data => { await api("/api/admin/cameras","PUT",{cameras:state.cameras.map(c => ({id:c.id,name:data[`camera_${c.id}`]}))}); state.cameras = (await api("/api/state")).cameras; toast("Названия сохранены"); });
    } else if(tab === "connection") {
      const connection = await api("/api/admin/connection"); if(!body.isConnected || tab !== adminTab) return;
      body.innerHTML = `<div class="form-limit"><h2>Подключение к камерам</h2><p><span class="pill">${connection.configured ? "Параметры заданы" : "Не настроено"}</span></p><p class="muted">${h(connection.state)}</p><p>Чтобы изменить подключение, введите все три параметра заново. Сохранённые значения не показываются.</p><form class="stack" autocomplete="off"><label>ID регистратора<input name="ID" required maxlength="128" autocomplete="off" spellcheck="false"></label><label>UserName<input name="UserName" required maxlength="128" autocomplete="off" spellcheck="false"></label><label>Password<input name="Password" type="password" required maxlength="256" autocomplete="new-password"></label><div class="error"></div><button type="submit" class="primary">Заменить параметры подключения</button></form><p class="install-help">Просмотр временно переподключится. Параметры используются только сервером для связи с регистратором.</p></div>`;
      bindForm(body.querySelector("form"), async data => { await api("/api/admin/connection","PUT",data); body.querySelector("form").reset(); state.configured = true; toast("Параметры сохранены. Подключаемся к регистратору."); admin(); });
    }
  } catch(e) { if(body.isConnected) body.innerHTML = `<p class="error">${h(e.message)}</p>`; }
}
function personFields(user = {}) { return `<div class="two"><label>Фамилия<input name="last_name" required maxlength="100" value="${h(user.last_name)}"></label><label>Имя<input name="first_name" required maxlength="100" value="${h(user.first_name)}"></label></div><label>Роль<select name="role"><option value="user" ${user.role !== "admin" ? "selected" : ""}>Пользователь</option><option value="admin" ${user.role === "admin" ? "selected" : ""}>Администратор</option></select></label>`; }
function inviteForm() {
  dialog("Пригласить пользователя",`<form class="stack">${personFields()}${accessFields()}<div class="error"></div><button class="primary" type="submit">Создать ссылку</button></form>`);
  const form = modal.querySelector("form"); bindForm(form,async data => { const invite = await api("/api/admin/invites","POST",{...data,...accessValue(form)}); const link = `${location.origin}/#/invite/${invite.token}`; modal.innerHTML = `<button class="close" aria-label="Закрыть">×</button><h2>Приглашение готово</h2><p>Передайте ссылку ${h(data.first_name)}. Она действует семь дней и сработает один раз.</p><p class="invite-link">${h(link)}</p><p class="muted">Ссылка больше не будет показана. Если потеряете её, создайте новое приглашение.</p><button class="primary" id="copy-link">Скопировать ссылку</button>`; modal.querySelector(".close").onclick = () => { modal.close(); admin(); }; modal.querySelector("#copy-link").onclick = async () => { try { await navigator.clipboard.writeText(link); toast("Ссылка скопирована"); } catch { toast("Выделите и скопируйте ссылку вручную"); } }; });
}
function userForm(user) {
  dialog("Права пользователя",`<p class="muted">Логин: ${h(user.login)}</p><form class="stack">${personFields(user)}<label class="row"><input type="checkbox" name="blocked" ${user.blocked ? "checked" : ""}>Заблокировать пользователя</label>${accessFields(user)}<div class="error"></div><button type="submit" class="primary">Сохранить права</button></form>`);
  const form = modal.querySelector("form"); bindForm(form,async data => { await api(`/api/admin/users/${user.id}`,"PUT",{...data,...accessValue(form),blocked:form.elements.blocked.checked}); modal.close(); toast("Права сохранены"); await refresh(); if(me) admin(); });
}
function groupForm(group = {}) {
  dialog(group.id ? "Изменить группу" : "Новая группа",`<form class="stack"><label>Название группы<input name="name" value="${h(group.name)}" maxlength="100" required></label><h3>Камеры</h3><div class="checks">${state.cameras.map(c => `<label><input type="checkbox" name="camera_ids" value="${c.id}" ${(group.camera_ids || []).includes(c.id) ? "checked" : ""}>${h(c.name)}</label>`).join("") || "Камер пока нет"}</div><div class="error"></div><div class="actions">${group.id ? `<button type="button" class="danger" id="delete-group">Удалить группу</button>` : ""}<button type="submit" class="primary">Сохранить группу</button></div></form>`);
  const form = modal.querySelector("form"); bindForm(form,async data => { await api(`/api/admin/groups${group.id ? `/${group.id}` : ""}`,group.id ? "PUT" : "POST",{name:data.name,camera_ids:accessValue(form).camera_ids}); modal.close(); state.groups = (await api("/api/state")).groups; admin(); toast("Группа сохранена"); });
  const remove = modal.querySelector("#delete-group"); if(remove) remove.onclick = async () => { if(!confirm("Удалить группу? Доступ, выданный через неё, прекратится.")) return; try { await api(`/api/admin/groups/${group.id}`,"DELETE",{}); modal.close(); state.groups = (await api("/api/state")).groups; admin(); } catch(e) { toast(e.message); } };
}
window.addEventListener("beforeinstallprompt", event => { event.preventDefault(); installPrompt = event; });
window.addEventListener("offline", () => { toast("Нет сети. Эфир возобновится после подключения."); });
window.addEventListener("online", () => { if(me) { refresh(); connectVideo(); } else boot(); });
document.addEventListener("visibilitychange", () => { if(document.hidden) { flushSave(); stopVideo(); } else if(me && page === "wall") { refresh(); connectVideo(); } });
document.addEventListener("fullscreenchange", () => { if(!document.fullscreenElement) document.body.classList.remove("fullscreen"); });
if("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
boot();
