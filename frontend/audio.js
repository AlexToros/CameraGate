/* One audible camera per page. Compressed G.711 is decoded only here. */
(() => {
  const RATE=8000,MAX_LEAD=2;
  const alaw=Float32Array.from({length:256},(_,byte)=>{
    const value=byte^0x55,segment=(value&112)>>4;
    let level=(value&15)<<4;
    level=segment===0 ? level+8 : segment===1 ? level+264 : (level+264)<<(segment-1);
    return (value&128 ? level : -level)/32768;
  });
  class CameraAudio {
    constructor(){this.context=null;this.enabled=false;this.packets=0;this.samples=0;this.epoch=0;}
    async start(video){
      const Context=window.AudioContext || window.webkitAudioContext;
      if(!Context)throw new Error("Браузер не поддерживает звук камер");
      this.binding?.abort();this.binding=new AbortController();
      this.enabled=true;this.packets=this.samples=0;
      if(!this.context || this.context.state==="closed")this.context=new Context({latencyHint:"interactive"});
      this.reset();const epoch=++this.epoch;
      const reset=()=>this.reset();
      video.addEventListener("pause",reset,{signal:this.binding.signal});
      video.addEventListener("waiting",reset,{signal:this.binding.signal});
      video.addEventListener("seeking",reset,{signal:this.binding.signal});
      await this.context.resume();
      if(epoch!==this.epoch)return;
      if(this.context.state!=="running")throw new Error("Не удалось включить звук. Нажмите ещё раз");
    }
    reset(){
      if(this.gain){this.gain.gain.setValueAtTime(0,this.context.currentTime);this.gain.disconnect();this.gain=null;}
      this.nextAt=null;
    }
    receive(data,player){
      try {this.decode(data,player);} catch {this.stop();this.onError?.("Не удалось воспроизвести звук");}
    }
    decode(data,player){
      if(!this.enabled || data.byteLength<=10 || data.byteLength>8010 || data[0]!==14 || data[1]!==2)return;
      this.packets++;
      if(!player || player.closed || player.offset===null || player.video.paused || player.video.readyState<2 || !player.element.classList.contains("live")) {this.reset();return;}
      const ctx=this.context;if(!ctx || ctx.state!=="running")return;
      const stamp=new DataView(data.buffer,data.byteOffset,data.byteLength).getFloat64(2),now=ctx.currentTime;
      const delta=stamp-(player.video.currentTime-player.offset);
      if(!Number.isFinite(delta) || delta<-.2 || delta>MAX_LEAD)return;
      const desired=now+Math.max(.02,delta);
      if(this.nextAt!==null && Math.abs(desired-this.nextAt)>.15)this.reset();
      const start=this.nextAt===null ? desired : Math.max(now+.01,this.nextAt);
      if(start>now+MAX_LEAD+.05)return;
      if(!this.gain){this.gain=ctx.createGain();this.gain.connect(ctx.destination);}
      const input=data.subarray(10),buffer=ctx.createBuffer(1,input.length,RATE),pcm=buffer.getChannelData(0);
      for(let i=0;i<input.length;i++)pcm[i]=alaw[input[i]];
      const source=ctx.createBufferSource();source.buffer=buffer;source.connect(this.gain);source.onended=()=>source.disconnect();source.start(start);
      this.nextAt=start+input.length/RATE;this.samples+=input.length;
    }
    stop(){this.epoch++;this.enabled=false;this.binding?.abort();this.reset();if(this.context?.state==="running")this.context.suspend().catch(()=>{});}
    close(){this.stop();const ctx=this.context;this.context=null;if(ctx && ctx.state!=="closed")ctx.close().catch(()=>{});}
  }
  window.CameraAudio=CameraAudio;
})();
