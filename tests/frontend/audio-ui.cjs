const {chromium,devices}=require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const assert=require('node:assert/strict');
const base=process.env.UI_TEST_URL || 'http://127.0.0.1:8082';
(async()=>{const browser=await chromium.launch({headless:true});try{
 const page=await browser.newPage({...devices['iPhone X']}),messages=[],errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.routeWebSocket('**/api/live',socket=>socket.onMessage(value=>messages.push(JSON.parse(value))));
 await page.goto(base);await page.locator('[name=login]').fill('auditadmin');await page.locator('[name=password]').fill('audit123');await page.locator('#auth-form button[type=submit]').click();await page.locator('#wall').waitFor();
 await page.locator('#preset').click();await page.locator('[data-preset="4"]').click();
 const buttons=page.locator('.tile-audio:visible');assert.ok(await buttons.count()>=2);
 await buttons.nth(0).click();await page.locator('.tile-audio[aria-pressed="true"]').waitFor();assert.equal(await page.locator('.tile-audio[aria-pressed="true"]').count(),1);
 const first=await page.evaluate(()=>audioSelection.cid);assert.ok(messages.some(m=>m.audio_camera_id===first));
 await buttons.nth(1).click();assert.equal(await page.locator('.tile-audio[aria-pressed="true"]').count(),1);assert.equal(await buttons.nth(0).getAttribute('aria-pressed'),'false');
 await buttons.nth(1).click();assert.equal(await page.locator('.tile-audio[aria-pressed="true"]').count(),0);assert.equal(await page.evaluate(()=>cameraAudio.enabled),false);
 await buttons.nth(0).click();await page.getByRole('button',{name:'Управление',exact:true}).click();await page.locator('.admin-menu').waitFor();assert.equal(await page.evaluate(()=>audioSelection),null);assert.equal(await page.evaluate(()=>cameraAudio.context),null);
 assert.deepEqual(errors,[]);console.log('PASS mobile audio toggle, only one audible tile, opt-in subscription, off, navigation closes audio context');
}finally{await browser.close();}})().catch(e=>{console.error(e);process.exitCode=1;});
