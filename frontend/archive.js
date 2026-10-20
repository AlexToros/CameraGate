/* On-demand recorder playback. Live players and their caches stay separate. */
let archiveViewer=null,archiveDownload=null;
function stopArchive() { archiveDownload?.close();archiveDownload=null;archiveViewer?.close();archiveViewer=null; }

class ArchivePlayer {
  constructor(container,record,onStatus) {
    this.container=container;this.record=record;this.onStatus=onStatus;
    this.video=container.querySelector('video');this.slider=container.querySelector('[data-position]');
    container.querySelector('[data-download]').onclick=()=>{if(this.running)this.pause();archiveDownload?.close();archiveDownload=new ArchiveDownload(record,this.position);};
    this.button=container.querySelector('[data-play]');this.clock=container.querySelector('[data-clock]');
    this.events=new AbortController();this.epoch=0;this.position=0;this.offset=0;this.closed=false;this.retryCount=0;
    this.slider.max=String(Math.max(1,record.duration));this.slider.value='0';
    this.button.onclick=()=>this.running ? this.pause() : this.start(this.position);
    this.slider.onchange=()=>this.start(+this.slider.value);
    const options={signal:this.events.signal};
    this.video.addEventListener('timeupdate',()=>this.updatePosition(),options);
    this.video.addEventListener('playing',()=>{if(this.running && !this.retryTimer)this.onStatus('');},options);
    this.video.addEventListener('waiting',()=>{if(this.running && !this.retryTimer)this.onStatus('Загрузка записи…');},options);
    this.video.addEventListener('error',()=>this.retry(),options);
    this.video.addEventListener('ended',()=>{if(this.retryTimer || !this.running)return;if(this.done){this.pause();this.onStatus('Запись закончилась');}else this.retry();},options);
    container.querySelector('[data-fullscreen]').onclick=()=>{
      if(this.video.requestFullscreen)this.video.requestFullscreen().catch(()=>{});
      else if(this.video.webkitEnterFullscreen)this.video.webkitEnterFullscreen();
    };
  }
  updatePosition() {
    if(this.running && !this.retryTimer && this.video.currentTime>=1)this.retryCount=0;
    this.position=Math.min(this.record.duration,this.offset+this.video.currentTime);
    this.slider.value=String(this.position);
    const base=new Date(this.record.start.replace(' ','T'));base.setSeconds(base.getSeconds()+Math.floor(this.position));
    this.clock.textContent=base.toLocaleTimeString('ru-RU');
  }
  release() {
    clearTimeout(this.retryTimer);this.retryTimer=null;
    this.epoch++;this.ws?.close();this.ws=null;
    this.queue=[];this.bytes=0;this.head=0;
  }
  pause() {
    this.updatePosition();this.running=false;this.release();this.video.pause();
    this.button.textContent='Смотреть';this.onStatus('Пауза');
  }
  start(position=0,retrying=false) {
    if(this.closed)return;
    if(!retrying)this.retryCount=0;
    this.release();const epoch=this.epoch;
    this.running=true;this.done=false;this.playPending=false;this.position=this.offset=Math.floor(Math.min(position,Math.max(0,this.record.duration-1)));
    this.media=null;this.buffer=null;
    this.video.pause();this.video.removeAttribute('src');this.video.load();
    if(this.url)URL.revokeObjectURL(this.url);this.url=null;
    this.button.textContent='Пауза';this.onStatus('Открываем запись…');
    const ws=this.ws=new WebSocket(`${location.protocol==='https:' ? 'wss' : 'ws'}://${location.host}/api/admin/archive/play`);
    ws.binaryType='arraybuffer';
    ws.onopen=()=>{if(epoch===this.epoch)ws.send(JSON.stringify({csrf,id:this.record.id,offset:this.offset}));};
    ws.onmessage=event=>{
      if(epoch!==this.epoch || this.closed)return;
      if(typeof event.data==='string') {
        const message=JSON.parse(event.data);
        if(message.type==='error'){if(message.retryable===false)this.fail(message.error);else this.retry();return;}
        if(message.type==='end'){this.done=true;this.pump();return;}
        if(message.type==='init') {
          this.offset=message.offset ?? this.offset;
          const Media=window.ManagedMediaSource || window.MediaSource;
          if(!Media || !Media.isTypeSupported(message.mime)){this.fail('Браузер не поддерживает кодек записи');return;}
          this.media=new Media();this.video.src=this.url=URL.createObjectURL(this.media);
          this.media.addEventListener('sourceopen',()=>{
            if(epoch!==this.epoch)return;
            try {
              this.buffer=this.media.addSourceBuffer(message.mime);
              this.buffer.addEventListener('updateend',()=>{if(epoch===this.epoch)this.pump();});
              this.buffer.addEventListener('error',()=>{if(epoch===this.epoch)this.retry();});
              this.pump();
            } catch {this.retry();}
          },{once:true});
        }
      } else {
        this.bytes+=event.data.byteLength;
        if(this.bytes>8*1024*1024){this.retry();return;}
        this.queue.push(event.data);this.pump();
      }
    };
    ws.onclose=ws.onerror=()=>{if(epoch===this.epoch && this.running && !this.done)this.retry();};
  }
  retry() {
    if(this.closed || !this.running || this.retryTimer)return;
    this.updatePosition();const position=this.position;
    this.release();this.video.pause();
    const epoch=this.epoch,delay=Math.min(15000,1000*2**Math.min(this.retryCount++,4));
    this.onStatus('Восстанавливаем запись…');
    this.retryTimer=setTimeout(()=>{
      this.retryTimer=null;
      if(!this.closed && this.running && epoch===this.epoch)this.start(position,true);
    },delay);
  }
  pump() {
    if(!this.running || this.retryTimer || !this.buffer || this.buffer.updating || this.media.readyState!=='open')return;
    try {
      const ranges=this.buffer.buffered;
      if(ranges.length) {
        if(ranges.end(ranges.length-1)-this.video.currentTime>30){this.retry();return;}
        if(this.video.currentTime-ranges.start(0)>15){this.buffer.remove(ranges.start(0),this.video.currentTime-10);return;}
        if(this.video.paused && !this.playPending) {
          this.playPending=true;const epoch=this.epoch;
          this.video.play().catch(error=>{if(epoch===this.epoch && error.name==='NotAllowedError'){this.pause();this.onStatus('Нажмите «Смотреть» для воспроизведения');}}).finally(()=>{if(epoch===this.epoch)this.playPending=false;});
        }
      }
      if(this.head<this.queue.length) {
        const data=this.queue[this.head++];this.bytes-=data.byteLength;
        if(this.head===this.queue.length){this.queue=[];this.head=0;}
        else if(this.head>=64 && this.head*2>=this.queue.length){this.queue=this.queue.slice(this.head);this.head=0;}
        this.buffer.appendBuffer(data);
      } else if(this.done)this.media.endOfStream();
    } catch {this.retry();}
  }
  fail(message) {if(this.closed)return;this.pause();this.onStatus(message);}
  close() {
    this.closed=true;this.release();this.running=false;this.events.abort();this.video.pause();this.video.removeAttribute('src');this.video.load();
    if(this.url)URL.revokeObjectURL(this.url);
  }
}

function renderArchive(body) {
  stopArchive();
  const today=new Date(), date=`${today.getFullYear()}-${String(today.getMonth()+1).padStart(2,'0')}-${String(today.getDate()).padStart(2,'0')}`;
  body.innerHTML=`${sectionHead('Архив')}<div class="archive-body"><form class="archive-search"><label>Камера<select name="camera_id" required>${state.cameras.map(camera=>`<option value="${camera.id}">${h(camera.name)}</option>`).join('')}</select></label><label>Дата<input name="date" type="date" required value="${date}"></label><button class="primary" type="submit">Найти</button></form><p class="field-hint">Время регистратора</p><p class="archive-error error" role="alert"></p><section class="archive-view" hidden><video muted autoplay playsinline></video><div class="archive-controls"><button type="button" data-play>Пауза</button><button type="button" data-download>Скачать</button><output data-clock></output><input type="range" data-position aria-label="Позиция в записи" min="0" step="1"><button type="button" class="icon-button" data-fullscreen aria-label="На весь экран">${icon('expand')}</button></div><p class="archive-status" role="status"></p></section><div class="archive-records object-list"></div><button type="button" class="archive-more" hidden>Ещё</button></div>`;
  bindAdminHeader(body);
  const form=body.querySelector('form'), list=body.querySelector('.archive-records'),more=body.querySelector('.archive-more'),error=body.querySelector('.archive-error'),view=body.querySelector('.archive-view');
  let cursor=null,epoch=0,query=null;
  const records=new Map();
  const search=async append=>{
    const current=++epoch;
    if(!append){stopArchive();view.hidden=true;records.clear();list.innerHTML='';cursor=null;query=Object.fromEntries(new FormData(form));}
    const submit=form.querySelector('[type=submit]');submit.disabled=more.disabled=true;error.textContent='';
    form.setAttribute('aria-busy','true');list.setAttribute('aria-busy','true');
    form.querySelectorAll('input,select').forEach(input=>input.disabled=true);
    submit.textContent=append ? 'Найти' : 'Поиск…';more.textContent=append ? 'Загрузка…' : 'Ещё';
    if(!append)list.innerHTML='<p class="archive-search-progress" role="status"><span class="search-spinner" aria-hidden="true"></span>Ищем записи…</p>';
    try {
      const result=await api('/api/admin/archive/search','POST',{camera_id:+query.camera_id,date:query.date,cursor});
      if(!body.isConnected || epoch!==current)return;
      if(!append)list.innerHTML='';
      for(const record of result.records) {
        const key=record.start+'|'+record.end;
        if(records.has(key))continue;
        records.set(key,record);
        const button=document.createElement('button');button.type='button';button.className='object-row archive-record';
        button.innerHTML=`<span>${h(record.start.slice(11))} — ${h(record.end.slice(11))}</span>${icon('chevron')}`;
        button.onclick=()=>{
          stopArchive();list.querySelector('[aria-current]')?.removeAttribute('aria-current');button.setAttribute('aria-current','true');view.hidden=false;
          archiveViewer=new ArchivePlayer(view,record,message=>{if(body.isConnected)view.querySelector('.archive-status').textContent=message;});
          archiveViewer.start();view.scrollIntoView({behavior:'smooth',block:'nearest'});
        };
        list.append(button);
      }
      cursor=result.cursor;more.hidden=!cursor;
      if(!records.size)list.innerHTML='<p class="empty">За эту дату записей нет</p>';
    } catch(e){if(body.isConnected && epoch===current){if(!append)list.innerHTML='';error.textContent=e.message;}}
    finally{if(epoch===current){submit.disabled=more.disabled=false;submit.textContent='Найти';more.textContent='Ещё';form.querySelectorAll('input,select').forEach(input=>input.disabled=false);form.removeAttribute('aria-busy');list.removeAttribute('aria-busy');}}
  };
  form.onsubmit=event=>{event.preventDefault();search(false);};more.onclick=()=>search(true);
}

class ArchiveDownload {
  constructor(record,position) {
    this.events=new AbortController();this.closed=false;this.record=record;
    const stamp=offset=>{
      const date=new Date(record.start.replace(' ','T'));date.setSeconds(date.getSeconds()+offset);
      return `${date.getFullYear()}-${String(date.getMonth()+1).padStart(2,'0')}-${String(date.getDate()).padStart(2,'0')}T${date.toTimeString().slice(0,8)}`;
    };
    const start=Math.max(0,Math.min(Math.floor(position),Math.max(0,record.duration-1)));
    dialog('Скачать фрагмент',`<form class="stack archive-export-form"><label>Начало<input name="start" type="datetime-local" step="1" required min="${stamp(0)}" max="${stamp(record.duration)}" value="${stamp(start)}"></label><label>Конец<input name="end" type="datetime-local" step="1" required min="${stamp(0)}" max="${stamp(record.duration)}" value="${stamp(Math.min(record.duration,start+60))}"></label><p class="field-hint">MP4 · без звука · до 15 минут</p><p class="error" role="alert"></p><p class="export-status" role="status"></p><button class="primary" type="submit">Подготовить</button><a class="button primary export-link" hidden download>Скачать MP4</a></form>`);
    this.form=modal.querySelector('form');this.status=this.form.querySelector('.export-status');
    modal.addEventListener('close',()=>this.close(),{signal:this.events.signal});
    this.form.onsubmit=event=>{event.preventDefault();this.prepare();};
  }
  async request(path,method='GET',data) {
    const response=await fetch(path,{method,credentials:'same-origin',cache:'no-store',signal:this.events.signal,
      headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},...(data ? {body:JSON.stringify(data)} : {})});
    const value=await response.json();if(!response.ok)throw new Error(value.error || 'Не удалось подготовить фрагмент');return value;
  }
  async prepare() {
    const inputs=new FormData(this.form),origin=new Date(this.record.start.replace(' ','T'));
    const start=(new Date(inputs.get('start'))-origin)/1000,end=(new Date(inputs.get('end'))-origin)/1000;
    const error=this.form.querySelector('.error'),button=this.form.querySelector('[type=submit]');error.textContent='';
    if(!Number.isInteger(start) || !Number.isInteger(end) || start<0 || end>this.record.duration || start>=end){error.textContent='Укажите интервал внутри выбранной записи';return;}
    if(end-start>900){error.textContent='Можно скачать до 15 минут за один раз';return;}
    button.disabled=true;this.form.querySelectorAll('input').forEach(input=>input.disabled=true);
    this.status.innerHTML='<span class="search-spinner" aria-hidden="true"></span> Готовим фрагмент…';
    try {
      const result=await this.request('/api/admin/archive/exports','POST',{id:this.record.id,start,end});
      if(this.closed)return;
      this.id=result.id;this.poll();
    }catch(e){if(!this.closed)this.fail(e.message);}
  }
  async poll() {
    try {
      const result=await this.request('/api/admin/archive/exports/'+this.id);
      if(this.closed)return;
      if(result.state==='error'){this.fail(result.error);return;}
      if(result.state==='ready') {
        this.status.textContent='';this.form.querySelector('[type=submit]').hidden=true;
        const link=this.form.querySelector('.export-link');link.href='/api/admin/archive/exports/'+this.id+'/download';link.hidden=false;
        link.onclick=()=>{this.handedOff=true;};return;
      }
      this.timer=setTimeout(()=>this.poll(),1000);
    }catch(e){if(!this.closed)this.fail(e.message);}
  }
  discard() {
    if(!this.id || this.handedOff)return;
    fetch('/api/admin/archive/exports/'+this.id,{method:'DELETE',credentials:'same-origin',keepalive:true,headers:{'X-CSRF-Token':csrf}}).catch(()=>{});
    this.id=null;
  }
  fail(message) {
    this.discard();this.status.textContent='';this.form.querySelector('.error').textContent=message;
    this.form.querySelector('[type=submit]').disabled=false;this.form.querySelectorAll('input').forEach(input=>input.disabled=false);
  }
  close() {
    if(this.closed)return;
    this.closed=true;clearTimeout(this.timer);this.discard();this.events.abort();
    if(archiveDownload===this){archiveDownload=null;if(modal.open)modal.close();}
  }
}
