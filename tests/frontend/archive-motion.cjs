const {chromium,devices}=require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const assert=require('node:assert/strict'),fs=require('node:fs'),os=require('node:os'),path=require('node:path'),{execFileSync}=require('node:child_process');
const temp=fs.mkdtempSync(path.join(os.tmpdir(),'cameragate-archive-ui-'));
const sample=path.join(temp,'sample.mp4');
execFileSync(process.env.FFMPEG_BINARY || 'ffmpeg',['-hide_banner','-loglevel','error','-f','lavfi','-i','testsrc=size=160x120:rate=10','-t','4','-c:v','libx264','-bf','0','-threads','1','-pix_fmt','yuv420p','-movflags','empty_moov+default_base_moof+frag_keyframe','-y',sample]);
(async()=>{const browser=await chromium.launch({headless:true,executablePath:process.env.CHROME_EXECUTABLE});try{
 const page=await browser.newPage({...devices['iPhone X']}),errors=[],plays=[];let live,closed=0,searches=0,releaseSearch;
 page.on('pageerror',e=>errors.push(e.message));
 await page.routeWebSocket('**/api/live',socket=>{live=socket;socket.onMessage(()=>{});});
 await page.routeWebSocket('**/api/admin/archive/play',socket=>{
   socket.onClose(()=>closed++);
   socket.onMessage(value=>{
     plays.push(JSON.parse(value));
     socket.send(JSON.stringify({type:'init',mime:'video/mp4; codecs="avc1.64000A"'}));
     socket.send(fs.readFileSync(sample));
   });
 });
 await page.route('**/api/admin/archive/search',async route=>{
   searches++;const input=route.request().postDataJSON();assert.equal(input.camera_id,1);
   if(searches===1)await new Promise(resolve=>releaseSearch=resolve);
   const record={id:'test-record',start:'2026-10-20 10:00:00',end:'2026-10-20 10:01:00',duration:60};
   await route.fulfill({json:{records:input.cursor ? [record,{...record,id:'second',start:'2026-10-20 11:00:00',end:'2026-10-20 11:01:00'}] : [record],cursor:input.cursor ? null : '2026-10-20 10:00:00'}});
 });
 await page.goto(process.env.UI_TEST_URL || 'http://127.0.0.1:8082');
 await page.locator('[name=login]').fill('auditadmin');await page.locator('[name=password]').fill('audit123');await page.locator('#auth-form button[type=submit]').click();await page.locator('#wall').waitFor();
 const cid=await page.evaluate(()=>state.layout.tiles[0].camera_id);
 await page.locator('.tile').first().evaluate(element=>element.classList.add('live'));
 live.send(JSON.stringify({type:'motion',available:true,camera_ids:[cid]}));await page.locator('.tile-motion:visible').first().waitFor();
 live.send(JSON.stringify({type:'motion',available:false,camera_ids:[]}));await page.locator('.tile-motion:visible').first().waitFor({state:'hidden'});
 await page.getByRole('button',{name:'Управление',exact:true}).click();await page.locator('.admin-menu').click();await page.locator('a[href="#/admin/archive"]').click();
 await page.locator('.archive-search [name=camera_id]').selectOption('1');await page.locator('.archive-search [name=date]').fill('2026-10-20');
 await page.locator('.archive-search button').click();await page.locator('.archive-search-progress').waitFor();
 assert.equal(await page.locator('.archive-search button').textContent(),'Поиск…');assert.equal(await page.locator('.archive-search').getAttribute('aria-busy'),'true');
 assert.equal(await page.locator('.archive-search [name=camera_id]').isDisabled(),true);
 releaseSearch();await page.locator('.archive-record').waitFor();assert.equal(await page.locator('.archive-search-progress').count(),0);
 await page.locator('.archive-more').click();await page.locator('.archive-record').nth(1).waitFor();assert.equal(await page.locator('.archive-record').count(),2);assert.equal(searches,2);
 await page.locator('.archive-record').first().click();await page.locator('.archive-view video').waitFor();await page.waitForTimeout(1000);
 const playback=await page.locator('.archive-view video').evaluate(video=>({width:video.videoWidth,time:video.currentTime,paused:video.paused,error:video.error?.message}));
 assert.ok(playback.width>0 && playback.time>0,JSON.stringify({playback,status:await page.locator('.archive-status').textContent(),errors}));
 await page.locator('[data-play]').click();assert.equal(await page.locator('.archive-status').textContent(),'Пауза');
 await page.locator('[data-position]').fill('30');await page.locator('[data-position]').dispatchEvent('change');await page.waitForTimeout(250);
 assert.equal(plays.at(-1).offset,30);assert.ok(closed>=1);
 for(const width of [320,390,768,1440]){
   await page.setViewportSize({width,height:900});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth),width);
 }
 if(process.env.UI_SCREENSHOTS_DIR){fs.mkdirSync(process.env.UI_SCREENSHOTS_DIR,{recursive:true});await page.setViewportSize({width:390,height:844});await page.screenshot({path:path.join(process.env.UI_SCREENSHOTS_DIR,'archive-mobile.png')});}
 await page.getByRole('button',{name:'Прямой эфир',exact:true}).click();await page.locator('#wall').waitFor();assert.equal(await page.evaluate(()=>archiveViewer),null);
 assert.deepEqual(errors,[]);console.log('PASS motion start/unavailable; admin archive search/paging/dedup; actual synthetic MSE playback; pause, seek, cleanup and widths320/390/768/1440');
}finally{await browser.close();fs.rmSync(temp,{recursive:true,force:true});}})().catch(error=>{console.error(error);fs.rmSync(temp,{recursive:true,force:true});process.exitCode=1;});
