const $ = id => document.getElementById(id);
let current = -1;
let names = [];
let sourceNames = [];
let busy = false;
let live = false;
let latestState = null;
let streamGeneration = null;
let imageReady = false;
let streamRequest = 0;
let intent = 0;
let minimumGeneration = -1;
let statusRequest = 0;
let appliedStatus = 0;

function showState() {
  if (!latestState) return;
  live = latestState.live && imageReady && !busy;
  $('status').textContent = busy ? 'Переключение камеры…' : latestState.state;
  $('waiting').textContent = busy ? 'Переключение камеры…' :
    latestState.live && !imageReady ? 'Ожидание изображения…' : latestState.state;
  $('waiting').hidden = live;
  $('lamp').classList.toggle('live', live);
  $('connection').textContent = live ? 'Прямой эфир' : 'Соединение';
}

function cancelStream() {
  streamRequest++;
  imageReady = false;
  streamGeneration = null;
  const image = $('video');
  image.hidden = true;
  image.removeAttribute('src');
}

function openStream(generation) {
  cancelStream();
  streamGeneration = generation;
  const request = streamRequest;
  const image = new Image();
  image.id = 'video';
  image.alt = 'Прямой эфир выбранной камеры';
  image.hidden = true;
  image.addEventListener('load', () => {
    if (request !== streamRequest || image !== $('video') || busy) return;
    imageReady = true;
    image.hidden = false;
    showState();
  });
  image.addEventListener('error', () => {
    if (request !== streamRequest || busy) return;
    imageReady = false;
    showState();
    setTimeout(() => {
      if (request === streamRequest && !busy) openStream(generation);
    }, 1500);
  });
  $('video').replaceWith(image);
  image.src = `/video?generation=${generation}&request=${request}`;
}

function cameraName(original, channel) {
  const name = String(original).trim();
  const yard = /^dvor\/(\d+)p(?:\/(\d+))?$/i.exec(name);
  if (yard) return `Двор у подъезда ${yard[1]}${yard[2] ? ` · камера ${yard[2]}` : ''}`;
  const passage = /^prohod\/(\d+)p$/i.exec(name);
  if (passage) return `Проход у подъезда ${passage[1]}`;
  const entrance = /^(\d+)pod\/cam(\d+)$/i.exec(name);
  if (entrance) return `Подъезд ${entrance[1]} · камера ${entrance[2]}`;
  if (!name || /^(?:CAM|D)\d+$/i.test(name)) return `Камера ${channel + 1}`;
  return name;
}

function renderChannels() {
  const query = $('search').value.toLocaleLowerCase('ru');
  const fragment = document.createDocumentFragment();
  names.forEach((name, channel) => {
    if (!`${channel + 1} ${name} ${sourceNames[channel]}`.toLocaleLowerCase('ru').includes(query)) return;
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'channel';
    button.setAttribute('aria-current', String(channel === current));
    button.dataset.channel = channel;
    const number = document.createElement('span');
    number.className = 'number';
    number.textContent = String(channel + 1).padStart(2, '0');
    const title = document.createElement('span');
    title.className = 'name';
    title.textContent = name;
    button.append(number, title);
    fragment.append(button);
  });
  $('empty').hidden = fragment.childElementCount > 0;
  $('channels').replaceChildren(fragment);
}

async function refresh() {
  const requestedIntent = intent;
  const request = ++statusRequest;
  try {
    const response = await fetch('/api/status', {cache: 'no-store'});
    if (!response.ok) throw new Error();
    const state = await response.json();
    if (requestedIntent !== intent || busy || request < appliedStatus) return;
    if (latestState && latestState.server_id !== state.server_id) {
      minimumGeneration = -1;
      cancelStream();
    }
    if (state.generation < minimumGeneration) return;
    appliedStatus = request;
    latestState = state;
    if (streamGeneration !== state.generation) openStream(state.generation);
    const changed = current !== state.selected || JSON.stringify(sourceNames) !== JSON.stringify(state.channels);
    current = state.selected;
    sourceNames = state.channels;
    names = sourceNames.map(cameraName);
    if (changed) renderChannels();
    $('count').textContent = names.length || '—';
    $('title').textContent = names[current] || 'Камера';
    showState();
  } catch {
    if (requestedIntent !== intent || busy) return;
    live = false;
    $('connection').textContent = 'Сервер недоступен';
    $('lamp').classList.remove('live');
    $('waiting').hidden = false;
    $('waiting').textContent = 'Нет связи с локальным сервером. Проверьте, что он запущен.';
  }
}

$('search').addEventListener('input', renderChannels);
$('channels').addEventListener('click', async event => {
  const button = event.target.closest('[data-channel]');
  if (!button || busy) return;
  const channel = Number(button.dataset.channel);
  if (channel === current) return;
  busy = true;
  intent++;
  cancelStream();
  showState();
  try {
    const response = await fetch('/api/channel', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({channel})});
    if (!response.ok) throw new Error();
    const result = await response.json();
    minimumGeneration = Math.max(minimumGeneration, result.generation);
  } catch { $('status').textContent = 'Не удалось переключить камеру. Повторите попытку.'; }
  finally { busy = false; await refresh(); }
});
$('fullscreen').addEventListener('click', () => $('picture').requestFullscreen().catch(() => {}));
async function poll() { await refresh(); setTimeout(poll, live ? 1000 : 200); }
poll();
