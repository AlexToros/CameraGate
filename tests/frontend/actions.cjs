const {chromium,devices}=require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const base=process.env.UI_TEST_URL || 'http://127.0.0.1:8082',output=path.resolve(process.env.UI_SCREENSHOTS_DIR || '.runtime/screenshots/mobile-ux-check');
fs.mkdirSync(output,{recursive:true});
(async()=>{
 const browser=await chromium.launch({headless:true});
 try {
  const context=await browser.newContext({...devices['iPhone X'],permissions:['clipboard-read','clipboard-write']}),page=await context.newPage();
  const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.routeWebSocket('**/api/live',socket=>socket.onMessage(()=>{}));
  await page.goto(base);await page.locator('[name=login]').fill('auditadmin');await page.locator('[name=password]').fill('audit123');await page.locator('#auth-form button[type=submit]').click();await page.locator('#wall').waitFor();
  await page.goto(base+'/#/admin/invites/new');await page.locator('[name=last_name]').fill('Тест');await page.locator('[name=first_name]').fill('Копирования');
  await page.locator('#admin-edit-form button[type=submit]').click();
  const link=page.locator('dialog[open] .invite-link');await link.waitFor();
  const url=page.url(),text=await link.textContent();
  await link.click();assert.equal(page.url(),url);assert.equal(await page.evaluate(()=>navigator.clipboard.readText()),text);
  await page.locator('#copy-link').click();assert.equal(await page.evaluate(()=>navigator.clipboard.readText()),text);
  await page.screenshot({path:path.join(output,'22-invite-dialog.png')});
  await page.locator('dialog .close').click();
  const revoke=page.getByRole('button',{name:'Отозвать приглашение: Тест Копирования',exact:true});await revoke.waitFor();
  page.once('dialog',d=>d.dismiss());await revoke.click();assert.equal(await revoke.count(),1);
  page.once('dialog',d=>d.accept());await revoke.click();await revoke.waitFor({state:'detached'});
  assert.ok(page.url().endsWith('#/admin/invites'));
  await page.goto(base+'/#/admin/cameras');await page.locator('[data-rename="31"]').waitFor();
  const button=page.locator('[data-rename="31"]'),original=await button.locator('.object-name').textContent();let writes=0,fail=true;
  await page.route('**/api/admin/cameras',route=>{writes++;return fail ? route.fulfill({status:503,json:{error:'Ошибка сохранения'}}) : route.continue();});
  await button.click();await page.locator('.camera-rename input').fill('Отмена');await page.keyboard.press('Escape');assert.equal(writes,0);assert.equal(await button.locator('.object-name').textContent(),original);
  await button.click();await page.locator('.camera-rename input').fill('Новый вход');await page.keyboard.press('Enter');await page.getByText('Ошибка сохранения',{exact:true}).waitFor();
  assert.equal(await page.locator('.camera-rename input').inputValue(),'Новый вход');assert.equal(writes,1);
  fail=false;await page.locator('.camera-rename button[type=submit]').click();await button.waitFor({state:'visible'});assert.equal(writes,2);assert.equal(await button.locator('.object-name').textContent(),'Новый вход');
  assert.ok(page.url().endsWith('#/admin/cameras'));await page.reload();await button.waitFor();assert.equal(await button.locator('.object-name').textContent(),'Новый вход');
  await button.click();await page.screenshot({path:path.join(output,'23-camera-inline.png')});await page.locator('.camera-rename [data-cancel]').click();
  assert.deepEqual(errors,[]);console.log('PASS invitation dialog, both clipboard actions without navigation, direct revoke with cancel, inline rename cancel/error/retry/persistence');
 } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
