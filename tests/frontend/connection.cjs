const {chromium,devices}=require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const base=process.env.UI_TEST_URL || 'http://127.0.0.1:8082';
const output=path.resolve(process.env.UI_SCREENSHOTS_DIR || '.runtime/screenshots/mobile-ux-check');
fs.mkdirSync(output,{recursive:true});
(async()=>{
 const browser=await chromium.launch({headless:true});
 try {
  const page=await browser.newPage({...devices['iPhone X']}),errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  await page.routeWebSocket('**/api/live',socket=>socket.onMessage(()=>{}));
  await page.goto(base);await page.locator('[name=login]').fill('auditadmin');await page.locator('[name=password]').fill('audit123');await page.locator('#auth-form button[type=submit]').click();await page.locator('#wall').waitFor();
  let response={configured:true,status:'checking',device:null};
  await page.route('**/api/admin/connection',route=>route.fulfill({json:response}));
  await page.goto(base+'/#/admin/connection');
  await page.getByText('Проверяем подключение к регистратору…',{exact:true}).waitFor();
  assert.equal(await page.locator('[name=ID]').inputValue(),'');
  assert.equal(await page.locator('[name=Password]').inputValue(),'');
  await page.locator('[name=ID]').fill('draft-id');
  response={configured:true,status:'connected',device:{name:'Дом <34>',channel_count:36}};
  await page.getByText('Подключён регистратор «Дом <34>»',{exact:true}).waitFor();
  assert.equal(await page.locator('.connection-status b').count(),0);
  assert.equal(await page.locator('[name=ID]').inputValue(),'draft-id');
  await page.getByText('36 каналов',{exact:true}).waitFor();
  await page.screenshot({path:path.join(output,'20-connection-name.png')});
  await page.locator('[name=ID]').fill('');
  response={configured:true,status:'error',device:{name:'Дом',channel_count:36}};
  await page.reload();await page.getByText('Не удалось подключиться к регистратору',{exact:true}).waitFor();
  response={configured:true,status:'connected',device:{name:null,channel_count:36}};
  await page.reload();await page.getByText('Подключён регистратор',{exact:true}).waitFor();
  await page.screenshot({path:path.join(output,'21-connection-unnamed.png')});
  response={configured:false,status:'not_configured',device:null};
  await page.reload();await page.getByText('Регистратор не подключён',{exact:true}).waitFor();
  await page.getByText('Введите Cloud ID, логин и пароль регистратора.',{exact:true}).waitFor();
  assert.deepEqual(errors,[]);
  console.log('PASS recorder name/count, unnamed/offline/unconfigured states, async status preserves draft, escaped device name, empty credential fields');
 } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
