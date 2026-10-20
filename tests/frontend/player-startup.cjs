const {chromium}=require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const assert=require('node:assert/strict'),fs=require('node:fs'),os=require('node:os'),path=require('node:path'),{execFileSync}=require('node:child_process');
const temp=fs.mkdtempSync(path.join(os.tmpdir(),'cameragate-startup-')),sample=path.join(temp,'sample.mp4');
(async()=>{let browser;try{
 execFileSync(process.env.FFMPEG_BINARY || 'ffmpeg',['-hide_banner','-loglevel','error','-f','lavfi','-i','testsrc=size=160x120:rate=10','-t','3','-c:v','libx264','-bf','0','-g','10','-threads','1','-pix_fmt','yuv420p','-movflags','empty_moov+default_base_moof+frag_every_frame','-y',sample]);
 const bytes=fs.readFileSync(sample),init=[],frames=[];let current=[];
 for(let offset=0;offset<bytes.length;){const size=bytes.readUInt32BE(offset),kind=bytes.toString('ascii',offset+4,offset+8),box=bytes.subarray(offset,offset+size);assert.ok(size>=8);if(kind==='ftyp' || kind==='moov')init.push(box);else if(kind==='moof')current=[box];else if(kind==='mdat'){current.push(box);frames.push(Buffer.concat(current));}offset+=size;}
 browser=await chromium.launch({headless:true,executablePath:process.env.CHROME_EXECUTABLE});const page=await browser.newPage(),errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.setContent('<article class="tile"><video></video><canvas class="last-frame" hidden></canvas><span class="stream-state"></span></article>');
 await page.addScriptTag({path:'frontend/player.js'});
 await page.evaluate(({init,first})=>{window.stream=new CameraStream('video/mp4; codecs="avc1.64000A"',Uint8Array.from(init),()=>{});stream.receive(3,Uint8Array.from(first));stream.attach(document.querySelector('.tile'));},{init:[...Buffer.concat(init)],first:[...frames[0]]});
 // Only one 100 ms frame exists: startup must not wait for a 600 ms reserve.
 await page.locator('.tile.live').waitFor();assert.equal(await page.locator('video').evaluate(v=>v.videoWidth),160);
 await page.evaluate(frames=>{for(const frame of frames)stream.receive(2,Uint8Array.from(frame));},frames.slice(1,11).map(b=>[...b]));
 await page.evaluate(()=>new Promise(resolve=>{const player=[...stream.players.values()][0];function flushed(){if(!player.bytes && !player.buffer.updating)return resolve();player.buffer.addEventListener('updateend',flushed,{once:true});}flushed();}));
 await page.locator('video').evaluate(v=>v.play());
 await page.locator('video').evaluate(v=>new Promise(resolve=>{if(v.paused)return resolve();v.addEventListener('pause',resolve,{once:true});}));
 assert.equal(await page.evaluate(()=>[...stream.players.values()][0].policy.recovering),true);
 await page.evaluate(frame=>stream.receive(2,Uint8Array.from(frame)),[...frames[11]]);
 assert.equal(await page.locator('video').evaluate(v=>v.paused),true);
 await page.evaluate(frames=>{for(const frame of frames)stream.receive(2,Uint8Array.from(frame));},frames.slice(12).map(b=>[...b]));
 await page.locator('video').evaluate(v=>new Promise(resolve=>{if(!v.paused)return resolve();v.addEventListener('playing',resolve,{once:true});}));
 assert.equal(await page.evaluate(()=>[...stream.players.values()][0].policy.recovering),false);
 assert.deepEqual(errors,[]);console.log('PASS first frame displayed without startup reserve; adaptive reserve only after actual starvation, bounded refill resumes existing decoder');
}finally{await browser?.close();fs.rmSync(temp,{recursive:true,force:true});}})().catch(e=>{console.error(e);process.exitCode=1;});
