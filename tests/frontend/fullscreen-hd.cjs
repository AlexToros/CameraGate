/* Actual MSE decoders with synthetic SD/Main, no recorder or account mutations. */
const {chromium,devices}=require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const assert=require('node:assert/strict'),fs=require('node:fs'),os=require('node:os'),path=require('node:path'),{execFileSync}=require('node:child_process');
const temp=fs.mkdtempSync(path.join(os.tmpdir(),'cameragate-hd-ui-'));
function movie(name,size){
 const file=path.join(temp,name+'.mp4');
 execFileSync(process.env.FFMPEG_BINARY || 'ffmpeg',['-hide_banner','-loglevel','error','-f','lavfi','-i',`testsrc=size=${size}:rate=10`,'-t','4','-c:v','libx264','-bf','0','-threads','1','-pix_fmt','yuv420p','-movflags','empty_moov+default_base_moof+frag_keyframe','-y',file]);
 const data=fs.readFileSync(file),init=[],media=[];
 for(let at=0;at<data.length;){const length=data.readUInt32BE(at),kind=data.toString('ascii',at+4,at+8),box=data.subarray(at,at+length);if(['ftyp','moov'].includes(kind))init.push(box);if(['moof','mdat'].includes(kind))media.push(box);at+=length;}
 return {init:Buffer.concat(init),media:Buffer.concat(media)};
}
const sd=movie('sd','160x90'),hd=movie('hd','960x540'),mime='video/mp4; codecs="avc1.64001F"';
function packet(cid,kind,bytes){const head=Buffer.alloc(9);head.writeUInt32BE(cid);head[4]=kind;head.writeUInt32BE(bytes.length,5);return Buffer.concat([head,bytes]);}
function init(cid,movie,type=mime){const m=Buffer.from(type),head=Buffer.alloc(2);head.writeUInt16BE(m.length);return packet(cid,1,Buffer.concat([head,m,movie.init]));}
(async()=>{const browser=await chromium.launch({headless:true,executablePath:process.env.CHROME_EXECUTABLE});try{
 const page=await browser.newPage({...devices['iPhone X']}),messages=[],errors=[];let socket;
 const shot=async name=>{if(process.env.UI_SCREENSHOTS_DIR){fs.mkdirSync(process.env.UI_SCREENSHOTS_DIR,{recursive:true});await page.screenshot({path:path.join(process.env.UI_SCREENSHOTS_DIR,name+'.png')});}};
 page.on('pageerror',error=>errors.push(error.message));
 await page.routeWebSocket('**/api/live',ws=>{socket=ws;const sent=new Set();ws.onMessage(value=>{
  const message=JSON.parse(value);messages.push(message);
  for(const cid of message.camera_ids)if(!sent.has(cid)){sent.add(cid);ws.send(init(cid,sd));ws.send(packet(cid,3,sd.media));}
 });});
 const width=async wanted=>{
  let latest;
  for(let attempt=0;attempt<100;attempt++){
   latest=await page.locator('.tile video').first().evaluate(video=>({width:video.videoWidth,ready:video.readyState,live:video.closest('.tile').classList.contains('live')}));
   if(latest.width===wanted && latest.ready>=2 && latest.live)return;
   await page.waitForTimeout(50);
  }
  assert.fail(JSON.stringify({wanted,latest}));
 };
 const aspect=async wanted=>{
  const result=await page.locator('.tile').first().evaluate(element=>{
   const video=element.querySelector('video'),canvas=element.querySelector('.last-frame'),rect=video.getBoundingClientRect(),hidden=canvas.hidden;
   canvas.hidden=false;const still=canvas.getBoundingClientRect();canvas.hidden=hidden;
   return {width:video.videoWidth,source:element.sourceId,ratio:getComputedStyle(element).getPropertyValue('--ratio'),native:video.videoWidth/video.videoHeight,display:rect.width/rect.height,objectFit:getComputedStyle(video).objectFit,still:still.width/still.height};
  });
  assert.ok(Math.abs(result.display-wanted)<.02,JSON.stringify(result));assert.equal(result.objectFit,'fill');
  assert.ok(Math.abs(result.still-wanted)<.02,JSON.stringify(result));
 };
 await page.goto(process.env.UI_TEST_URL || 'http://127.0.0.1:8082');await page.locator('[name=login]').fill('auditadmin');await page.locator('[name=password]').fill('audit123');await page.locator('#auth-form button[type=submit]').click();await page.locator('#wall').waitFor();
 await page.locator('#preset').click();await page.locator('[data-preset="4"]').click();await width(160);
 await aspect(4/3); // Native SD is 16:9, displayed as the initial HD reference.
 await shot('sd-stretched-mobile');
 await page.locator('.tile-audio').first().click();await page.evaluate(()=>{window.originalVideo=document.querySelector('.tile video');window.originalContext=cameraAudio.context;});
 await page.locator('.tile-expand').first().click();
 assert.equal(messages.at(-1).hd_camera_id,1);assert.equal(await page.locator('video').count(),4);
 socket.send(init(257,hd));await page.waitForTimeout(100);
 assert.equal(await page.locator('.tile video').first().evaluate(v=>v.videoWidth),160);
 socket.send(packet(257,3,hd.media));await width(960);
 await aspect(16/9); // Actual Main dimensions replace the initial reference.
 await shot('hd-native-mobile');
 assert.equal(await page.evaluate(()=>document.querySelector('.tile video')===originalVideo && cameraAudio.context===originalContext && cameraAudio.enabled),true);
 assert.equal(messages.at(-1).audio_hd,true);
 assert.equal(await page.evaluate(()=>[...mediaStreams.values()].reduce((n,s)=>n+s.players.size,0)),4);
 // A failed Main source falls back to the cached SD without a second decoder.
 socket.send(JSON.stringify({type:'status',cameras:{1:'Прямой эфир',257:'Восстанавливаем HD'}}));await width(160);
 await aspect(16/9);
 assert.equal(messages.at(-1).audio_hd,false);
 socket.send(packet(257,3,hd.media));await width(960);
 await page.evaluate(()=>document.fullscreenElement ? document.exitFullscreen() : finishCameraFullscreen());await width(160);
 await aspect(16/9); // SD keeps the learned Main ratio after fullscreen closes.
 assert.equal(messages.at(-1).hd_camera_id,null);assert.equal(messages.at(-1).audio_hd,false);
 assert.equal(await page.evaluate(()=>cameraAudio.enabled && cameraAudio.context===originalContext && !mediaStreams.has(257)),true);
 socket.send(packet(257,3,hd.media));await page.waitForTimeout(100);assert.equal(await page.locator('.tile video').first().evaluate(v=>v.videoWidth),160);
 // The viewport fallback also requests HD, but an unsupported codec keeps SD.
 await page.locator('.picture').first().evaluate(picture=>{picture.requestFullscreen=undefined;picture.querySelector('video').webkitEnterFullscreen=undefined;});
 await page.locator('.tile-expand').first().click();await page.locator('.video-expanded').waitFor();
 await aspect(16/9);
 socket.send(init(257,hd,'video/mp4; codecs="unsupported"'));
 await page.getByText('Браузер не поддерживает HD этой камеры',{exact:true}).waitFor();
 assert.equal(messages.at(-1).hd_camera_id,null);assert.equal(messages.at(-1).audio_hd,false);await width(160);
 await page.locator('.tile-expand').first().click();assert.equal(await page.locator('.video-expanded').count(),0);
 // A different camera's SD keeps its own reference; no extra Main subscriptions.
 const other=await page.locator('.tile').nth(1).locator('video').evaluate(video=>{const rect=video.getBoundingClientRect();return rect.width/rect.height;});
 assert.ok(Math.abs(other-4/3)<.02);
 assert.ok(messages.every(message=>message.hd_camera_id==null || message.hd_camera_id===1));
 for(const viewport of [{width:320,height:800},{width:900,height:500}]){
  await page.setViewportSize(viewport);
  await page.locator('.tile-expand').first().click();await page.locator('.video-expanded').waitFor();await aspect(16/9);
  const bounds=await page.locator('.tile video').first().boundingBox();assert.ok(bounds.width<=viewport.width+.1 && bounds.height<=viewport.height+.1);
  await page.locator('.tile-expand').first().click();
 }
 assert.deepEqual(errors,[]);console.log('PASS SD retained until HD keyframe; same video/decoder count; audio follows profile; fallback/recovery; exit releases HD and ignores late packets');
}finally{await browser.close();fs.rmSync(temp,{recursive:true,force:true});}})().catch(error=>{console.error(error);fs.rmSync(temp,{recursive:true,force:true});process.exitCode=1;});
