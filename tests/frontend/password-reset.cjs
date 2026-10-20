const {chromium}=require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const assert=require('node:assert/strict');
(async()=>{const browser=await chromium.launch({headless:true,executablePath:process.env.CHROME_EXECUTABLE});try{
 const base=process.env.UI_TEST_URL || 'http://127.0.0.1:8082',admin=await browser.newPage();
 admin.setDefaultTimeout(10000);
 await admin.routeWebSocket('**/api/live',socket=>socket.onMessage(()=>{}));
 await admin.goto(base);await admin.locator('[name=login]').fill('auditadmin');await admin.locator('[name=password]').fill('audit123');await admin.locator('#auth-form button').click();await admin.locator('#wall').waitFor();
 const invitation=await admin.evaluate(async()=>api('/api/admin/invites','POST',{first_name:'Тест',last_name:'Сброс',role:'user',camera_ids:[],group_ids:[]}));
 const userContext=await browser.newContext(),client=userContext.request,login='reset'+Date.now();
 let response=await client.post(base+'/api/auth/register',{data:{token:invitation.token,login,password:'before1'}});assert.equal(response.status(),200);
 for(let i=0;i<3;i++){response=await client.post(base+'/api/auth/login',{data:{login,password:'wrong!'}});assert.equal(response.status(),401);}
 assert.equal((await client.get(base+'/api/me')).status(),401);
 await admin.getByRole('button',{name:'Управление',exact:true}).click();
 await admin.getByRole('button',{name:new RegExp(login)}).click();
 await admin.getByRole('button',{name:'Ссылка для сброса пароля',exact:true}).click();
 await admin.locator('.invite-link').waitFor();const url=await admin.locator('.invite-link').textContent();assert.ok(url.includes('/#/reset/'));
 const page=await userContext.newPage();page.setDefaultTimeout(10000);await page.goto(url);
 assert.equal(await page.locator('[name=login]').count(),0);
 await page.locator('[name=password]').fill('after12');await page.locator('[name=confirmation]').fill('mismatch');await page.locator('form button').click();await page.getByText('Пароли не совпадают',{exact:true}).waitFor();
 await page.locator('[name=confirmation]').fill('after12');await page.locator('form button').click();await page.locator('[name=login]').waitFor();
 response=await client.post(base+'/api/auth/login',{data:{login,password:'after12'}});assert.equal(response.status(),200);
 const token=url.split('/#/reset/')[1];response=await client.post(base+'/api/auth/reset',{data:{token,password:'after13'}});assert.equal(response.status(),410);
 console.log('PASS lockout, revoked sessions, admin copy-link dialog, reset form confirmation, new password login, single-use token');
}finally{await browser.close();}})().catch(error=>{console.error(error);process.exitCode=1;});
