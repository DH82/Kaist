let me=null,meGeneration=0,meBusy=false,meDrawing=false,meLast=null,meRaf=0;
const meMask=document.createElement('canvas'),meOverlay=document.createElement('canvas');
function meMessage(text,error=false){$('meStatus').textContent=text;$('meStatus').classList.toggle('error',error)}
function meControls(){const ready=!!me&&!meBusy;$('meSave').disabled=!ready||!me.dirty;$('meUndo').disabled=!ready||meDrawing||!me.history.length;$('meRedo').disabled=!ready||meDrawing||!me.future.length;$('meReset').disabled=!ready||!me.dirty;['meCam','meObj','meFrame','mePrev','meNext','meBrushMode','meBrush','meEraser','meRadius','meZoom'].forEach(id=>$(id).disabled=meBusy);$('meDirty').textContent=me?.dirty?'저장하지 않은 수정 있음':'서버 마스크와 동일';}
function meFit(){if(!me)return;const fit=Math.max(100,Math.min(me.width,$('meViewport').clientWidth-4));$('meCanvas').style.width=(fit*Number($('meZoom').value))+'px';$('meZoomLabel').textContent=Math.round(Number($('meZoom').value)*100)+'%';}
// Zoom the view only: mask pixels and brush coordinates remain in source resolution.
function meZoomAt(value,clientX,clientY){
 if(!me||meBusy||meDrawing)return;
 const viewport=$('meViewport'),canvas=$('meCanvas'),before=canvas.getBoundingClientRect(),view=viewport.getBoundingClientRect();
 const x=clientX??view.left+viewport.clientWidth/2,y=clientY??view.top+viewport.clientHeight/2;
 const u=(x-before.left)/before.width,v=(y-before.top)/before.height;
 $('meZoom').value=String(Math.max(1,Math.min(8,value)));meFit();
 const after=canvas.getBoundingClientRect();
 viewport.scrollLeft+=after.left+u*after.width-x;
 viewport.scrollTop+=after.top+v*after.height-y;
}
$('meViewport').addEventListener('wheel',e=>{
 if(!me)return;
 e.preventDefault();
 if(meBusy||meDrawing)return;
 const unit=e.deltaMode===1?16:e.deltaMode===2?$('meViewport').clientHeight:1;
 const delta=Math.max(-240,Math.min(240,e.deltaY*unit));
 meZoomAt(Number($('meZoom').value)*Math.exp(-delta*.002),e.clientX,e.clientY);
},{passive:false});
function meRender(){if(!me)return;const canvas=$('meCanvas'),ctx=canvas.getContext('2d'),overlay=meOverlay.getContext('2d');ctx.clearRect(0,0,canvas.width,canvas.height);ctx.drawImage(me.rgb,0,0);overlay.clearRect(0,0,canvas.width,canvas.height);overlay.globalCompositeOperation='source-over';overlay.fillStyle=me.obj===0?'rgb(40,230,90)':'rgb(245,70,205)';overlay.fillRect(0,0,canvas.width,canvas.height);overlay.globalCompositeOperation='destination-in';overlay.drawImage(meMask,0,0);ctx.globalAlpha=.48;ctx.drawImage(meOverlay,0,0);ctx.globalAlpha=1;}
function meSchedule(){if(!meRaf)meRaf=requestAnimationFrame(()=>{meRaf=0;meRender()})}
function meImage(src){return new Promise((resolve,reject)=>{const im=new Image();im.onload=()=>resolve(im);im.onerror=()=>reject(Error('이미지를 불러오지 못했습니다. 다시 불러오세요.'));im.src=src})}
function meRestoreSelectors(){if(!me)return;$('meCam').value=me.cam;$('meObj').value=me.obj;$('meFrame').value=me.frame+1;}
async function meLoad(cam,obj,frame,force=false){
 if(meBusy)return;
 if(!force&&me?.dirty&&!confirm('저장하지 않은 수정을 버리고 다른 마스크를 열까요?')){meRestoreSelectors();return}
 const generation=++meGeneration;meBusy=true;meControls();meMessage('RGB와 편집용 마스크를 불러오는 중…');
 try{
  const base=HC_BASE+'/episodes/'+encodeURIComponent($('meEpisode').textContent);const data=await hcRequest(base+'/edit?'+new URLSearchParams({cam,obj,frame}));
  const [rgb,mask]=await Promise.all([meImage(base+'/image?'+new URLSearchParams({cam,frame,mode:'rgb'})),meImage('data:image/png;base64,'+data.mask_png)]);
  if(generation!==meGeneration)return;
  me={...data,base,rgb,dirty:false,history:[],future:[]};
  for(const canvas of [meMask,meOverlay,$('meCanvas')]){canvas.width=data.width;canvas.height=data.height}
  const ctx=meMask.getContext('2d',{willReadFrequently:true});ctx.drawImage(mask,0,0);const pixels=ctx.getImageData(0,0,data.width,data.height);
  for(let i=0;i<pixels.data.length;i+=4){const inside=pixels.data[i]>0;pixels.data[i]=pixels.data[i+1]=pixels.data[i+2]=255;pixels.data[i+3]=inside?255:0}
  ctx.putImageData(pixels,0,0);me.original=ctx.getImageData(0,0,data.width,data.height);meRestoreSelectors();$('meZoom').value='1';meFit();$('meViewport').scrollLeft=0;$('meViewport').scrollTop=0;meRender();
  $('meDimensions').textContent=data.width+' × '+data.height+'px · 프레임 '+(frame+1);$('meFrame').max=hcMeta.frames;
  meMessage(data.approval.status==='approved'?'승인된 마스크입니다. 초안을 저장하면 기존 승인이 해제되고 재검수가 필요합니다.':'브러시로 수정한 뒤 초안 저장을 누르세요.');
 }catch(e){meRestoreSelectors();meMessage(e.message,true)}
 finally{meBusy=false;meControls()}
}
async function openMaskEditor(cam=0){
 if(!hcMeta||hcStage!=='mask')return;hcStop();me=null;meBusy=false;$('meCanvas').getContext('2d').clearRect(0,0,$('meCanvas').width,$('meCanvas').height);$('meEpisode').textContent=hcMeta.episode;$('meDialog').showModal();await meLoad(cam,Number($('meObj').value),hcFrame,true)
}
function closeMaskEditor(){if(meBusy)return;if(me?.dirty&&!confirm('저장하지 않은 수정을 버리고 닫을까요?'))return;meDrawing=false;$('meDialog').close();me=null}
function mePoint(e){const r=$('meCanvas').getBoundingClientRect();return {x:Math.max(0,Math.min(me.width-1,(e.clientX-r.left)*me.width/r.width)),y:Math.max(0,Math.min(me.height-1,(e.clientY-r.top)*me.height/r.height))}}
function meStroke(point){
 const ctx=meMask.getContext('2d'),radius=Number($('meRadius').value);ctx.globalCompositeOperation=$('meBrushMode').value==='add'?'source-over':'destination-out';ctx.fillStyle=ctx.strokeStyle='white';ctx.lineWidth=2*radius;ctx.lineCap='round';ctx.lineJoin='round';
 if(meLast){ctx.beginPath();ctx.moveTo(meLast.x,meLast.y);ctx.lineTo(point.x,point.y);ctx.stroke()}else{ctx.beginPath();ctx.arc(point.x,point.y,radius,0,Math.PI*2);ctx.fill()}
 ctx.globalCompositeOperation='source-over';meLast=point;me.dirty=true;meSchedule();meControls();
}
const meCanvas=$('meCanvas');
meCanvas.onpointerdown=e=>{if(!me||meBusy||e.button!==0)return;e.preventDefault();meCanvas.focus({preventScroll:true});me.history.push(meSnapshot());me.future=[];if(me.history.length>20)me.history.shift();meDrawing=true;meLast=null;meCanvas.setPointerCapture(e.pointerId);meStroke(mePoint(e))};
meCanvas.onpointermove=e=>{if(meDrawing&&me&&!meBusy){e.preventDefault();meStroke(mePoint(e))}};
meCanvas.onpointerup=meCanvas.onpointercancel=()=>{meDrawing=false;meLast=null;meControls()};
function meSnapshot(){return {pixels:meMask.getContext('2d').getImageData(0,0,me.width,me.height),dirty:me.dirty}}
function meApply(snapshot){meMask.getContext('2d').putImageData(snapshot.pixels,0,0);me.dirty=snapshot.dirty;meLast=null;meRender();meControls()}
function meUndo(){if(!me||meBusy||meDrawing||!me.history.length)return;me.future.push(meSnapshot());meApply(me.history.pop())}
function meRedo(){if(!me||meBusy||meDrawing||!me.future.length)return;me.history.push(meSnapshot());meApply(me.future.pop())}
function meSetTool(tool){if(meBusy||meDrawing)return;$('meBrushMode').value=tool;$('meBrush').setAttribute('aria-pressed',String(tool==='add'));$('meEraser').setAttribute('aria-pressed',String(tool==='erase'));$('meCanvas').style.cursor=tool==='erase'?'cell':'crosshair'}
$('meBrush').onclick=()=>meSetTool('add');$('meEraser').onclick=()=>meSetTool('erase');
$('meUndo').onclick=meUndo;$('meRedo').onclick=meRedo;
$('meReset').onclick=()=>{if(!me||meBusy||meDrawing||!me.dirty)return;me.history.push(meSnapshot());if(me.history.length>20)me.history.shift();me.future=[];meApply({pixels:me.original,dirty:false})};
$('meDialog').addEventListener('keydown',e=>{
 if(!me||meBusy||meDrawing||e.isComposing||e.target.closest('input,textarea,select,[contenteditable="true"]'))return;
 const key=e.key.toLowerCase();
 if((e.ctrlKey||e.metaKey)&&key==='z'){e.preventDefault();e.shiftKey?meRedo():meUndo()}
 else if(e.ctrlKey&&key==='y'){e.preventDefault();meRedo()}
 else if(!e.ctrlKey&&!e.metaKey&&!e.altKey&&(key==='b'||key==='e')){e.preventDefault();meSetTool(key==='b'?'add':'erase')}
});
$('meCam').onchange=$('meObj').onchange=$('meFrame').onchange=()=>meLoad(Number($('meCam').value),Number($('meObj').value),Math.max(0,Math.min(hcMeta.frames-1,Number($('meFrame').value)-1)));
$('mePrev').onclick=()=>{if(me)meLoad(me.cam,me.obj,Math.max(0,me.frame-1))};$('meNext').onclick=()=>{if(me)meLoad(me.cam,me.obj,Math.min(hcMeta.frames-1,me.frame+1))};
$('meZoom').oninput=()=>meZoomAt(Number($('meZoom').value));window.addEventListener('resize',meFit);$('meRadius').oninput=()=>$('meRadiusLabel').textContent=$('meRadius').value+'px';
$('meClose').onclick=closeMaskEditor;$('meDialog').oncancel=e=>{e.preventDefault();closeMaskEditor()};
$('hcEditMask').onclick=()=>openMaskEditor(0);
$('meSave').onclick=async()=>{
 if(!me||meBusy||!me.dirty)return;
 if(me.approval.status==='approved'&&!confirm('수정 초안을 저장하면 기존 승인이 해제됩니다. 저장하고 다시 검수할까요?'))return;
 const opaque=document.createElement('canvas');opaque.width=me.width;opaque.height=me.height;const ctx=opaque.getContext('2d');ctx.fillStyle='black';ctx.fillRect(0,0,me.width,me.height);ctx.drawImage(meMask,0,0);const pixels=ctx.getImageData(0,0,me.width,me.height);for(let i=0;i<pixels.data.length;i+=4){const v=pixels.data[i]>=128?255:0;pixels.data[i]=pixels.data[i+1]=pixels.data[i+2]=v;pixels.data[i+3]=255}ctx.putImageData(pixels,0,0);
 const payload={cam:me.cam,obj:me.obj,frame:me.frame,basis:me.basis,file_sha256:me.file_sha256,mask_png:opaque.toDataURL('image/png').split(',')[1]};
 const episode=me.episode,frame=me.frame;meBusy=true;meControls();meMessage('수정 내용과 원본 변경 여부를 검사하고 저장하는 중…');
 try{
  const result=await hcRequest(me.base+'/edit',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':hcToken},body:JSON.stringify(payload)});
  me.dirty=false;$('meDialog').close();me=null;meBusy=false;await openReview(episode,'mask');if(hcMeta)await hcShowFrame(frame);hcStatus(result.message);loadJobs();
 }catch(e){meMessage(e.message,true)}finally{meBusy=false;meControls()}
};
