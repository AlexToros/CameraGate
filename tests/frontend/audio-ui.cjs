const {chromium,devices}=require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const assert=require('node:assert/strict');
const base=process.env.UI_TEST_URL || 'http://127.0.0.1:8082';
(async()=>{const browser=await chromium.launch({headless:true});try{
 const page=await browser.newPage({...devices['iPhone X']}),messages=[],errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.addInitScript(()=>Object.defineProperty(navigator,'audioSession',{value:undefined}));
 await page.routeWebSocket('**/api/live',socket=>socket.onMessage(value=>messages.push(JSON.parse(value))));
 await page.goto(base);await page.locator('[name=login]').fill('auditadmin');await page.locator('[name=password]').fill('audit123');await page.locator('#auth-form button[type=submit]').click();await page.locator('#wall').waitFor();
 await page.locator('#preset').click();await page.locator('[data-preset="4"]').click();
 const buttons=page.locator('.tile-audio:visible');assert.ok(await buttons.count()>=2);
 await buttons.nth(0).click();await page.locator('.tile-audio[aria-pressed="true"]').waitFor();assert.equal(await page.locator('.tile-audio[aria-pressed="true"]').count(),1);
 const first=await page.evaluate(()=>audioSelection.cid);assert.ok(messages.some(m=>m.audio_camera_id===first));
 assert.equal(await page.evaluate(()=>{
  const output=cameraAudio.output;
  if(!(output?.element.srcObject instanceof MediaStream) || output.element.paused || cameraAudio.context.state!=='running')return false;
  // Synthetic PCM verifies the browser output graph, not a real camera.
  const packet=new Uint8Array(330);packet[0]=14;packet[1]=2;new DataView(packet.buffer).setFloat64(2,1);packet.fill(0xd5,10);
  cameraAudio.receive(packet,{closed:false,offset:0,video:{paused:false,readyState:4,currentTime:1},element:{classList:{contains:()=>true}}});
  return cameraAudio.samples===320 && output.node.stream.getAudioTracks().length===1;
 }),true);
 await page.evaluate(()=>{window.originalAudioContext=cameraAudio.context;window.originalAudioOutput=cameraAudio.output?.element;});
 // Rebuild the complete wall through the actual preset UI, without a new
 // audio gesture. Preserve the output/context and bind the replacement video.
 await page.locator('#preset').click();await page.locator('[data-preset="9"]').click();
 assert.equal(await page.locator('.tile-audio[aria-pressed="true"]').count(),1);
 assert.equal(await page.evaluate(()=>audioSelection.cid),first);
 assert.equal(await page.evaluate(()=>cameraAudio.enabled && cameraAudio.context===originalAudioContext && cameraAudio.output?.element===originalAudioOutput && audioSelection.element.isConnected),true);
 await page.locator('#preset').click();await page.locator('[data-preset="1"]').click();
 assert.equal(await page.evaluate(()=>audioSelection.cid),first);
 await page.locator('#preset').click();await page.locator('[data-preset="4"]').click();
 await buttons.nth(1).click();assert.equal(await page.locator('.tile-audio[aria-pressed="true"]').count(),1);assert.equal(await buttons.nth(0).getAttribute('aria-pressed'),'false');
 await buttons.nth(1).click();assert.equal(await page.locator('.tile-audio[aria-pressed="true"]').count(),0);assert.equal(await page.evaluate(()=>cameraAudio.enabled),false);
 await page.locator('#preset').click();await page.locator('[data-preset="9"]').click();
 assert.equal(await page.locator('.tile-audio[aria-pressed="true"]').count(),0);
 await buttons.nth(8).click();await page.locator('.tile-audio[aria-pressed="true"]').waitFor();
 await page.locator('#preset').click();await page.locator('[data-preset="1"]').click();
 assert.equal(await page.evaluate(()=>audioSelection),null);assert.equal(await page.evaluate(()=>cameraAudio.enabled),false);
 await buttons.nth(0).click();await page.getByRole('button',{name:'Управление',exact:true}).click();await page.locator('.admin-menu').waitFor();assert.equal(await page.evaluate(()=>audioSelection),null);assert.equal(await page.evaluate(()=>cameraAudio.context),null);assert.equal(await page.locator('body > audio').count(),0);
 assert.deepEqual(errors,[]);console.log('PASS mobile media-stream audio output, one audible tile, grid persistence, removed camera/off stay silent, navigation cleans up');
}finally{await browser.close();}})().catch(e=>{console.error(e);process.exitCode=1;});
