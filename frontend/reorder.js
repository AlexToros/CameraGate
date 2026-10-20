/* Reorder existing tiles; the lifted tile keeps its video and decoder. */
function enableWallReorder(wall,commit) {
  const events=new AbortController(),signal=events.signal,interactive='button,a,input,select,textarea,[role="button"],[contenteditable="true"]';
  const reduced=matchMedia('(prefers-reduced-motion: reduce)').matches,animations=new Map();
  let drag=null,frame=0,suppressUntil=0;
  const tiles=()=>[...wall.children].filter(node=>node.matches('.tile') && node!==drag?.element);
  const order=()=>[...wall.children].filter(node=>node.matches('.tile')).map(node=>node.dataset.tile);
  function relocate(node,reference){if(wall.moveBefore)wall.moveBefore(node,reference);else wall.insertBefore(node,reference);}
  function measure(){return new Map([...wall.children].filter(node=>node!==drag?.element).map(node=>[node,node.getBoundingClientRect()]));}
  function animateChange(change) {
    const before=measure();
    for(const animation of animations.values())animation.cancel();animations.clear();
    change();const after=measure();
    if(drag){drag.boxes=after;drag.scroll=scrollY;}
    if(!reduced)for(const [node,rect] of after){
      const old=before.get(node);if(!old || node===drag?.placeholder)continue;
      const x=old.left-rect.left,y=old.top-rect.top;
      if(Math.abs(x)+Math.abs(y)<1)continue;
      const animation=node.animate([{transform:`translate(${x}px,${y}px)`},{transform:'translate(0,0)'}],{duration:180,easing:'ease-out'});
      animations.set(node,animation);animation.onfinish=()=>{if(animations.get(node)===animation)animations.delete(node);};
    }
  }
  function activate() {
    if(!drag || drag.active)return;
    clearTimeout(drag.timer);drag.active=true;
    const rect=drag.element.getBoundingClientRect();drag.style=drag.element.style.cssText;
    drag.placeholder=document.createElement('div');drag.placeholder.className='tile-placeholder';drag.placeholder.setAttribute('aria-hidden','true');
    drag.placeholder.style.height=`${rect.height}px`;drag.placeholder.style.gridColumn=drag.element.style.gridColumn || `span ${getComputedStyle(drag.element).getPropertyValue('--width') || 1}`;
    drag.placeholder.style.gridRowEnd=drag.element.style.gridRowEnd;drag.placeholder.textContent=drag.element.querySelector('.camera-trigger').textContent;
    wall.insertBefore(drag.placeholder,drag.element);
    Object.assign(drag.element.style,{position:'fixed',left:`${rect.left}px`,top:`${rect.top}px`,width:`${rect.width}px`,height:`${rect.height}px`});
    drag.element.classList.add('dragging');document.body.classList.add('wall-reordering');
    drag.boxes=measure();drag.scroll=scrollY;
    if(drag.pointer!==null)drag.element.setPointerCapture(drag.pointer);
    schedule();
  }
  function update() {
    frame=0;if(!drag?.active)return;
    const {x,y,startX,startY}=drag;
    drag.element.style.transform=`translate3d(${x-startX}px,${y-startY}px,0) scale(1.025)`;
    const edge=48,step=y<edge ? -12 : y>innerHeight-edge ? 12 : 0;
    if(step)window.scrollBy(0,step);
    const scroll=scrollY-drag.scroll;
    let target=null;
    for(const [node,rect] of drag.boxes){
      if(node===drag.placeholder)continue;
      if(x>=rect.left && x<=rect.right && y>=rect.top-scroll && y<=rect.bottom-scroll){target=node;break;}
    }
    if(target){
      const children=[...wall.children].filter(node=>node!==drag.element),from=children.indexOf(drag.placeholder),to=children.indexOf(target);
      animateChange(()=>relocate(drag.placeholder,from<to ? target.nextElementSibling : target));
    }
    if(step)schedule();
  }
  function schedule(){if(!frame)frame=requestAnimationFrame(update);}
  function finish(save=false) {
    cancelAnimationFrame(frame);frame=0;if(!drag || drag.finishing)return;
    drag.finishing=true;
    const previous=drag;clearTimeout(previous.timer);
    if(previous.active){
      const from=previous.element.getBoundingClientRect();
      animateChange(()=>{
        if(save)relocate(previous.element,previous.placeholder);
        previous.placeholder.remove();previous.element.style.cssText=previous.style;previous.element.classList.remove('dragging');
      });
      const to=previous.element.getBoundingClientRect();
      if(save && !reduced)previous.element.animate([{transform:`translate(${from.left-to.left}px,${from.top-to.top}px) scale(1.025)`},{transform:'translate(0,0) scale(1)'}],{duration:180,easing:'ease-out'});
      document.body.classList.remove('wall-reordering');suppressUntil=Date.now()+400;
    }
    drag=null;
    if(previous.pointer!==null && previous.element.hasPointerCapture(previous.pointer))previous.element.releasePointerCapture(previous.pointer);
    if(save && previous.active){const ids=order();if(ids.some((id,i)=>id!==previous.order[i]))commit(ids);}
  }
  function begin(target,x,y,pointer,touch) {
    if(drag)return false;
    const element=target.closest('.tile'),grip=target.closest('.tile-drag');
    if(!element || element.parentElement!==wall || (!grip && target.closest(interactive)))return false;
    suppressUntil=0;
    drag={element,pointer,touch,x,y,startX:x,startY:y,active:false,order:order()};
    if(grip)activate();else if(touch!==null)drag.timer=setTimeout(activate,350);
    return true;
  }
  wall.addEventListener('pointerdown',event=>{
    if(event.pointerType==='touch' || event.button!==0 || !event.isPrimary)return;
    if(begin(event.target,event.clientX,event.clientY,event.pointerId,null)){event.preventDefault();}
  },{signal});
  document.addEventListener('pointermove',event=>{
    if(!drag || drag.pointer!==event.pointerId)return;
    drag.x=event.clientX;drag.y=event.clientY;
    if(!drag.active && Math.hypot(drag.x-drag.startX,drag.y-drag.startY)>=6)activate();
    if(drag.active)schedule();
  },{signal});
  document.addEventListener('pointerup',event=>{if(drag?.pointer===event.pointerId){if(drag.active)update();finish(true);}},{signal});
  document.addEventListener('pointercancel',event=>{if(drag?.pointer===event.pointerId)finish();},{signal});
  wall.addEventListener('lostpointercapture',event=>{if(drag?.pointer===event.pointerId)finish();},{signal});
  wall.addEventListener('touchstart',event=>{
    if(event.touches.length!==1){finish();return;}
    const touch=event.changedTouches[0];
    if(begin(event.target,touch.clientX,touch.clientY,null,touch.identifier) && drag.active)event.preventDefault();
  },{signal,passive:false});
  document.addEventListener('touchmove',event=>{
    if(!drag || drag.touch===null)return;
    const touch=[...event.touches].find(item=>item.identifier===drag.touch);if(!touch)return;
    drag.x=touch.clientX;drag.y=touch.clientY;
    if(!drag.active){if(Math.hypot(drag.x-drag.startX,drag.y-drag.startY)>10)finish();return;}
    event.preventDefault();schedule();
  },{signal,passive:false});
  document.addEventListener('touchend',event=>{
    if(!drag || ![...event.changedTouches].some(item=>item.identifier===drag.touch))return;
    if(drag.active){event.preventDefault();update();}finish(true);
  },{signal,passive:false});
  document.addEventListener('touchcancel',()=>finish(),{signal});
  wall.addEventListener('contextmenu',event=>{if(drag)event.preventDefault();},{signal});
  wall.addEventListener('dragstart',event=>{if(event.target.closest('.tile'))event.preventDefault();},{signal});
  wall.addEventListener('click',event=>{if(Date.now()<suppressUntil){event.preventDefault();event.stopImmediatePropagation();}},{signal,capture:true});
  document.addEventListener('keydown',event=>{
    if(event.key==='Escape'){finish();return;}
    const handle=event.target.closest?.('.tile-drag');
    if(!handle || !wall.contains(handle) || !event.key.startsWith('Arrow'))return;
    event.preventDefault();finish();
    const elements=tiles(),source=handle.closest('.tile'),index=elements.indexOf(source),columns=parseInt(getComputedStyle(wall).getPropertyValue('--columns')) || 1;
    const delta={ArrowLeft:-1,ArrowRight:1,ArrowUp:-columns,ArrowDown:columns}[event.key],target=elements[index+delta];
    if(target){animateChange(()=>relocate(source,delta>0 ? target.nextElementSibling : target));commit(order());}
  },{signal});
  return ()=>{finish();events.abort();for(const animation of animations.values())animation.cancel();animations.clear();};
}
