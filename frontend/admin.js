"use strict";

const adminSections = {users:"Пользователи",invites:"Приглашения",groups:"Группы",cameras:"Камеры",connection:"Подключение"};
let adminDraft = null, adminPicker = null;
const quantity = (count, forms) => `${count} ${forms[count%100>=11 && count%100<=14 ? 2 : count%10===1 ? 0 : count%10>=2 && count%10<=4 ? 1 : 2]}`;
const cameraCount = count => quantity(count,["камера","камеры","камер"]);
const groupCount = count => quantity(count,["группа","группы","групп"]);

function draftChanged() { return adminDraft && JSON.stringify(adminDraft.data)!==adminDraft.baseline; }
function mayLeaveAdmin() {
  if(adminDraft?.saving) { toast("Подождите завершения сохранения");return false; }
  return !draftChanged() || confirm("Изменения не сохранены. Выйти без сохранения?");
}
function adminPath() { return location.hash.slice(2).split("/").filter(Boolean).slice(1); }
function objectRow({id,name,detail,tag="",kind}) {
  return `<button type="button" class="object-row" data-object="${id}" data-kind="${kind}"><span class="object-copy"><span class="object-name">${h(name)}</span>${detail ? `<span class="object-detail">${h(detail)}</span>` : ""}</span>${tag ? `<span class="pill blocked">${h(tag)}</span>` : ""}${icon("chevron")}</button>`;
}
function sectionHead(title, action="", back=false) {
  return `<div class="section-head">${back ? `<button class="admin-back icon-button" aria-label="Назад">${icon("back")}</button>` : `<button class="admin-menu icon-button" aria-label="Разделы управления" aria-expanded="false" aria-controls="admin-navigation">${icon("menu")}</button>`}<h2>${h(title)}</h2>${action}</div>`;
}
function bindAdminHeader(body, back="#/admin/users") {
  const previous=body.querySelector(".admin-back"), menu=body.querySelector(".admin-menu");
  if(previous)previous.onclick=()=>navigateTo(back);
  if(menu){menu.setAttribute("aria-expanded",String(!!document.querySelector(".admin-nav.is-open")));menu.onclick=()=>toggleAdminMenu(true);}
}
function toggleAdminMenu(open) {
  const nav=document.querySelector(".admin-nav"), scrim=document.querySelector(".admin-scrim");
  if(!nav || !scrim)return;
  nav.classList.toggle("is-open",open);scrim.hidden=!open;
  document.body.classList.toggle("admin-menu-open",open);
  const button=document.querySelector(".admin-menu");
  button?.setAttribute("aria-expanded",String(open));
  document.querySelector("#admin-body").inert=open;
  document.querySelector(".header").inert=open;
  if(open)(nav.querySelector('[aria-current="page"]') || nav.querySelector("a")).focus();
  else button?.focus({preventScroll:true});
}
function bindAdminRows(body, section) {
  body.querySelectorAll("[data-object]").forEach(button => button.onclick=()=>navigateTo(`#/admin/${section}/${button.dataset.object}`));
}
async function admin() {
  if(!me || me.role!=="admin" || page!=="admin")return;
  let [section, item]=adminPath();
  const content=document.querySelector("#content");
  if(!Object.hasOwn(adminSections,section)) {
    section="users";item=undefined;
    history.replaceState(null,"","#/admin/users");activeLocation=location.hash;
  }
  if(adminDraft?.route!==location.hash)adminDraft=null;
  root.classList.toggle("editing",!!item);
  content.innerHTML=`<section class="admin"><button class="admin-scrim" aria-label="Закрыть меню" hidden></button><nav id="admin-navigation" class="admin-nav" aria-label="Управление"><div class="admin-menu-head"><span>Управление</span><button class="icon-button admin-menu-close" aria-label="Закрыть меню">×</button></div>${Object.entries(adminSections).map(([key,label])=>`<a href="#/admin/${key}" ${key===section ? 'aria-current="page"' : ""}>${label}</a>`).join("")}</nav><div id="admin-body" class="surface">${sectionHead("Загрузка…","",!!item)}</div></section>`;
  content.querySelectorAll(".admin-nav a").forEach(link=>link.onclick=event=>{event.preventDefault();toggleAdminMenu(false);navigateTo(link.getAttribute("href"));});
  content.querySelector(".admin-scrim").onclick=()=>toggleAdminMenu(false);
  content.querySelector(".admin-menu-close").onclick=()=>toggleAdminMenu(false);
  const body=content.querySelector("#admin-body");
  bindAdminHeader(body,`#/admin/${section}`);
  try {
    if(item) { await adminEditor(body,section,item);return; }
    const create=section==="users" || section==="invites" ? `<button class="primary" id="new-invite" aria-label="Пригласить">${icon("plus")}<span>Пригласить</span></button>` : section==="groups" ? `<button class="icon-button primary" id="new-group" aria-label="Создать группу">${icon("plus")}</button>` : section==="cameras" ? `<button class="icon-button" id="sync" aria-label="Обновить список камер">${icon("refresh")}</button>` : "";
    let rows=[];
    if(section==="users") {
      const data=await api("/api/admin/users");if(!body.isConnected)return;
      rows=data.users.map(user=>objectRow({id:user.id,name:`${user.last_name} ${user.first_name}`,detail:`${user.login} · ${roleName(user.role)}`,tag:user.blocked ? "Заблокирован" : "",kind:"user"}));
    } else if(section==="invites") {
      const data=await api("/api/admin/invites");if(!body.isConnected)return;
      rows=data.invitations.map(invite=>objectRow({id:invite.id,name:`${invite.last_name} ${invite.first_name}`,detail:roleName(invite.role),kind:"invite"}));
    } else if(section==="groups") {
      rows=state.groups.map(group=>objectRow({id:group.id,name:group.name,detail:cameraCount(group.camera_ids.length),kind:"group"}));
    } else if(section==="cameras") {
      rows=state.cameras.map(camera=>objectRow({id:camera.id,name:camera.name,detail:`Канал ${camera.id}`,kind:"camera"}));
    } else if(section==="connection") {
      await adminEditor(body,section,"settings");return;
    }
    body.innerHTML=`${sectionHead(adminSections[section],create)}${section==="cameras" || section==="users" ? `<label class="search-field"><span class="sr-only">${section==="cameras" ? "Найти камеру" : "Найти пользователя"}</span><input type="search" id="object-search" placeholder="Поиск" autocomplete="off"></label>` : ""}<div class="object-list">${rows.join("") || '<p class="empty">Пока пусто</p>'}</div><p class="empty search-empty" hidden>Ничего не найдено</p>`;
    bindAdminHeader(body);
    bindAdminRows(body,section);
    const search=body.querySelector("#object-search");
    if(search) {
      const entries=[...body.querySelectorAll(".object-row")].map(element=>({element,text:element.textContent.toLocaleLowerCase()}));
      search.oninput=()=>{const query=search.value.trim().toLocaleLowerCase();let found=0;for(const entry of entries){entry.element.hidden=!entry.text.includes(query);if(!entry.element.hidden)found++;}body.querySelector(".search-empty").hidden=found>0;};
    }
    const invite=body.querySelector("#new-invite");if(invite)invite.onclick=()=>navigateTo("#/admin/invites/new");
    const group=body.querySelector("#new-group");if(group)group.onclick=()=>navigateTo("#/admin/groups/new");
    const sync=body.querySelector("#sync");if(sync)sync.onclick=async()=>{sync.disabled=true;try{await api("/api/admin/sync","POST",{});toast("Обновление списка запущено");}catch(error){toast(error.message);}finally{sync.disabled=false;}};
  } catch(error) {
    if(body.isConnected){root.classList.remove("editing");body.innerHTML=`${sectionHead("Ошибка","",!!item)}<div class="form-fields"><p class="error" role="alert">${h(error.message)}</p><button id="retry-admin">Повторить</button></div>`;bindAdminHeader(body,`#/admin/${section}`);body.querySelector("#retry-admin").onclick=admin;}
  }
}

function personFields(value) {
  return `<label>Фамилия<input name="last_name" required maxlength="100" autocomplete="family-name" value="${h(value.last_name)}"></label><label>Имя<input name="first_name" required maxlength="100" autocomplete="given-name" value="${h(value.first_name)}"></label><label>Роль<select name="role"><option value="user" ${value.role!=="admin" ? "selected" : ""}>Пользователь</option><option value="admin" ${value.role==="admin" ? "selected" : ""}>Администратор</option></select></label>`;
}
function accessFields(value) {
  return `<div class="user-access" ${value.role==="admin" ? "hidden" : ""}><h3>Доступ</h3><div class="selection-summary"><button type="button" data-access="group_ids"><span>Группы</span><span data-summary="group_ids">${groupCount(value.group_ids.length)}</span>${icon("chevron")}</button><button type="button" data-access="camera_ids"><span>Камеры</span><span data-summary="camera_ids">${cameraCount(value.camera_ids.length)}</span>${icon("chevron")}</button></div></div><p class="admin-access muted" ${value.role!=="admin" ? "hidden" : ""}>Доступ ко всем камерам</p>`;
}
function connectionSummary(connection) {
  const device=connection.device, name=device?.name ? ` «${device.name}»` : "";
  const summary=!connection.configured ? "Регистратор не подключён" : connection.status==="connected" ? `Подключён регистратор${name}` : connection.status==="error" ? "Не удалось подключиться к регистратору" : "Проверяем подключение к регистратору…";
  return `<p>${h(summary)}</p>${connection.status==="connected" && device ? `<p class="muted">${h(quantity(device.channel_count,["канал","канала","каналов"]))}</p>` : ""}`;
}
function followConnection(form) {
  // Update the status without replacing the form or losing credentials being typed.
  setTimeout(async()=>{
    if(!form.isConnected)return;
    try {
      const connection=await api("/api/admin/connection");
      if(!form.isConnected)return;
      form.querySelector(".connection-status").innerHTML=connectionSummary(connection);
      if(connection.status==="checking")followConnection(form);
    } catch(error){if(form.isConnected && error.status!==401)form.querySelector(".connection-status").textContent=error.message;}
  },2000);
}
async function adminEditor(body,section,item) {
  let connectionStatus, value, title, extra="",submit="Сохранить", back=`#/admin/${section}`,save;
  if(section==="users") {
    const data=await api("/api/admin/users");if(!body.isConnected)return;
    value=data.users.find(user=>user.id===+item);if(!value)throw new Error("Пользователь не найден");
    title="Пользователь";
    save=async data=>api(`/api/admin/users/${item}`,"PUT",data);
  } else if(section==="invites" && item==="new") {
    value={first_name:"",last_name:"",role:"user",camera_ids:[],group_ids:[]};title="Приглашение";submit="Создать";
    save=async data=>api("/api/admin/invites","POST",data);
  } else if(section==="invites") {
    const data=await api("/api/admin/invites");if(!body.isConnected)return;
    const invite=data.invitations.find(value=>value.id===+item);if(!invite)throw new Error("Приглашение не найдено");
    body.innerHTML=`${sectionHead("Приглашение","",true)}<div class="form-fields"><h3>${h(invite.last_name)} ${h(invite.first_name)}</h3><p class="muted">${roleName(invite.role)}</p><button class="danger" id="revoke-invite">Отозвать приглашение</button></div>`;
    bindAdminHeader(body,back);
    body.querySelector("#revoke-invite").onclick=async()=>{if(!confirm("Отозвать приглашение?"))return;try{await api(`/api/admin/invites/${item}`,"DELETE",{});await navigateTo(back);}catch(error){toast(error.message);}};return;
  } else if(section==="groups") {
    value=item==="new" ? {name:"",camera_ids:[]} : state.groups.find(group=>group.id===+item);
    if(!value)throw new Error("Группа не найдена");title="Группа";
    extra=value.builtin ? '<p class="field-hint">Основной список для просмотра. Другие разрешения сохранятся.</p>' : item!=="new" ? '<button type="button" class="danger delete-group">Удалить группу</button>' : "";
    save=async data=>api(`/api/admin/groups${item==="new" ? "" : "/"+item}`,item==="new" ? "POST" : "PUT",{name:data.name,camera_ids:data.camera_ids});
  } else if(section==="cameras") {
    const camera=state.cameras.find(camera=>camera.id===+item);if(!camera)throw new Error("Камера не найдена");
    value={name:camera.name};title=`Канал ${camera.id}`;
    save=async data=>api("/api/admin/cameras","PUT",{cameras:[{id:camera.id,name:data.name}]});
  } else if(section==="connection") {
    const connection=await api("/api/admin/connection");if(!body.isConnected)return;
    value={ID:"",UserName:"",Password:""};title="Подключение";back="#/admin";
    connectionStatus=connection.status;
    extra=`<div class="connection-status" role="status">${connectionSummary(connection)}</div><p class="field-hint">${connection.configured ? "Чтобы подключить другой регистратор, введите его Cloud ID, логин и пароль." : "Введите Cloud ID, логин и пароль регистратора."}</p>`;
    save=async data=>api("/api/admin/connection","PUT",data);
  } else throw new Error("Раздел не найден");
  if(!body.isConnected)return;
  root.classList.add("editing");
  if(!adminDraft || adminDraft.route!==location.hash) {
    const data=structuredClone(value);
    if(data.camera_ids)data.camera_ids.sort((a,b)=>a-b);
    if(data.group_ids)data.group_ids.sort((a,b)=>a-b);
    // Keep only the editable profile; credentials and invitation tokens never enter history.
    adminDraft={route:location.hash,data,baseline:JSON.stringify(data),saving:false};
  }
  const draft=adminDraft, data=draft.data;
  const person=section==="users" || section==="invites";
  const fields=person ? `${section==="users" ? `<p class="muted">${h(value.login)}</p>` : ""}${personFields(data)}${section==="users" ? `<label class="switch-row"><span>Заблокирован</span><input type="checkbox" name="blocked" ${data.blocked ? "checked" : ""}></label>` : ""}${accessFields(data)}` : section==="groups" ? `<label>Название<input name="name" required maxlength="100" value="${h(data.name)}" ${value.builtin ? "readonly" : ""}></label>${extra}<div class="selection-summary"><button type="button" data-access="camera_ids"><span>Камеры</span><span data-summary="camera_ids">${cameraCount(data.camera_ids.length)}</span>${icon("chevron")}</button></div>` : section==="cameras" ? `<label>Название<input name="name" required maxlength="120" value="${h(data.name)}"></label>` : `${extra}<label>Cloud ID<input name="ID" required maxlength="128" autocomplete="off" autocapitalize="none" spellcheck="false"></label><label>Логин<input name="UserName" required maxlength="128" autocomplete="off" autocapitalize="none" spellcheck="false"></label><label>Пароль<input name="Password" type="password" required maxlength="256" autocomplete="new-password"></label>`;
  body.innerHTML=`<form id="admin-edit-form" class="admin-editor" autocomplete="off"><header class="editor-head"><button type="button" class="editor-back icon-button" aria-label="Назад">${icon("back")}</button><h2>${h(title)}</h2><button class="primary" type="submit">${submit}</button></header><div class="form-fields stack">${fields}<div class="error" role="alert"></div></div></form>`;
  const form=body.querySelector("form");
  if(connectionStatus==="checking")followConnection(form);
  if(section==="connection")form.querySelectorAll("input").forEach(input=>input.value=data[input.name]);
  form.querySelector(".editor-back").onclick=()=>navigateTo(back);
  const sync=()=>{for(const input of form.querySelectorAll("input,select")){data[input.name]=input.type==="checkbox" ? input.checked : input.value;}if(person){form.querySelector(".user-access").hidden=data.role==="admin";form.querySelector(".admin-access").hidden=data.role!=="admin";}};
  form.addEventListener("input",sync);form.addEventListener("change",sync);
  form.querySelectorAll("[data-access]").forEach(button=>button.onclick=()=>openAdminPicker(form,button.dataset.access));
  const remove=form.querySelector(".delete-group");
  if(remove)remove.onclick=async()=>{if(!confirm("Удалить группу? Доступ через неё прекратится."))return;try{await api(`/api/admin/groups/${item}`,"DELETE",{});draft.baseline=JSON.stringify(data);adminDraft=null;state.groups=(await api("/api/state")).groups;await navigateTo(back);}catch(error){form.querySelector(".error").textContent=error.message;}};
  bindForm(form,async()=>{
    sync();draft.saving=true;
    try {
      const result=await save(data);
      draft.baseline=JSON.stringify(data);draft.saving=false;
      if(section==="invites") { showCreatedInvite(body,result.token,back);return; }
      const next=await api("/api/state");if(!me)return;
      state={...state,...next,layout:state.layout};
      adminDraft=null;await navigateTo(back);
    } finally { draft.saving=false; }
  });
}

function openAdminPicker(form,field) {
  const data=adminDraft.data, entries=field==="group_ids" ? state.groups : state.cameras;
  const selected=new Set(data[field]);
  const picker=document.createElement("section");picker.className="access-picker";
  picker.innerHTML=`<div class="picker-top"><header class="editor-head"><button type="button" class="picker-back icon-button" aria-label="Назад">${icon("back")}</button><h2>${field==="group_ids" ? "Группы" : "Камеры"}</h2><button type="button" class="primary picker-done">Готово</button></header><div class="picker-tools"><input type="search" placeholder="Поиск" aria-label="Поиск" autocomplete="off"><div class="picker-summary"><span class="picker-count" aria-live="polite"></span><button type="button" class="picker-toggle">Выбрать все</button></div></div></div><div class="picker-items"></div>`;
  const ranked=entries.map(entry=>({...entry,search:`${entry.name} ${field==="camera_ids" ? "Канал "+entry.id : ""}`.toLocaleLowerCase()}));
  const initialSelected=new Set(selected);
  const ordered=[...ranked.filter(entry=>initialSelected.has(entry.id)),...ranked.filter(entry=>!initialSelected.has(entry.id))];
  const list=picker.querySelector(".picker-items");
  list.innerHTML=ordered.map(entry=>`<label class="picker-option"><span>${h(entry.name)}</span><input type="checkbox" value="${entry.id}" ${selected.has(entry.id) ? "checked" : ""}></label>`).join("") || '<p class="empty">Пока пусто</p>';
  const rows=[...list.querySelectorAll("label")].map((element,index)=>({element,input:element.querySelector("input"),id:ordered[index].id,search:ordered[index].search}));
  const toggle=picker.querySelector(".picker-toggle");
  const update=()=>{data[field]=[...selected].sort((a,b)=>a-b);picker.querySelector(".picker-count").textContent=`Выбрано: ${selected.size}`;form.querySelector(`[data-summary="${field}"]`).textContent=field==="group_ids" ? groupCount(selected.size) : cameraCount(selected.size);let visible=0,checked=0;for(const row of rows){if(!row.element.hidden){visible++;if(selected.has(row.id))checked++;}}toggle.disabled=!visible;toggle.textContent=visible && checked===visible ? "Снять выбор" : "Выбрать все";};
  toggle.onclick=()=>{const visible=rows.filter(row=>!row.element.hidden),clear=visible.every(row=>selected.has(row.id));for(const row of visible){clear ? selected.delete(row.id) : selected.add(row.id);row.input.checked=!clear;}update();};
  list.addEventListener("change",event=>{const input=event.target;if(input.type!=="checkbox")return;input.checked ? selected.add(+input.value) : selected.delete(+input.value);update();});
  picker.querySelector("[type=search]").oninput=event=>{const query=event.target.value.trim().toLocaleLowerCase();let found=0;for(const row of rows){row.element.hidden=!row.search.includes(query);if(!row.element.hidden)found++;}let empty=list.querySelector(".search-empty");if(!empty){empty=document.createElement("p");empty.className="empty search-empty";empty.textContent="Ничего не найдено";list.append(empty);}empty.hidden=found>0 || !entries.length;update();};
  const scroll=window.scrollY;
  adminPicker={form,picker,scroll};form.hidden=true;form.parentElement.append(picker);
  picker.querySelector(".picker-back").onclick=()=>history.back();picker.querySelector(".picker-done").onclick=()=>history.back();
  history.pushState({picker:true},"",location.href);update();window.scrollTo(0,0);
}
function finishAdminPicker() {
  if(!adminPicker)return;
  const {form,picker,scroll}=adminPicker;adminPicker=null;picker.remove();form.hidden=false;window.scrollTo(0,scroll);
}
function showCreatedInvite(body,token,back) {
  adminDraft=null;
  const link=`${location.origin}/#/invite/${token}`;
  body.innerHTML=`${sectionHead("Приглашение","",true)}<div class="form-fields stack"><p>Ссылка действует 7 дней. Скопируйте её до закрытия.</p><a class="invite-link" href="${h(link)}" target="_blank" rel="noopener noreferrer">${h(link)}</a><button class="primary" id="copy-link">Копировать ссылку</button></div>`;
  bindAdminHeader(body,back);
  body.querySelector("#copy-link").onclick=async()=>{try{await navigator.clipboard.writeText(link);toast("Ссылка скопирована");}catch{toast("Выделите и скопируйте ссылку");}};
}

document.addEventListener("keydown",event=>{
  const nav=document.querySelector(".admin-nav.is-open");
  if(!nav)return;
  if(event.key==="Escape"){event.preventDefault();toggleAdminMenu(false);}
  if(event.key==="Tab") {
    const controls=nav.querySelectorAll("button,a"), first=controls[0], last=controls[controls.length-1];
    if(event.shiftKey && document.activeElement===first){event.preventDefault();last.focus();}
    else if(!event.shiftKey && document.activeElement===last){event.preventDefault();first.focus();}
  }
});
matchMedia("(max-width:900px)").addEventListener("change",event=>{if(!event.matches)toggleAdminMenu(false);});
