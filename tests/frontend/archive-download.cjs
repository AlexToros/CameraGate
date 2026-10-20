/* Run against fixture.py with UI_ARCHIVE_SAMPLE: real export API/MP4, synthetic recorder. */
const {chromium,devices}=require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const assert=require('node:assert/strict');
(async()=>{const browser=await chromium.launch({headless:true});try{
 const page=await browser.newPage({...devices['iPhone X'],acceptDownloads:true,serviceWorkers:'block'}),errors=[],requests=[],cancelled=[];
 page.on('pageerror',error=>errors.push(error.message));
 await page.routeWebSocket('**/api/live',socket=>socket.onMessage(()=>{}));
 await page.routeWebSocket('**/api/admin/archive/play',socket=>socket.onMessage(()=>{}));
 let polls=0;
 page.on('request',request=>{
   if(request.url().endsWith('/api/admin/archive/exports') && request.method()==='POST')requests.push(request.postDataJSON());
   if(request.url().includes('/api/admin/archive/exports/') && request.method()==='DELETE')cancelled.push(request.url());
   if(request.url().includes('/api/admin/archive/exports/') && request.method()==='GET')polls++;
 });
 await page.goto(process.env.UI_TEST_URL || 'http://127.0.0.1:8083');
 await page.locator('[name=login]').fill('auditadmin');await page.locator('[name=password]').fill('audit123');await page.locator('#auth-form button[type=submit]').click();await page.locator('#wall').waitFor();
 await page.getByRole('button',{name:'Управление',exact:true}).click();await page.locator('.admin-menu').click();await page.locator('a[href="#/admin/archive"]').click();
 await page.locator('.archive-search [name=date]').fill('2026-10-20');await page.locator('.archive-search button').click();await page.locator('.archive-record').click();
 await page.locator('[data-download]').click();await page.locator('.archive-export-form').waitFor();
 const form=page.locator('.archive-export-form');
 assert.equal(await form.locator('[name=start]').inputValue(),'2026-10-20T10:00');
 await form.locator('[name=end]').fill('2026-10-20T10:20');await form.locator('[type=submit]').click();await form.getByText('Можно скачать до 15 минут за один раз').waitFor();assert.equal(requests.length,0);
 await form.locator('[name=start]').fill('2026-10-20T10:00:30');await form.locator('[name=end]').fill('2026-10-20T10:01:30');
 const polled=page.waitForResponse(response=>/\/api\/admin\/archive\/exports\/[^/]+$/.test(response.url()) && response.request().method()==='GET');
 await form.locator('[type=submit]').click();await form.getByText('Готовим фрагмент…').waitFor();assert.equal(await form.locator('[type=submit]').isDisabled(),true);await polled;
 await page.locator('dialog .close').click();await page.waitForTimeout(100);assert.equal(cancelled.length,1);
 await page.locator('[data-download]').click();await page.locator('.archive-export-form [type=submit]').click();
 const link=page.locator('.export-link:visible');await link.waitFor();
 const downloading=page.waitForEvent('download');await link.click();const download=await downloading;assert.match(download.suggestedFilename(),/^camera-1_.*\.mp4$/);
 assert.equal(await download.failure(),null);assert.ok(polls>=1);
 assert.equal(requests[0].start,30);assert.equal(requests[0].end,90);assert.ok(requests[0].id);
 for(const width of [320,390,768,1440]){await page.setViewportSize({width,height:900});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth),width);}
 await page.locator('dialog .close').click();assert.equal(cancelled.length,1);
 assert.deepEqual(errors,[]);console.log('PASS mobile export interval validation, preparing state, cancel, native download attachment, no blob buffering, responsive widths');
}finally{await browser.close();}})().catch(error=>{console.error(error);process.exitCode=1;});
