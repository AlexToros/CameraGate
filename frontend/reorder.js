/* Move existing tiles: no video copies, player recreation or subscriptions. */
function enableWallReorder(wall,move) {
  const events=new AbortController(),options={signal:events.signal};
  let drag=null,frame=0;
  function clean() {
    cancelAnimationFrame(frame);frame=0;
    if(!drag)return;
    const previous=drag;drag=null;
    previous.element.classList.remove('dragging');previous.target?.classList.remove('drop');previous.ghost?.remove();
    if(previous.handle.hasPointerCapture(previous.pointer))previous.handle.releasePointerCapture(previous.pointer);
    document.body.classList.remove('wall-reordering');
  }
  function update() {
    frame=0;if(!drag || !drag.active)return;
    const {x,y}=drag;
    drag.ghost.style.transform=`translate(${x+12}px,${y+12}px)`;
    const edge=48,step=y<edge ? -12 : y>innerHeight-edge ? 12 : 0;
    if(step)window.scrollBy(0,step);
    const target=document.elementFromPoint(x,y)?.closest('.tile');
    const next=target && target!==drag.element && target.parentElement===wall ? target : null;
    if(next!==drag.target){drag.target?.classList.remove('drop');drag.target=next;next?.classList.add('drop');}
    if(step)frame=requestAnimationFrame(update);
  }
  wall.addEventListener('pointerdown',event=>{
    const handle=event.target.closest('.tile-drag');
    if(!handle || event.button!==0 || !event.isPrimary || drag)return;
    event.preventDefault();handle.focus({preventScroll:true});
    drag={handle,element:handle.closest('.tile'),pointer:event.pointerId,x:event.clientX,y:event.clientY,startX:event.clientX,startY:event.clientY,active:false,target:null};
    handle.setPointerCapture(event.pointerId);
  },options);
  wall.addEventListener('pointermove',event=>{
    if(!drag || event.pointerId!==drag.pointer)return;
    drag.x=event.clientX;drag.y=event.clientY;
    if(!drag.active && Math.hypot(drag.x-drag.startX,drag.y-drag.startY)>=6) {
      drag.active=true;drag.element.classList.add('dragging');document.body.classList.add('wall-reordering');
      drag.ghost=document.createElement('div');drag.ghost.className='tile-drag-ghost';drag.ghost.setAttribute('aria-hidden','true');
      drag.ghost.textContent=drag.element.querySelector('.camera-trigger').textContent;document.body.append(drag.ghost);
    }
    if(drag.active && !frame)frame=requestAnimationFrame(update);
  },options);
  wall.addEventListener('pointerup',event=>{
    if(!drag || event.pointerId!==drag.pointer)return;
    if(drag.active){cancelAnimationFrame(frame);update();}
    const source=drag.element,target=drag.target,active=drag.active;
    clean();if(active && target)move(source.dataset.tile,target.dataset.tile);
  },options);
  wall.addEventListener('pointercancel',clean,options);
  wall.addEventListener('lostpointercapture',clean,options);
  document.addEventListener('keydown',event=>{
    if(event.key==='Escape'){clean();return;}
    const handle=event.target.closest?.('.tile-drag');
    if(!handle || !wall.contains(handle) || !event.key.startsWith('Arrow'))return;
    event.preventDefault();
    const tiles=[...wall.querySelectorAll('.tile')],source=handle.closest('.tile'),index=tiles.indexOf(source);
    const columns=parseInt(getComputedStyle(wall).getPropertyValue('--columns')) || 1;
    const delta={ArrowLeft:-1,ArrowRight:1,ArrowUp:-columns,ArrowDown:columns}[event.key];
    const target=tiles[index+delta];if(target)move(source.dataset.tile,target.dataset.tile);
  },options);
  return ()=>{clean();events.abort();};
}
