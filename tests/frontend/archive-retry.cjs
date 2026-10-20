/* Archive connection lifecycle with deterministic timers, no recorder/network. */
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
function fixture(){
 const sockets=[],timers=new Map(),status=[];let id=0;
 const video={currentTime:0,paused:true,handlers:{},addEventListener(name,fn){this.handlers[name]=fn;},pause(){this.paused=true;},load(){this.currentTime=0;},removeAttribute(){},play(){this.paused=false;return Promise.resolve();}};
 const nodes={video,'[data-position]':{},'[data-play]':{},'[data-clock]':{},'[data-fullscreen]':{}};
 class Socket{constructor(){sockets.push(this);this.sent=[];}send(v){this.sent.push(JSON.parse(v));}close(){this.onclose?.();}}
 const context=vm.createContext({AbortController,Date,Math,JSON,WebSocket:Socket,csrf:'test',location:{protocol:'https:',host:'example.test'},URL:{revokeObjectURL(){}},setTimeout(fn,delay){timers.set(++id,{fn,delay});return id;},clearTimeout(id){timers.delete(id);}});
 vm.runInContext(fs.readFileSync('frontend/archive.js','utf8')+'\nglobalThis.Player=ArchivePlayer;',context);
 const player=new context.Player({querySelector:s=>nodes[s]},{id:'record',duration:600,start:'2026-10-20 10:00:00'},s=>status.push(s));
 function fire(){assert.equal(timers.size,1);const [key,{fn}]=timers.entries().next().value;timers.delete(key);fn();}
 return {player,video,sockets,timers,status,nodes,fire};
}
{
 const f=fixture();f.player.start(30);f.video.currentTime=7.8;
 const old=f.sockets[0];old.onerror();old.onclose();
 assert.equal(f.player.running,true);assert.equal(f.timers.size,1);assert.equal(f.nodes['[data-play]'].textContent,'Пауза');
 assert.equal(f.status.at(-1),'Восстанавливаем запись…');f.fire();f.sockets[1].onopen();
 assert.equal(f.sockets[1].sent[0].offset,37);old.onclose();assert.equal(f.timers.size,0);
 for(const delay of [2000,4000,8000,15000,15000]){f.sockets.at(-1).onclose();assert.equal([...f.timers.values()][0].delay,delay);f.fire();}
 f.video.currentTime=2;f.video.handlers.timeupdate();f.sockets.at(-1).onclose();assert.equal([...f.timers.values()][0].delay,1000);
 f.player.close();assert.equal(f.timers.size,0);
}
for(const action of ['pause','close']){
 const f=fixture();f.player.start(15);f.sockets[0].onclose();assert.equal(f.timers.size,1);f.player[action]();
 assert.equal(f.timers.size,0);f.sockets[0].onclose();assert.equal(f.sockets.length,1);assert.equal(f.timers.size,0);
}
{
 const f=fixture();f.player.start();f.sockets[0].onclose();f.player.start(90);
 assert.equal(f.timers.size,0);f.sockets.at(-1).onopen();assert.equal(f.sockets.at(-1).sent[0].offset,90);
 f.sockets[0].onclose();assert.equal(f.timers.size,0);f.player.close();
}
{
 const f=fixture();f.player.start();f.sockets[0].onmessage({data:JSON.stringify({type:'error',error:'temporary',retryable:true})});assert.equal(f.timers.size,1);f.fire();
 f.sockets.at(-1).onmessage({data:JSON.stringify({type:'error',error:'Access denied',retryable:false})});
 assert.equal(f.timers.size,0);assert.equal(f.player.running,false);assert.equal(f.status.at(-1),'Access denied');
}
{
 const f=fixture();f.player.start();f.sockets[0].onmessage({data:JSON.stringify({type:'end'})});f.sockets[0].onclose();
 assert.equal(f.timers.size,0);f.video.handlers.ended();assert.equal(f.status.at(-1),'Запись закончилась');
}
{
 const f=fixture();f.player.start();f.sockets[0].onmessage({data:JSON.stringify({type:'end'})});
 f.video.handlers.error();assert.equal(f.timers.size,1);f.player.close();
}
console.log('PASS archive reconnect/backoff/resume position, error+close dedup, pause/close/seek cancellation, terminal errors and EOF');
