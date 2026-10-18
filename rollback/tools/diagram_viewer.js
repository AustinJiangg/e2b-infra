// 构建时内嵌到 HTML。使用原 SVG 节点，保留矢量、样式和内部引用。
(function(){
  var viewer=document.getElementById('diagram-viewer'),viewport=document.getElementById('diagram-viewport');
  var canvas=document.getElementById('diagram-canvas'),caption=document.getElementById('diagram-title');
  var readout=document.getElementById('diagram-scale');
  var zoomOut=document.getElementById('diagram-zoom-out'),zoomIn=document.getElementById('diagram-zoom-in');
  var current=null,scale=1,fitScale=1,x=0,y=0,bounds=null,animation=null,scaleTimer=null;
  var points=new Map(),gesture=null,ignoreClickUntil=0;

  function reducedMotion(){return window.matchMedia&&window.matchMedia('(prefers-reduced-motion: reduce)').matches;}
  function transform(){return 'translate('+x+'px,'+y+'px) scale('+scale+')';}
  function stopAnimation(){
    if(animation){animation.cancel();animation=null;
      if(current&&!current.closing&&viewer.dataset.phase==='opening') viewer.dataset.phase='open';}
  }
  function moveFrom(from,done){
    stopAnimation();
    if(!canvas.animate||reducedMotion()||from===canvas.style.transform){if(done) done();return;}
    var active=canvas.animate([{transform:from},{transform:canvas.style.transform}],
      {duration:200,easing:'cubic-bezier(.2,.7,.2,1)'});
    animation=active;
    active.finished.then(function(){if(animation===active){animation=null;if(done) done();}},function(){});
  }
  function clamp(){
    var w=current.width*scale,h=current.height*scale;
    x=w<=bounds.width?bounds.left+(bounds.width-w)/2:Math.max(bounds.right-w,Math.min(bounds.left,x));
    y=h<=bounds.height?bounds.top+(bounds.height-h)/2:Math.max(bounds.bottom-h,Math.min(bounds.top,y));
  }
  function paint(){
    clamp();canvas.style.transform=transform();
    viewer.classList.toggle('is-zoomed',scale>fitScale*1.001);
    readout.textContent=Math.round(scale/fitScale*100)+'%';
    zoomOut.disabled=scale<=fitScale;zoomIn.disabled=scale>=fitScale*8;
    caption.style.top=Math.min(viewport.clientHeight-112,y+current.height*scale+12)+'px';
  }
  function showScale(){
    viewer.classList.add('show-scale');clearTimeout(scaleTimer);
    scaleTimer=setTimeout(function(){viewer.classList.remove('show-scale');},900);
  }
  function fit(animate){
    if(!current||current.closing) return;
    var from=getComputedStyle(canvas).transform;
    var w=viewport.clientWidth,h=viewport.clientHeight,margin=w<600?16:48;
    bounds={left:margin,top:64,right:w-margin,bottom:Math.max(96,h-112)};
    bounds.width=Math.max(1,bounds.right-bounds.left);bounds.height=Math.max(1,bounds.bottom-bounds.top);
    fitScale=Math.max(.001,Math.min(bounds.width/current.width,bounds.height/current.height));
    scale=fitScale;x=bounds.left;y=bounds.top;paint();
    if(animate){moveFrom(from);showScale();}
  }
  function zoom(next,cx,cy,animate){
    if(!current||current.closing) return;
    var from=getComputedStyle(canvas).transform;stopAnimation();
    next=Math.max(fitScale,Math.min(fitScale*8,next));
    x=cx-(cx-x)*next/scale;y=cy-(cy-y)*next/scale;scale=next;paint();showScale();
    if(animate) moveFrom(from);
  }
  function centerZoom(next){zoom(next,(bounds.left+bounds.right)/2,(bounds.top+bounds.bottom)/2,true);}
  function detailZoom(cx,cy){
    var next=scale>fitScale*1.01?fitScale:Math.min(fitScale*8,Math.max(1,fitScale*2.5));
    zoom(next,cx,cy,true);
  }
  function originalTransform(){
    var rect=current.placeholder.getBoundingClientRect(),view=viewport.getBoundingClientRect();
    return 'translate('+(rect.left-view.left)+'px,'+(rect.top-view.top)+'px) scale('+(rect.width/current.width)+')';
  }
  function openDiagram(trigger){
    if(current) return;
    var svg=trigger.querySelector('svg');if(!svg) return;
    var box=(svg.getAttribute('viewBox')||'').trim().split(/[\s,]+/).map(Number),rect=svg.getBoundingClientRect();
    var width=(box.length===4&&box[2]>0?box[2]:parseFloat(svg.getAttribute('width')))||rect.width;
    var height=(box.length===4&&box[3]>0?box[3]:parseFloat(svg.getAttribute('height')))||rect.height;
    if(!(width>0&&height>0)) return;
    var placeholder=document.createElement('span');
    placeholder.style.cssText='display:block;margin:auto;max-width:100%;width:'+rect.width+'px;height:'+rect.height+'px';
    current={svg:svg,placeholder:placeholder,trigger:trigger,width:width,height:height,closing:false,
      style:svg.getAttribute('style'),overflow:document.body.style.overflow,padding:document.body.style.paddingRight};
    ignoreClickUntil=0;caption.textContent=trigger.dataset.diagramTitle;caption.title=caption.textContent;
    svg.replaceWith(placeholder);canvas.appendChild(svg);
    svg.style.width=width+'px';svg.style.height=height+'px';svg.style.maxWidth='none';svg.style.margin='0';
    canvas.style.width=width+'px';canvas.style.height=height+'px';
    var scrollbar=window.innerWidth-document.documentElement.clientWidth;
    if(scrollbar>0) document.body.style.paddingRight=(parseFloat(getComputedStyle(document.body).paddingRight)||0)+scrollbar+'px';
    document.body.style.overflow='hidden';viewer.showModal();fit(false);
    viewer.dataset.phase='opening';
    moveFrom(originalTransform(),function(){if(current&&!current.closing) viewer.dataset.phase='open';});
    requestAnimationFrame(function(){if(current&&!current.closing) viewer.classList.add('is-visible');});
    window.addEventListener('wheel',wheelDiagram,{passive:false,capture:true});
    viewer.focus({preventScroll:true});
  }
  function restoreDiagram(){
    if(!current) return;
    stopAnimation();clearTimeout(scaleTimer);
    var old=current;current=null;points.forEach(function(_,id){
      if(canvas.hasPointerCapture&&canvas.hasPointerCapture(id)) canvas.releasePointerCapture(id);
    });
    points.clear();gesture=null;ignoreClickUntil=0;
    window.removeEventListener('wheel',wheelDiagram,true);
    viewer.classList.remove('is-visible','is-zoomed','is-dragging','show-scale');viewer.dataset.phase='closed';
    old.placeholder.replaceWith(old.svg);
    if(old.style===null) old.svg.removeAttribute('style');else old.svg.setAttribute('style',old.style);
    document.body.style.overflow=old.overflow;document.body.style.paddingRight=old.padding;
    old.trigger.focus({preventScroll:true});
  }
  function closeDiagram(immediate){
    if(!current||(current.closing&&!immediate)) return;
    current.closing=true;viewer.dataset.phase='closing';viewer.classList.remove('is-visible','show-scale');
    var from=getComputedStyle(canvas).transform;
    canvas.style.transform=originalTransform();
    if(immediate){stopAnimation();viewer.close();restoreDiagram();}
    else moveFrom(from,function(){viewer.close();});
  }
  document.querySelectorAll('main figure,main pre.mermaid').forEach(function(trigger){
    if(!trigger.matches('pre.mermaid')&&!trigger.querySelector('svg')) return;
    var title=trigger.querySelector('figcaption'),section=trigger.closest('section'),heading='流程图';
    if(!title&&section){
      var found=false;
      section.querySelectorAll('h1,h2,h3,h4,pre.mermaid').forEach(function(node){
        if(node===trigger) found=true;else if(!found&&/^H[1-4]$/.test(node.tagName)) heading=node.textContent.trim();
      });
    }
    trigger.dataset.diagramTitle=title?title.textContent.trim():heading;
    trigger.classList.add('diagram-trigger');trigger.tabIndex=0;trigger.setAttribute('role','button');
    trigger.setAttribute('aria-haspopup','dialog');trigger.setAttribute('aria-controls','diagram-viewer');
    trigger.setAttribute('aria-label',trigger.dataset.diagramTitle+'，点击放大');trigger.title='点击放大';
    trigger.addEventListener('click',function(e){if(!e.target.closest('a')) openDiagram(trigger);});
    trigger.addEventListener('keydown',function(e){
      if(e.target===trigger&&(e.key==='Enter'||e.key===' ')){e.preventDefault();openDiagram(trigger);}
    });
  });
  document.getElementById('diagram-close').addEventListener('click',function(){closeDiagram(false);});
  viewer.addEventListener('cancel',function(e){e.preventDefault();closeDiagram(false);});
  viewer.addEventListener('close',function(){if(!viewer.open) restoreDiagram();});
  viewer.addEventListener('click',function(e){
    if(performance.now()<ignoreClickUntil||canvas.contains(e.target)||e.target.closest('button,.diagram-toolbar')) return;
    closeDiagram(false);
  });
  zoomOut.addEventListener('click',function(){centerZoom(scale/1.4);});
  zoomIn.addEventListener('click',function(){centerZoom(scale*1.4);});
  document.getElementById('diagram-fit').addEventListener('click',function(){fit(true);});
  viewport.addEventListener('dblclick',function(e){
    if(!current||!canvas.contains(e.target)) return;
    e.preventDefault();var rect=viewport.getBoundingClientRect();detailZoom(e.clientX-rect.left,e.clientY-rect.top);
  });
  function wheelDiagram(e){
    if(!current||current.closing||!viewport.contains(e.target)) return;
    e.preventDefault();var rect=viewport.getBoundingClientRect();
    var delta=e.deltaY*(e.deltaMode===1?16:e.deltaMode===2?viewport.clientHeight:1);
    zoom(scale*Math.exp(-Math.max(-200,Math.min(200,delta))*.002),e.clientX-rect.left,e.clientY-rect.top,false);
  }
  function startGesture(){
    var p=Array.from(points.values()),rect=viewport.getBoundingClientRect();
    if(p.length>=2){
      var cx=(p[0].x+p[1].x)/2-rect.left,cy=(p[0].y+p[1].y)/2-rect.top;
      gesture={type:'pinch',distance:Math.max(1,Math.hypot(p[0].x-p[1].x,p[0].y-p[1].y)),scale:scale,
        anchorX:(cx-x)/scale,anchorY:(cy-y)/scale};
      ignoreClickUntil=performance.now()+400;
    }else if(p.length){gesture={type:'pan',x:p[0].x,y:p[0].y,startX:p[0].x,startY:p[0].y};}
    else gesture=null;
    viewer.classList.toggle('is-dragging',points.size>0&&scale>fitScale*1.001);
  }
  viewport.addEventListener('pointerdown',function(e){
    if(!current||current.closing||e.button!==0||(!canvas.contains(e.target)&&!points.size)) return;
    e.preventDefault();stopAnimation();points.set(e.pointerId,{x:e.clientX,y:e.clientY});
    canvas.setPointerCapture(e.pointerId);startGesture();
  });
  viewport.addEventListener('pointermove',function(e){
    if(!current||!points.has(e.pointerId)) return;
    points.set(e.pointerId,{x:e.clientX,y:e.clientY});
    if(gesture.type==='pinch'&&points.size>=2){
      var p=Array.from(points.values()),rect=viewport.getBoundingClientRect();
      var distance=Math.hypot(p[0].x-p[1].x,p[0].y-p[1].y);
      scale=Math.max(fitScale,Math.min(fitScale*8,gesture.scale*distance/gesture.distance));
      x=(p[0].x+p[1].x)/2-rect.left-gesture.anchorX*scale;
      y=(p[0].y+p[1].y)/2-rect.top-gesture.anchorY*scale;showScale();
      ignoreClickUntil=performance.now()+400;
    }else if(gesture.type==='pan'){
      if(Math.hypot(e.clientX-gesture.startX,e.clientY-gesture.startY)>5) ignoreClickUntil=performance.now()+400;
      x+=e.clientX-gesture.x;y+=e.clientY-gesture.y;gesture.x=e.clientX;gesture.y=e.clientY;
    }
    paint();
  });
  function stopPointer(e){
    if(!points.has(e.pointerId)) return;
    points.delete(e.pointerId);startGesture();
    if(canvas.hasPointerCapture(e.pointerId)) canvas.releasePointerCapture(e.pointerId);
  }
  viewport.addEventListener('pointerup',stopPointer);viewport.addEventListener('pointercancel',stopPointer);
  viewport.addEventListener('lostpointercapture',stopPointer);
  viewer.addEventListener('keydown',function(e){
    if(!current||current.closing) return;
    if(e.key==='+'||e.key==='=') centerZoom(scale*1.4);
    else if(e.key==='-') centerZoom(scale/1.4);
    else if(e.key==='0') fit(true);
    else if(e.key==='ArrowLeft') {stopAnimation();x+=40;paint();}
    else if(e.key==='ArrowRight') {stopAnimation();x-=40;paint();}
    else if(e.key==='ArrowUp') {stopAnimation();y+=40;paint();}
    else if(e.key==='ArrowDown') {stopAnimation();y-=40;paint();}
    else return;
    e.preventDefault();
  });
  window.addEventListener('resize',function(){if(current&&!current.closing){stopAnimation();fit(false);}});
  window.addEventListener('beforeprint',function(){closeDiagram(true);});
})();
