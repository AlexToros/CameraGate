const assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs');
let contexts=0;
class Context {
 constructor(){contexts++;this.state='suspended';this.currentTime=100;this.destination={};this.sources=[];this.gains=[];}
 resume(){this.state='running';return Promise.resolve();}suspend(){this.state='suspended';return Promise.resolve();}close(){this.state='closed';return Promise.resolve();}
 createGain(){const g={gain:{setValueAtTime(value){g.level=value;}},connect(){g.connected=true;},disconnect(){g.connected=false;}};this.gains.push(g);return g;}
 createBuffer(channels,length,rate){const pcm=new Float32Array(length);return {getChannelData:()=>pcm,pcm,rate};}
 createBufferSource(){const s={connect(g){s.gain=g;},disconnect(){s.disconnected=true;},start(at){s.at=at;}};this.sources.push(s);return s;}
}
const scope={window:{AudioContext:Context},AbortController};vm.createContext(scope);vm.runInContext(fs.readFileSync('frontend/audio.js','utf8'),scope);
const packet=(stamp,values=[0xd5,0x55,0])=>{const data=new Uint8Array(10+values.length);data[0]=14;data[1]=2;new DataView(data.buffer).setFloat64(2,stamp);data.set(values,10);return data;};
(async()=>{
 const audio=new scope.window.CameraAudio(),video=new EventTarget();Object.assign(video,{paused:false,readyState:4,currentTime:5});
 const player={video,offset:-10,closed:false,element:{classList:{contains:()=>true}}};
 await audio.start(video);assert.equal(audio.context.gains.length,0);
 audio.receive(packet(15.05),player);assert.equal(audio.samples,3);assert.equal(contexts,1);
 const ctx=audio.context,first=ctx.sources[0];assert.ok(Math.abs(first.at-100.05)<.0001);assert.equal(first.buffer.rate,8000);
 assert.deepEqual([...first.buffer.pcm],[8/32768,-8/32768,-5504/32768]);
 audio.receive(packet(40),player);audio.receive(packet(10),player);assert.equal(ctx.sources.length,1);
 video.dispatchEvent(new Event('waiting'));assert.equal(first.gain.connected,false);assert.equal(first.gain.level,0);
 await audio.start(video);assert.equal(contexts,1);audio.receive(packet(15.05),player);assert.equal(audio.samples,3);
 const second=ctx.sources.at(-1);audio.stop();assert.equal(second.gain.connected,false);assert.equal(ctx.state,'suspended');
 audio.receive(packet(15.05),player);assert.equal(ctx.sources.length,2);
 await audio.start(video);player.video.paused=true;
 const gains=ctx.gains.length;
 for(let i=0;i<100;i++)audio.receive(packet(15.05),player);
 assert.equal(ctx.sources.length,2);assert.equal(ctx.gains.length,gains);
 player.video.paused=false;
 audio.receive(packet(16.2),player);assert.equal(ctx.sources.length,3);assert.ok(Math.abs(ctx.sources.at(-1).at-101.2)<.0001);
 let error;audio.onError=value=>error=value;ctx.createBuffer=()=>{throw new Error('unsupported');};
 audio.receive(packet(15.05),player);assert.equal(audio.enabled,false);assert.equal(error,'Не удалось воспроизвести звук');
 audio.close();assert.equal(ctx.state,'closed');assert.equal(audio.context,null);
 console.log('PASS A-law golden samples, selected source isolation, sync bounds, flush on waiting/switch/stop, context reuse and close');
})().catch(e=>{console.error(e);process.exitCode=1;});
