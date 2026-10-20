/* Exercise the real socket handler without cameras or a database. */
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync('frontend/app.js','utf8');
const handler=source.slice(source.indexOf('function connectVideo() {'),source.indexOf('window.addEventListener("beforeinstallprompt"'));
function run(count) {
 const tiles=Array.from({length:count},(_,i)=>({id:String(i+1),camera_id:i+1}));
 const elements=tiles.map(t=>({dataset:{tile:t.id}})),player={},received=[],video=[],drawn=[];
 let queries=0,subscriptions=0;
 class Socket {constructor(){this.close=()=>assert.fail('Valid media must not close the socket');}}
 const streams=new Map(tiles.map(t=>[t.camera_id,{players:new Map([[elements[0],player]]),receive:(kind,data)=>video.push([t.camera_id,kind,data])}]));
 const scope={WebSocket:Socket,stopVideo(){},me:{},page:'wall',document:{hidden:false,querySelectorAll(){queries++;return elements;}},socketEpoch:1,
   location:{protocol:'http:',host:'localhost'},state:{layout:{tiles}},subscribe(){},subscriptions(){subscriptions++;return tiles.map(t=>t.camera_id);},
   sourceSubscriptions(){return scope.subscriptions();},audioSourceId(){return 1;},HD_OFFSET:256,hdSelection:null,
   mediaStreams:streams,audioSelection:{cid:1,element:elements[0]},cameraAudio:{receive:(data,p)=>received.push([data,p])},drawCached:(tile)=>drawn.push(tile.camera_id),
   DataView,Uint8Array,TextDecoder,console};
 vm.createContext(scope);vm.runInContext(handler+';connectVideo();',scope);
 function record(cid,kind,bytes) {
   const data=new Uint8Array(9+bytes.length),view=new DataView(data.buffer);
   view.setUint32(0,cid);view.setUint8(4,kind);view.setUint32(5,bytes.length);data.set(bytes,9);return data;
 }
 function send(...records) {
   const bytes=new Uint8Array(records.reduce((n,r)=>n+r.length,0));let offset=0;
   for(const record of records){bytes.set(record,offset);offset+=record.length;}
   scope.socket.onmessage({data:bytes.buffer});return bytes.buffer;
 }
 const original=send(record(1,4,[14,2,3]));
 assert.equal(received.length,1);assert.equal(received[0][0].buffer,original);assert.equal(received[0][1],player);
 assert.equal(queries,0);assert.equal(subscriptions,0);assert.equal(video.length,0);
 send(record(2,4,[14,2,4]));assert.equal(received.length,1);assert.equal(queries,0);
 send(record(1,4,[14,2,5]),...tiles.map(t=>record(t.camera_id,2,[t.camera_id])));
 assert.equal(received.length,2);assert.equal(video.length,count);assert.equal(queries,1);assert.equal(subscriptions,1);
 assert.deepEqual(drawn,tiles.map(t=>t.camera_id));
 if(process.env.BENCH_AUDIO_ROUTING) {
   const bytes=record(1,4,[14,2,3]).buffer;
   const start=process.hrtime.bigint(),iterations=10000;
   for(let i=0;i<iterations;i++){scope.socket.onmessage({data:bytes});received.length=0;}
   console.log({tiles:count,audioPacketMicroseconds:Number(process.hrtime.bigint()-start)/iterations/1000});
 }
}
for(const count of [2,16,64])run(count);
console.log('PASS audio-only packets borrow bytes without wall scans; mixed batches retain every video and use one wall traversal');
