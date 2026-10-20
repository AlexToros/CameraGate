const {chromium,devices}=require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const assert=require('node:assert/strict'),fs=require('node:fs');
(async()=>{const browser=await chromium.launch({headless:true});try{
 for(const mobile of [false,true])for(const grip of [true,false]){
  const context=await browser.newContext(mobile ? {...devices['iPhone X']} : {viewport:{width:1200,height:900}}),page=await context.newPage(),errors=[];let subscriptions=0,writes=0;
  if(mobile)await context.addInitScript(()=>Object.defineProperty(navigator,'standalone',{value:true}));
  page.on('pageerror',e=>errors.push(e.message));page.on('request',r=>{if(r.url().endsWith('/api/layout') && r.method()==='PUT')writes++;});
  await page.routeWebSocket('**/api/live',socket=>socket.onMessage(()=>subscriptions++));
  await page.goto(process.env.UI_TEST_URL || 'http://127.0.0.1:8082');await page.locator('[name=login]').fill('auditadmin');await page.locator('[name=password]').fill('audit123');await page.locator('#auth-form button[type=submit]').click();await page.locator('#wall').waitFor();
  await page.locator('#preset').click();const presetSaved=page.waitForResponse(r=>r.url().endsWith('/api/layout') && r.request().method()==='PUT');await page.locator('[data-preset="4"]').click();await presetSaved;
  const before=await page.locator('.tile').evaluateAll(tiles=>tiles.map(tile=>tile.dataset.tile)),expected=[before[1],before[2],before[0],before[3]];
  await page.evaluate(()=>{window.testVideos=[...document.querySelectorAll('.tile video')];});
  const session=mobile ? await context.newCDPSession(page) : null;
  async function start(selector){const r=await page.locator(selector).first().boundingBox(),point={x:r.x+r.width/2,y:r.y+r.height/2};if(mobile)await session.send('Input.dispatchTouchEvent',{type:'touchStart',touchPoints:[point]});else{await page.mouse.move(point.x,point.y);await page.mouse.down();}return point;}
  async function move(from,to){if(mobile){for(let step=1;step<=8;step++){await session.send('Input.dispatchTouchEvent',{type:'touchMove',touchPoints:[{x:from.x+(to.x-from.x)*step/8,y:from.y+(to.y-from.y)*step/8}]});await page.waitForTimeout(25);}}else await page.mouse.move(to.x,to.y,{steps:10});await page.waitForTimeout(60);}
  async function end(){if(mobile)await session.send('Input.dispatchTouchEvent',{type:'touchEnd',touchPoints:[]});else await page.mouse.up();}
  // A functional button retains its normal action and never lifts the tile.
  await page.locator('.camera-trigger').first().click();await page.locator('dialog[open]').waitFor();assert.equal(await page.locator('.tile.dragging').count(),0);await page.locator('dialog .close').click();
  if(mobile && !grip){
    const point=await start('.picture');assert.equal(await page.locator('.tile.dragging').count(),0);
    await session.send('Input.dispatchTouchEvent',{type:'touchMove',touchPoints:[{x:point.x,y:point.y+20}]});await end();
    await page.waitForTimeout(400);assert.equal(await page.locator('.tile-placeholder').count(),0);assert.deepEqual(await page.locator('.tile').evaluateAll(nodes=>nodes.map(n=>n.dataset.tile)),before);
  }
  const initialSubscriptions=subscriptions,initialWrites=writes,from=await start(grip ? '.tile-drag' : '.picture');
  if(!mobile && !grip)await page.mouse.move(from.x+8,from.y,{steps:2});
  if(mobile && !grip){await page.waitForTimeout(100);assert.equal(await page.locator('.tile.dragging').count(),0);}
  await page.locator('.tile.dragging').waitFor();
  const target=await page.locator('.tile:not(.dragging)').nth(1).boundingBox(),to={x:target.x+target.width/2,y:target.y+target.height/2};
  await move(from,to);
  const preview=await page.locator('#wall').evaluate(wall=>[...wall.children].filter(n=>!n.classList.contains('dragging')).map(n=>n.classList.contains('tile-placeholder') ? document.querySelector('.dragging').dataset.tile : n.dataset.tile));
  assert.deepEqual(preview,expected);assert.equal(writes,initialWrites); // preview precedes release and persistence
  assert.deepEqual(await page.evaluate(()=>state.layout.tiles.map(t=>t.id)),before);
  if(process.env.UI_SCREENSHOTS_DIR){fs.mkdirSync(process.env.UI_SCREENSHOTS_DIR,{recursive:true});await page.screenshot({path:`${process.env.UI_SCREENSHOTS_DIR}/drag-${mobile?'touch':'mouse'}-${grip?'grip':'picture'}.png`});}
  const saved=page.waitForResponse(r=>r.url().endsWith('/api/layout') && r.request().method()==='PUT' && r.request().postDataJSON().tiles[2]?.id===before[0]);
  await end();assert.equal((await saved).status(),200);
  assert.deepEqual(await page.locator('.tile').evaluateAll(nodes=>nodes.map(n=>n.dataset.tile)),expected);
  assert.equal(await page.evaluate(()=>testVideos.every(video=>video.isConnected && [...document.querySelectorAll('video')].includes(video))),true);
  assert.equal(subscriptions,initialSubscriptions);assert.equal(await page.locator('dialog[open]').count(),0);assert.equal(await page.locator('.tile-placeholder').count(),0);
  // Escape restores both the preview and saved order without another write.
  await page.waitForTimeout(200);const cancelFrom=await start('.tile-drag'),cancelTo=await page.locator('.tile:not(.dragging)').last().boundingBox();await move(cancelFrom,{x:cancelTo.x+cancelTo.width/2,y:cancelTo.y+cancelTo.height/2});await page.keyboard.press('Escape');await end();
  assert.deepEqual(await page.locator('.tile').evaluateAll(nodes=>nodes.map(n=>n.dataset.tile)),expected);
  await page.reload();await page.locator('.tile').nth(3).waitFor();assert.equal(await page.locator('.tile').nth(2).getAttribute('data-tile'),before[0]);
  await page.locator('.tile-drag').nth(2).focus();await page.keyboard.press('ArrowLeft');assert.equal(await page.locator('.tile').nth(1).getAttribute('data-tile'),before[0]);
  assert.deepEqual(errors,[]);await context.close();
 }
 console.log('PASS mouse whole-tile/grip drag; immediate touch grip, delayed whole-tile touch, scroll cancellation; live preview before release, Escape rollback, buttons, persistence, keyboard; videos retained, no extra subscriptions');
}finally{await browser.close();}})().catch(e=>{console.error(e);process.exitCode=1;});
