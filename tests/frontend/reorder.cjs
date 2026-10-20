const {chromium,devices}=require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const assert=require('node:assert/strict');
(async()=>{const browser=await chromium.launch({headless:true});try{
 for(const mobile of [false,true]){
  const context=await browser.newContext(mobile ? {...devices['iPhone X']} : {viewport:{width:1200,height:900}}),page=await context.newPage(),errors=[];let subscriptions=0;
  page.on('pageerror',e=>errors.push(e.message));await page.routeWebSocket('**/api/live',socket=>socket.onMessage(()=>subscriptions++));
  await page.goto(process.env.UI_TEST_URL || 'http://127.0.0.1:8082');await page.locator('[name=login]').fill('auditadmin');await page.locator('[name=password]').fill('audit123');await page.locator('#auth-form button[type=submit]').click();await page.locator('#wall').waitFor();
  await page.locator('#preset').click();await page.locator('[data-preset="4"]').click();await page.locator('.tile').nth(3).waitFor();
  const before=await page.locator('.tile').evaluateAll(tiles=>tiles.map(tile=>tile.dataset.tile));
  await page.evaluate(()=>{window.testVideos=[...document.querySelectorAll('.tile video')];});
  const handle=page.locator('.tile-drag').first(),target=page.locator('.tile').nth(2),from=await handle.boundingBox(),to=await target.boundingBox();
  const saved=page.waitForResponse(r=>r.url().endsWith('/api/layout') && r.request().method()==='PUT' && r.request().postDataJSON().tiles[2]?.id===before[0]);
  const initialSubscriptions=subscriptions;
  if(mobile){
    const session=await context.newCDPSession(page),start={x:from.x+from.width/2,y:from.y+from.height/2},end={x:to.x+to.width/2,y:to.y+to.height/2};
    await session.send('Input.dispatchTouchEvent',{type:'touchStart',touchPoints:[start]});
    for(let step=1;step<=8;step++){await session.send('Input.dispatchTouchEvent',{type:'touchMove',touchPoints:[{x:start.x+(end.x-start.x)*step/8,y:start.y+(end.y-start.y)*step/8}]});await page.waitForTimeout(25);}
    await session.send('Input.dispatchTouchEvent',{type:'touchEnd',touchPoints:[]});
  } else {
    await page.mouse.move(from.x+from.width/2,from.y+from.height/2);await page.mouse.down();await page.mouse.move(to.x+to.width/2,to.y+to.height/2,{steps:10});await page.waitForTimeout(60);await page.mouse.up();
  }
  assert.equal((await saved).status(),200);
  assert.deepEqual(await page.locator('.tile').evaluateAll(tiles=>tiles.map(t=>t.dataset.tile)),[before[1],before[2],before[0],before[3]]);
  assert.equal(await page.evaluate(()=>testVideos.every(video=>video.isConnected && [...document.querySelectorAll('video')].includes(video))),true);
  assert.equal(subscriptions,initialSubscriptions);assert.equal(await page.locator('dialog[open]').count(),0);assert.equal(await page.locator('.tile-drag-ghost').count(),0);
  await page.reload();await page.locator('.tile').nth(3).waitFor();assert.equal(await page.locator('.tile').nth(2).getAttribute('data-tile'),before[0]);
  await page.locator('.tile-drag').nth(2).focus();await page.keyboard.press('ArrowLeft');assert.equal(await page.locator('.tile').nth(1).getAttribute('data-tile'),before[0]);
  assert.deepEqual(errors,[]);await context.close();
 }
 console.log('PASS mouse/touch tile dragging, keyboard reordering, persistence; existing videos retained, no extra subscriptions or camera picker');
}finally{await browser.close();}})().catch(e=>{console.error(e);process.exitCode=1;});
