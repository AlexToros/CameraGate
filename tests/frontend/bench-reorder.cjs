/* DOM-only benchmark: no recorder, database or portal requests. */
const {chromium}=require(process.env.PLAYWRIGHT_MODULE || 'playwright');
(async()=>{const browser=await chromium.launch({headless:true});try{
 const page=await browser.newPage({viewport:{width:1200,height:900},reducedMotion:'reduce'});
 await page.setContent('<style>.wall{display:grid;grid-template-columns:repeat(8,100px);gap:4px;--columns:8}.tile{height:60px}</style><div class="wall"></div>');await page.addScriptTag({path:'frontend/reorder.js'});
 for(const count of [4,16,64])console.log(await page.evaluate(count=>{
  const wall=document.querySelector('.wall');wall.innerHTML=Array.from({length:count},(_,i)=>`<article class="tile" data-tile="${i}"><button class="tile-drag">${i}</button></article>`).join('');
  let commits=0;const stop=enableWallReorder(wall,()=>commits++),handle=wall.querySelector('button'),start=performance.now();
  for(let i=0;i<200;i++)handle.dispatchEvent(new KeyboardEvent('keydown',{key:i%2 ? 'ArrowLeft' : 'ArrowRight',bubbles:true}));
  const elapsed=performance.now()-start;stop();if(commits!==200)throw new Error('Reorder did not commit');return{tiles:count,iterations:200,msPerMove:+(elapsed/200).toFixed(3)};
 },count));
}finally{await browser.close();}})().catch(e=>{console.error(e);process.exitCode=1;});
