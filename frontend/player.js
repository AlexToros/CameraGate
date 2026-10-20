/* Compressed fMP4 playback. No credentials, transcoding or unbounded queues. */
(() => {
  const MAX_BYTES = 2 * 1024 * 1024;
  class BufferPolicy {
    constructor(){this.target=.6;this.recovering=false;this.lastStall=this.lastDecay=Date.now();}
    stall(now=Date.now()){
      if(this.recovering)return;
      this.target=Math.min(1.5,this.target+.3);this.recovering=true;this.lastStall=now;
    }
    ready(ahead,now=Date.now()){
      if(this.recovering && ahead<this.target)return false;
      this.recovering=false;
      if(now-this.lastStall>30000 && now-this.lastDecay>10000){this.target=Math.max(.6,this.target-.1);this.lastDecay=now;}
      return true;
    }
    position(start,end,current,started){
      // First picture never waits for the recovery reserve. Start at the live
      // edge of the data already received; build a reserve only after a stall.
      if(!started)return Math.max(start,end-.05);
      return current<start || end-current>Math.max(3,this.target+1.5) ? Math.max(start,end-this.target) : null;
    }
  }
  const blockedPlayers=new Set();
  function playbackPolicy() { window.dispatchEvent(new CustomEvent("camera-playback-policy",{detail:{blocked:blockedPlayers.size>0}})); }
  function resumePlayback() { for(const player of [...blockedPlayers])player.play(true); }
  document.addEventListener("pointerdown",resumePlayback,{capture:true});
  document.addEventListener("keydown",resumePlayback,{capture:true});
  function captureFrame(element,force=false) {
    const video=element.querySelector("video"),canvas=element.querySelector(".last-frame");
    if(!video || !canvas || video.readyState<2 || video.error || video.seeking || !video.videoWidth)return;
    if(!force && Date.now()-(+canvas.dataset.captured||0)<1000)return;
    try {
      if(canvas.width!==video.videoWidth || canvas.height!==video.videoHeight) { canvas.width=video.videoWidth;canvas.height=video.videoHeight; }
      canvas.getContext("2d").drawImage(video,0,0);canvas.dataset.captured=Date.now();
    } catch { /* A snapshot failure must not interrupt live playback. */ }
  }
  function freezeFrame(element) {
    captureFrame(element);
    const canvas=element.querySelector(".last-frame"),saved=Boolean(canvas?.dataset.captured);
    if(canvas)canvas.hidden=!saved;
    element.classList.toggle("stale",saved);element.classList.remove("live");
    return saved;
  }
  function timestamp(data) {
    const view = new DataView(data.buffer, data.byteOffset, data.byteLength);
    function find(start,end) {
      for(let offset=start;offset+8<=end;) {
        const size=view.getUint32(offset),kind=view.getUint32(offset+4);
        if(size<8 || offset+size>end) throw new Error("Неверный видеофрагмент");
        if(kind===0x74666474) return view.getUint8(offset+8)===1 ? (view.getUint32(offset+12)*4294967296+view.getUint32(offset+16))/90000 : view.getUint32(offset+12)/90000;
        if(kind===0x6d6f6f66 || kind===0x74726166) { const value=find(offset+8,offset+size);if(value!==null)return value; }
        offset+=size;
      }
      return null;
    }
    const result=find(0,data.byteLength);
    if(result===null) throw new Error("Нет времени видеофрагмента");
    return result;
  }
  class Player {
    constructor(stream,element) {
      this.stream=stream;this.element=element;this.queue=[];this.head=0;this.bytes=0;this.closed=false;this.offset=null;this.started=false;
      this.policy=new BufferPolicy();this.hasPlayed=false;
      this.video=element.querySelector("video");this.events=new AbortController();
      this.video.defaultMuted=true;this.video.muted=true;this.video.autoplay=true;this.video.playsInline=true;this.video.disableRemotePlayback=true;this.video.controls=false;this.video.preload="auto";
      // WebKit evaluates autoplay visibility before the first decoded frame.
      // Keep the muted inline video visible beneath the status/snapshot layers.
      this.video.hidden=false;
      const Media=window.ManagedMediaSource || window.MediaSource;
      if(!Media || !Media.isTypeSupported(stream.mime)) { this.fail("Браузер не поддерживает кодек этой камеры. Откройте в совместимом браузере.");return; }
      try {
        this.media=new Media();this.url=URL.createObjectURL(this.media);this.video.src=this.url;
        this.media.addEventListener("sourceopen",()=>{
          if(this.closed)return;
          try {
            this.buffer=this.media.addSourceBuffer(stream.mime);this.media.duration=Infinity;
            if(this.offset!==null) { this.buffer.timestampOffset=this.offset;this.offsetApplied=true; }
            this.buffer.addEventListener("updateend",()=>this.pump());
            this.buffer.addEventListener("error",()=>this.fail("Ошибка воспроизведения. Переподключаемся…"));
            this.pump();
          } catch { this.fail("Не удалось запустить видео в этом браузере"); }
        },{once:true});
        this.video.addEventListener("loadeddata",()=>{this.visible();captureFrame(this.element,true);this.play();},{once:true,signal:this.events.signal});
        this.video.addEventListener("playing",()=>{this.hasPlayed=true;this.visible();},{signal:this.events.signal});
        this.video.addEventListener("waiting",()=>{
          const ranges=this.video.buffered,last=ranges.length-1;
          const ahead=last>=0 && this.video.currentTime>=ranges.start(last) ? ranges.end(last)-this.video.currentTime : 0;
          // waiting also fires during seeks/decoder transitions with data still
          // available. Only starvation should deliberately pause for refill.
          if(!this.closed && this.hasPlayed && !this.video.seeking && this.video.readyState<3 && ahead<.35 && !this.policy.recovering){this.policy.stall();this.video.pause();}
        },{signal:this.events.signal});
        this.video.addEventListener("timeupdate",()=>captureFrame(this.element),{signal:this.events.signal});
        this.video.addEventListener("error",()=>{if(!this.closed){console.warn("camera_media_error",{code:this.video.error?.code,detail:this.video.error?.message});this.fail("Восстанавливаем видео…");}},{signal:this.events.signal});
        this.enqueue(stream.init);
        for(const fragment of stream.fragments)this.enqueue(fragment,true);
      } catch { this.fail("Не удалось запустить видеоплеер"); }
    }
    visible() {
      if(this.closed || !this.element.isConnected)return;
      this.video.hidden=false;this.element.classList.add("live");this.element.classList.remove("stale");
      const canvas=this.element.querySelector(".last-frame");if(canvas)canvas.hidden=true;
      captureFrame(this.element);
      if(this.video.videoWidth && this.video.videoHeight) {
        const ratio=this.video.videoWidth/this.video.videoHeight;
        this.element.style.setProperty("--ratio",ratio);this.stream.onRatio(ratio);
      }
    }
    play(gesture=false) {
      if(this.closed || this.policy.recovering || this.playPending || (!gesture && blockedPlayers.has(this)) || !this.video.paused)return;
      this.video.muted=true;this.playPending=true;
      const result=this.video.play();
      if(!result) { this.playPending=false;return; }
      result.then(()=>{blockedPlayers.delete(this);playbackPolicy();}).catch(error=>{
        if(!this.closed && error.name==="NotAllowedError") { blockedPlayers.add(this);playbackPolicy(); }
      }).finally(()=>{this.playPending=false;});
    }
    enqueue(data,media=false) {
      if(this.closed)return;
      if(media && this.offset===null) this.offset=-timestamp(data);
      if(this.bytes+data.byteLength>MAX_BYTES) { this.fail("Соединение не успевает за эфиром. Переподключаемся…");return; }
      this.queue.push(data);this.bytes+=data.byteLength;this.pump();
    }
    pump() {
      if(this.closed || !this.buffer || this.buffer.updating)return;
      try {
        const ranges=this.buffer.buffered;
        if(ranges.length) {
          const start=ranges.start(ranges.length-1),end=ranges.end(ranges.length-1);
          const position=this.policy.position(start,end,this.video.currentTime,this.started);
          if(position!==null){this.video.currentTime=position;this.started=true;}
          if(this.policy.ready(end-this.video.currentTime))this.play();
          const earliest=ranges.start(0);
          if(end-earliest>8 && this.video.currentTime>earliest+4) { this.buffer.remove(earliest,this.video.currentTime-3);return; }
        }
        if(this.head>=this.queue.length)return;
        const data=this.queue[this.head++];this.bytes-=data.byteLength;
        if(this.head===this.queue.length) { this.queue=[];this.head=0; }
        else if(this.head>=64 && this.head*2>=this.queue.length) { this.queue=this.queue.slice(this.head);this.head=0; }
        if(this.offset!==null && !this.offsetApplied) { this.buffer.timestampOffset=this.offset;this.offsetApplied=true; }
        this.buffer.appendBuffer(data);
      } catch { this.fail("Ошибка видеопотока. Переподключаемся…"); }
    }
    fail(message) {
      this.close();this.element.classList.remove("live");this.element.querySelector(".stream-state").textContent=message;
      this.retryAt=Date.now()+5000;
    }
    close() {
      if(this.closed)return;
      freezeFrame(this.element);this.events.abort();blockedPlayers.delete(this);playbackPolicy();
      this.closed=true;this.queue=[];this.head=0;this.bytes=0;
      if(this.video) { this.video.hidden=true;this.video.pause();this.video.removeAttribute("src");this.video.load(); }
      if(this.url)URL.revokeObjectURL(this.url);
    }
  }
  class CameraStream {
    constructor(mime,init,onRatio) { this.mime=mime;this.init=init;this.onRatio=onRatio;this.fragments=[];this.bytes=0;this.players=new Map();this.lastFrame=0;this.keyTime=0; }
    receive(kind,data) {
      if(kind===3) { this.fragments=[];this.bytes=0;this.keyTime=Date.now(); }
      else if(!this.fragments.length)return;
      if(this.bytes+data.byteLength>MAX_BYTES || Date.now()-this.keyTime>15000) { this.fragments=[];this.bytes=0;return; }
      this.fragments.push(data);this.bytes+=data.byteLength;this.lastFrame=Date.now();
      for(const [element,player] of this.players) {
        if(!element.isConnected) { player.close();this.players.delete(element);continue; }
        if(player.closed && player.retryAt && Date.now()>=player.retryAt && kind===3) { this.players.delete(element);this.attach(element); }
        else player.enqueue(data,true);
      }
    }
    attach(element) {
      if(!this.players.has(element) && this.fragments.length && Date.now()-this.lastFrame<5000)this.players.set(element,new Player(this,element));
    }
    detach(element) { const player=this.players.get(element);if(player){player.close();this.players.delete(element);} }
    releasePlayers() { for(const player of this.players.values())player.close();this.players.clear(); }
    destroy() { this.releasePlayers();this.fragments=[];this.bytes=0; }
  }
  window.CameraStream=CameraStream;
  CameraStream.BufferPolicy=BufferPolicy;
  window.captureCameraFrame=captureFrame;
  window.freezeCameraFrame=freezeFrame;
  window.resumeCameraPlayback=resumePlayback;
})();
