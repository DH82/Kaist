// HC 마스크 검수 화면의 카메라 1~4 타일에서 바로 마스크를 수정합니다.
// - 원본 + 마스크 표시에서 타일 위를 드래그(또는 클릭)하면 선택한 대상(손/물체)의 마스크를 칠하거나 지웁니다.
// - 마법봉: 클릭한 곳을 SAM3.1로 분할합니다. 물체 이름을 함께 주면 같은 종류의 물체를 먼저 찾고 클릭한 것을 고릅니다.
//   왼쪽 클릭 = 포함할 곳, Shift+클릭 또는 오른쪽 클릭 = 제외할 곳, Enter = 모든 카메라의 결과 적용 후 바로 저장,
//   Esc = 마지막으로 누른 카메라의 점 지우기, '모든 점 초기화' = 모든 카메라의 점·결과 지우기
//   '빈 마스크 일괄 적용' = 마지막으로 누른 카메라의 점 프롬프트로, 같은 카메라에서 마스크가 비어 있는 프레임들을 일괄 분할·저장
// - 타일을 더블클릭하면 그 카메라만 크게 보고, 다시 더블클릭(또는 Esc)하면 2×2로 돌아갑니다.
// - 마스크 좌표와 브러시 반경은 원본 해상도 기준이며, 원본 RGB는 바꾸지 않습니다.
const hcEdit={cams:[null,null,null,null],history:[],future:[],stroke:null,tap:null,saving:false,solo:null,
 names:{0:null,1:'hand'},wandCam:null,wandLoaded:false};
const HC_TAP_DELAY=260;
// 마법봉 일괄 적용 상태(같은 카메라의 빈 마스크 프레임에 현재 점 프롬프트를 차례로 적용·저장)
const hcBatch={running:false,stop:false};
// 빈 마스크 프레임 찾기 상태(조회한 픽셀 수 캐시 포함)
const hcEmptyNav={running:false,stop:false,cache:new Map()};

function hcTool(){return $('hcBrushMode').value}
function hcEditKey(){return $('hcEditObj').value+':'+hcFrame}
function hcEditDirtyCams(){return [0,1,2,3].filter(cam=>hcEdit.cams[cam]?.ready&&hcEdit.cams[cam].dirty)}
function hcEditDirty(){return hcEditDirtyCams().length>0}
function hcEditMessage(text,error=false){$('hcEditState').textContent=text;$('hcEditState').classList.toggle('error',error)}
function hcWandMessage(text,error=false){$('hcWandState').textContent=text;$('hcWandState').classList.toggle('error',error)}
function hcEditBlocked(){
 if(!hcMeta||hcStage!=='mask')return '검수 화면을 불러온 뒤 수정할 수 있습니다.';
 if(hcEdit.saving)return '저장하는 중입니다.';
 if(hcBusy)return '검수 결정을 기록하는 중입니다.';
 if(hcFrameLoading)return '프레임을 불러오는 중입니다.';
 if($('hcMaskMode').value!=='overlay')return '표시를 "원본 + 마스크"로 바꾸면 화면에서 바로 수정할 수 있습니다.';
 return '';
}
function hcWandPending(e){return Boolean(e?.ready&&e.wand&&(e.wand.mask||e.wand.points.length))}
// 적용·지우기 후에는 마법봉 작업이 남아 있는 다른 카메라를 이어서 활성 카메라로 삼습니다.
function hcWandNextCam(){const cam=[0,1,2,3].find(c=>hcWandPending(hcEdit.cams[c]));return cam===undefined?null:cam}
function hcWandReadyCams(){return [0,1,2,3].filter(c=>{const e=hcEdit.cams[c];return e?.ready&&e.wand?.mask&&!e.wand.busy})}
function hcWandPendingCams(){return [0,1,2,3].filter(c=>hcWandPending(hcEdit.cams[c]))}
function hcWandEntry(){const cam=hcEdit.wandCam;const e=cam===null?null:hcEdit.cams[cam];return e?.ready&&e.wand?e:null}
function hcEditControls(){
 const dirty=hcEditDirtyCams(),busy=hcEdit.saving||hcBusy,we=hcWandEntry();
 $('hcSaveEdits').disabled=busy||!dirty.length;$('hcDiscardEdits').disabled=busy||!dirty.length;
 $('hcEditUndo').disabled=busy||!hcEdit.history.length;$('hcEditRedo').disabled=busy||!hcEdit.future.length;
 $('hcSaveEdits').textContent=dirty.length?'수정 저장 ('+dirty.map(c=>'카메라 '+(c+1)).join(', ')+')':'수정 저장';
 const ready=hcWandReadyCams(),pending=hcWandPendingCams();
 $('hcWandApply').disabled=busy||!ready.length||hcWandBusyCams().length>0;$('hcWandClear').disabled=busy||!we?.wand.points.length;$('hcWandClearAll').disabled=busy||!pending.length;
 hcEmptyNavControls();
 $('hcBatchApply').textContent='빈 '+($('hcEditObj').value==='0'?'물체':'손')+' 마스크 일괄 적용';
 $('hcBatchApply').disabled=busy||hcBatch.running||!we?.wand.points.some(x=>x[2]===1)||we.wand.busy;$('hcBatchStop').hidden=!hcBatch.running;
 $('hcWandApply').textContent=(ready.length>1?'마법봉 결과 적용·저장 · '+ready.map(c=>'카메라 '+(c+1)).join(', '):'마법봉 결과 적용·저장')+' (Enter)';
 for(let cam=0;cam<4;cam++){const tag=$('hcCamEdit'+cam);if(tag){const e=hcEdit.cams[cam],d=e?.ready&&e.dirty,none=e?.ready&&e.data.missing&&!d;
  // SAM3가 이 프레임에서 대상을 찾지 못해 마스크 파일이 없는 경우: 빈 마스크에서 새로 칠할 수 있습니다.
  const w=hcWandPending(e)?(e.wand.busy?'마법봉 분할 중…':e.wand.mask?'마법봉 적용 대기 (Enter)':'마법봉 점만 있음'):'';
  tag.textContent=[d?'수정됨 · 저장 전':none?(e.obj===0?'물체':'손')+' 마스크 없음 · 칠하면 새로 만듦':'',w].filter(Boolean).join(' · ');tag.hidden=!tag.textContent}}
 if(!busy&&!$('hcEditState').classList.contains('error'))hcEditMessage(dirty.length?'저장하지 않은 수정이 있습니다. 다른 프레임으로 이동하기 전에 저장하세요.':'');
}

// ---- 타일 렌더링 ----
function hcEditRender(cam){
 const e=hcEdit.cams[cam],canvas=$('hcEdit'+cam);if(!canvas)return;
 const ctx=canvas.getContext('2d'),wand=e?.ready&&e.wand&&(e.wand.mask||e.wand.points.length);
 if(!e?.ready||(!e.dirty&&!wand)){ctx.clearRect(0,0,canvas.width,canvas.height);return}
 if(canvas.width!==e.width||canvas.height!==e.height){canvas.width=e.width;canvas.height=e.height}
 ctx.globalCompositeOperation='source-over';ctx.globalAlpha=1;ctx.clearRect(0,0,e.width,e.height);
 const tint=e.tint.getContext('2d');
 if(e.dirty){
  // 수정 전·후 마스크가 닿는 영역만 다시 그립니다. 그 밖은 서버 오버레이 이미지가 그대로 보입니다.
  const region=e.scratch.getContext('2d');region.globalCompositeOperation='source-over';region.clearRect(0,0,e.width,e.height);
  region.putImageData(e.original,0,0);region.drawImage(e.mask,0,0);
  ctx.drawImage(e.rgb,0,0);ctx.globalCompositeOperation='destination-in';ctx.drawImage(e.scratch,0,0);ctx.globalCompositeOperation='source-over';
  tint.globalCompositeOperation='source-over';tint.clearRect(0,0,e.width,e.height);
  tint.fillStyle=e.obj===0?'rgb(40,230,90)':'rgb(245,70,205)';tint.fillRect(0,0,e.width,e.height);
  tint.globalCompositeOperation='destination-in';tint.drawImage(e.mask,0,0);
  ctx.globalAlpha=.48;ctx.drawImage(e.tint,0,0);ctx.globalAlpha=1;
 }
 if(e.wand?.mask){
  // 마법봉 결과(적용 전)는 노란색으로 겹쳐 보여 줍니다.
  tint.globalCompositeOperation='source-over';tint.clearRect(0,0,e.width,e.height);
  tint.fillStyle='rgb(255,210,0)';tint.fillRect(0,0,e.width,e.height);
  tint.globalCompositeOperation='destination-in';tint.drawImage(e.wand.mask,0,0);
  ctx.globalAlpha=e.wand.busy?.3:.55;ctx.drawImage(e.tint,0,0);ctx.globalAlpha=1;
 }
 if(e.wand){
  const r=Math.max(3,e.width/110);
  for(const [x,y,label] of e.wand.points){
   ctx.beginPath();ctx.arc(x*e.width,y*e.height,r,0,Math.PI*2);ctx.fillStyle=label?'#22c55e':'#ef4444';ctx.fill();
   ctx.lineWidth=Math.max(1,r/3);ctx.strokeStyle='white';ctx.stroke();
   ctx.beginPath();ctx.moveTo(x*e.width-r*.55,y*e.height);ctx.lineTo(x*e.width+r*.55,y*e.height);
   if(label){ctx.moveTo(x*e.width,y*e.height-r*.55);ctx.lineTo(x*e.width,y*e.height+r*.55)}ctx.stroke();
  }
 }
}
function hcEditSchedule(cam){const e=hcEdit.cams[cam];if(e&&!e.raf)e.raf=requestAnimationFrame(()=>{e.raf=0;hcEditRender(cam)})}

// ---- 편집용 마스크 불러오기 (타일에 마우스를 올리면 미리 불러옵니다) ----
function hcEditImage(src){return new Promise((resolve,reject)=>{const im=new Image();im.onload=()=>resolve(im);im.onerror=()=>reject(Error('원본 이미지를 불러오지 못했습니다.'));im.src=src})}
function hcEditLoad(cam){
 const key=hcEditKey(),cur=hcEdit.cams[cam];
 if(cur&&cur.key===key&&!cur.failed)return cur.promise;
 const obj=Number($('hcEditObj').value),frame=hcFrame,path=hcPath;
 const entry={key,cam,obj,frame,ready:false,dirty:false,failed:false,raf:0,wand:null};
 entry.promise=(async()=>{
  const data=await hcRequest(path+'/edit?'+new URLSearchParams({cam,obj,frame}));
  const [rgb,maskImage]=await Promise.all([hcEditImage(path+'/image?'+new URLSearchParams({cam,frame,mode:'rgb'})),hcEditImage('data:image/png;base64,'+data.mask_png)]);
  if(hcEdit.cams[cam]!==entry)return null;
  const make=()=>{const c=document.createElement('canvas');c.width=data.width;c.height=data.height;return c};
  Object.assign(entry,{data,rgb,width:data.width,height:data.height,mask:make(),scratch:make(),tint:make()});
  const ctx=entry.mask.getContext('2d',{willReadFrequently:true});ctx.drawImage(maskImage,0,0);
  const px=ctx.getImageData(0,0,data.width,data.height);
  for(let i=0;i<px.data.length;i+=4){const inside=px.data[i]>0;px.data[i]=px.data[i+1]=px.data[i+2]=255;px.data[i+3]=inside?255:0}
  ctx.putImageData(px,0,0);entry.original=ctx.getImageData(0,0,data.width,data.height);entry.ready=true;
  // 물체 이름 기본값: 이 에피소드 SAM3 실행에 쓴 프롬프트
  if(hcEdit.names[0]===null&&data.sam_prompt){hcEdit.names[0]=data.sam_prompt;hcWandSyncName()}
  hcEditControls();
  return entry;
 })().catch(err=>{if(hcEdit.cams[cam]===entry){entry.failed=true;hcEditMessage('카메라 '+(cam+1)+': '+err.message,true)}return null});
 hcEdit.cams[cam]=entry;return entry.promise;
}
function hcEditReset(){
 clearTimeout(hcEdit.tap?.timer);hcEdit.tap=null;hcEdit.stroke=null;hcEdit.wandCam=null;
 hcEdit.cams.forEach(e=>{if(e?.raf)cancelAnimationFrame(e.raf)});
 hcEdit.cams=[null,null,null,null];hcEdit.history=[];hcEdit.future=[];
 for(let cam=0;cam<4;cam++)hcEditRender(cam);
 $('hcEditState').classList.remove('error');hcWandMessage('');hcEditControls();
}
function hcEditSnap(cam,e){return {cam,entry:e,pixels:e.mask.getContext('2d').getImageData(0,0,e.width,e.height),dirty:e.dirty}}
// 되돌리기 한 단계 = 카메라 여러 개의 스냅숏. 마법봉을 여러 카메라에 한꺼번에 적용해도 한 번에 되돌립니다.
function hcEditPushHistory(snaps){
 hcEdit.history.push(snaps);
 if(hcEdit.history.length>30)hcEdit.history.shift();hcEdit.future=[];
}

// ---- 브러시 ----
function hcEditPoint(cam,ev){const r=$('hcEdit'+cam).getBoundingClientRect(),e=hcEdit.cams[cam],w=e?.width||$('hcImage'+cam).naturalWidth||r.width,h=e?.height||$('hcImage'+cam).naturalHeight||r.height;return {x:Math.max(0,Math.min(w-1,(ev.clientX-r.left)*w/r.width)),y:Math.max(0,Math.min(h-1,(ev.clientY-r.top)*h/r.height))}}
function hcEditDraw(e,from,to){
 const ctx=e.mask.getContext('2d'),radius=Number($('hcRadius').value);
 ctx.globalCompositeOperation=hcTool()==='add'?'source-over':'destination-out';ctx.fillStyle=ctx.strokeStyle='white';
 if(from){ctx.lineWidth=2*radius;ctx.lineCap='round';ctx.lineJoin='round';ctx.beginPath();ctx.moveTo(from.x,from.y);ctx.lineTo(to.x,to.y);ctx.stroke()}
 else{ctx.beginPath();ctx.arc(to.x,to.y,radius,0,Math.PI*2);ctx.fill()}
 ctx.globalCompositeOperation='source-over';
}
async function hcEditFlush(stroke){
 const e=await hcEditLoad(stroke.cam);
 if(!e||stroke.cancelled||hcEdit.cams[stroke.cam]!==e)return;
 if(!stroke.started){stroke.started=true;hcEditPushHistory([hcEditSnap(stroke.cam,e)])}
 while(stroke.drawn<stroke.pts.length){hcEditDraw(e,stroke.drawn?stroke.pts[stroke.drawn-1]:null,stroke.pts[stroke.drawn]);stroke.drawn++}
 e.dirty=true;hcEditSchedule(stroke.cam);hcEditControls();
}
function hcEditRing(cam,ev){
 const ring=$('hcRing'+cam),canvas=$('hcEdit'+cam);if(!ring||!canvas)return;
 if(!ev||hcTool()==='wand'||hcEditBlocked()){ring.hidden=true;return}
 const r=canvas.getBoundingClientRect(),w=hcEdit.cams[cam]?.width||$('hcImage'+cam).naturalWidth||r.width;
 const size=2*Number($('hcRadius').value)*r.width/w;
 Object.assign(ring.style,{width:size+'px',height:size+'px',left:(ev.clientX-r.left)+'px',top:(ev.clientY-r.top)+'px'});
 ring.classList.toggle('erase',hcTool()==='erase');ring.hidden=false;
}
function hcCancelTap(){if(hcEdit.tap){clearTimeout(hcEdit.tap.timer);hcEdit.tap.stroke.cancelled=true;hcEdit.tap=null}}
function hcEditBind(cam){
 const canvas=$('hcEdit'+cam);
 canvas.onpointerenter=ev=>{if(!hcEditBlocked())hcEditLoad(cam);hcEditRing(cam,ev)};
 canvas.onpointerleave=()=>hcEditRing(cam,null);
 canvas.oncontextmenu=ev=>{if(hcTool()==='wand')ev.preventDefault()};
 canvas.onpointerdown=ev=>{
  const wand=hcTool()==='wand';
  if(ev.button!==0&&!(wand&&ev.button===2))return;
  const blocked=hcEditBlocked();if(blocked){hcEditMessage(blocked,true);return}
  ev.preventDefault();hcStop();$('hcEditState').classList.remove('error');
  const p=hcEditPoint(cam,ev);
  // 마법봉 오른쪽 클릭은 더블클릭과 겹치지 않으므로 바로 '제외' 점으로 씁니다.
  if(wand&&ev.button===2){hcWandClick(cam,p,0);return}
  // 같은 자리를 빠르게 두 번 누르면 더블클릭(크게 보기)으로 보고 점을 찍지 않습니다.
  if(hcEdit.tap&&hcEdit.tap.cam===cam&&Math.hypot(hcEdit.tap.client.x-ev.clientX,hcEdit.tap.client.y-ev.clientY)<12)hcCancelTap();
  hcEdit.stroke={cam,pts:[p],drawn:0,started:false,moved:false,cancelled:false,pointer:ev.pointerId,client:{x:ev.clientX,y:ev.clientY},
   wand,label:ev.shiftKey||ev.altKey?0:1};
  canvas.setPointerCapture(ev.pointerId);hcEditLoad(cam);
 };
 canvas.onpointermove=ev=>{
  hcEditRing(cam,ev);const s=hcEdit.stroke;
  if(!s||s.cam!==cam||s.pointer!==ev.pointerId||s.wand)return;
  ev.preventDefault();s.pts.push(hcEditPoint(cam,ev));
  if(!s.moved&&Math.hypot(ev.clientX-s.client.x,ev.clientY-s.client.y)>4)s.moved=true;
  if(s.moved)hcEditFlush(s);
 };
 canvas.onpointerup=ev=>{
  const s=hcEdit.stroke;if(!s||s.cam!==cam||s.pointer!==ev.pointerId)return;hcEdit.stroke=null;
  if(s.moved){hcEditFlush(s);return}
  s.pts=[s.pts[0]];
  const run=s.wand?()=>{if(!s.cancelled)hcWandClick(cam,s.pts[0],s.label)}:()=>hcEditFlush(s);
  hcEdit.tap={cam,stroke:s,client:s.client,timer:setTimeout(()=>{if(hcEdit.tap?.stroke===s)hcEdit.tap=null;run()},HC_TAP_DELAY)};
 };
 canvas.onpointercancel=()=>{if(hcEdit.stroke?.cam===cam){hcEdit.stroke.cancelled=!hcEdit.stroke.started;hcEdit.stroke=null}};
 canvas.ondblclick=ev=>{ev.preventDefault();hcCancelTap();hcToggleSolo(cam)};
}

// ---- 마법봉 (SAM3.1) ----
function hcWandSyncName(){const v=hcEdit.names[Number($('hcEditObj').value)];$('hcWandText').value=v||''}
function hcWandRequestBody(e){
 const useName=$('hcWandPrompt').value==='name';
 return {cam:e.cam,frame:e.frame,points:e.wand.points,text:useName?$('hcWandText').value.trim():'',threshold:Number($('hcWandThr').value)};
}
async function hcWandClick(cam,p,label){
 const blocked=hcEditBlocked();if(blocked){hcEditMessage(blocked,true);return}
 const e=await hcEditLoad(cam);if(!e||hcEdit.cams[cam]!==e)return;
 // 마법봉 점·결과는 카메라마다 따로 유지합니다. 다른 카메라를 눌러도 이전 카메라의 작업은 그대로 남고,
 // 적용(Enter)·점 지우기(Esc)는 마지막으로 누른 카메라에 동작합니다.
 hcEdit.wandCam=cam;
 if(!e.wand)e.wand={points:[],mask:null,seq:0,busy:false};
 if(label===0&&!e.wand.points.some(x=>x[2]===1)){hcWandMessage('먼저 포함할 곳을 왼쪽 클릭하세요. 제외는 Shift+클릭 또는 오른쪽 클릭입니다.',true);return}
 if(e.wand.points.length>=20){hcWandMessage('점은 20개까지 찍을 수 있습니다. 점 지우기 후 다시 시작하세요.',true);return}
 e.wand.points.push([p.x/e.width,p.y/e.height,label]);hcEditRender(cam);hcEditControls();hcWandRun(cam);
}
async function hcWandRun(cam){
 const e=hcEdit.cams[cam];if(!e?.wand?.points.length)return;
 const w=e.wand,seq=++w.seq;w.busy=true;hcEditRender(cam);hcEditControls();
 hcWandMessage(hcEdit.wandLoaded?'SAM3로 분할하는 중…':'SAM3 모델을 GPU에 불러오는 중… 처음 한 번은 10~20초 걸립니다.');
 try{
  const r=await hcRequest(hcPath+'/wand',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':hcToken},body:JSON.stringify(hcWandRequestBody(e))});
  if(hcEdit.cams[cam]!==e||e.wand!==w||w.seq!==seq)return;
  const im=await hcEditImage('data:image/png;base64,'+r.mask_png);
  if(hcEdit.cams[cam]!==e||e.wand!==w||w.seq!==seq)return;
  const c=document.createElement('canvas');c.width=e.width;c.height=e.height;const ctx=c.getContext('2d',{willReadFrequently:true});ctx.drawImage(im,0,0,e.width,e.height);
  const px=ctx.getImageData(0,0,e.width,e.height);
  for(let i=0;i<px.data.length;i+=4){const inside=px.data[i]>127;px.data[i]=px.data[i+1]=px.data[i+2]=255;px.data[i+3]=inside?255:0}
  ctx.putImageData(px,0,0);w.mask=c;hcEdit.wandLoaded=true;
  const how={text:'이름으로 찾음','text+points':'이름 + 클릭으로 다듬음',points:'클릭으로 분할'}[r.method]||r.method;
  hcWandMessage('카메라 '+(cam+1)+' · '+how+' · '+r.area.toLocaleString()+'px · '+r.message+' 노란 영역을 확인하고 Enter(적용)를 누르세요.');
 }catch(err){
  if(hcEdit.cams[cam]===e&&e.wand===w&&w.seq===seq){w.points.pop();hcWandMessage(err.message,true)}
 }finally{
  if(e.wand===w&&w.seq===seq){w.busy=false;hcEditRender(cam);hcEditControls()}
 }
}
function hcWandBusyCams(){return [0,1,2,3].filter(c=>hcEdit.cams[c]?.wand?.busy)}
// 지금까지 마법봉 결과(노란 영역)가 나온 모든 카메라에 한 번에 적용하고 곧바로 저장합니다.
// 저장하면 프레임을 새로 불러와 남은 마법봉 점도 지워지므로, 분할 중인 카메라가 있으면 끝날 때까지 기다립니다.
async function hcWandApply(){
 if(hcEdit.saving||hcBusy)return;
 const cams=hcWandReadyCams();if(!cams.length)return;
 const busyCams=hcWandBusyCams();
 if(busyCams.length){hcWandMessage(busyCams.map(c=>'카메라 '+(c+1)).join(', ')+' 분할이 끝난 뒤 적용·저장할 수 있습니다.',true);return}
 const mode=$('hcWandMode').value;
 hcEditPushHistory(cams.map(cam=>hcEditSnap(cam,hcEdit.cams[cam])));
 for(const cam of cams){
  const e=hcEdit.cams[cam],ctx=e.mask.getContext('2d');
  if(mode==='replace')ctx.clearRect(0,0,e.width,e.height);
  ctx.globalCompositeOperation=mode==='subtract'?'destination-out':'source-over';ctx.drawImage(e.wand.mask,0,0);ctx.globalCompositeOperation='source-over';
  e.wand=null;e.dirty=true;hcEditRender(cam);
 }
 hcEdit.wandCam=hcWandNextCam();
 const names=cams.map(c=>'카메라 '+(c+1)).join(', '),how=$('hcWandMode').selectedOptions[0].textContent;
 hcWandMessage(names+'에 적용했습니다('+how+'). 저장하는 중…');hcEditControls();
 // 수정 저장과 같은 경로로 저장합니다(이 프레임의 저장 전 브러시 수정도 함께 저장됩니다). 저장 후 화면이 바로 새 마스크로 바뀝니다.
 const ok=await hcSaveEdits();
 if(ok&&!hcEditDirty())hcWandMessage(names+' 마법봉 수정을 저장했습니다('+how+').');
 else hcWandMessage(names+'에 적용했지만 저장하지 못했습니다. 위 메시지를 확인하고 수정 저장을 다시 누르세요.',true);
}
function hcWandClear(){
 const e=hcWandEntry();if(!e)return false;
 const cam=hcEdit.wandCam;e.wand=null;hcEdit.wandCam=hcWandNextCam();hcEditRender(cam);hcWandMessage('');hcEditControls();return true;
}
// 마음에 들지 않을 때: 모든 카메라의 마법봉 점(포인트 프롬프트)과 노란 결과를 지우고 처음부터 다시 찍습니다.
// 이미 적용한 마스크 수정은 그대로 두며, 그것은 ↶ 취소로 되돌립니다.
function hcWandClearAll(){
 const cams=hcWandPendingCams();if(!cams.length)return false;
 for(const cam of cams){hcEdit.cams[cam].wand=null;hcEditRender(cam)}
 hcEdit.wandCam=null;hcWandMessage(cams.map(c=>'카메라 '+(c+1)).join(', ')+'의 마법봉 점을 모두 지웠습니다. 다시 클릭해 시작하세요.');hcEditControls();return true;
}
function hcWandRerun(){for(let cam=0;cam<4;cam++){const e=hcEdit.cams[cam];if(e?.ready&&e.wand?.points.length)hcWandRun(cam)}}

// ---- 크게 보기 (더블클릭) ----
function hcToggleSolo(cam){
 hcEdit.solo=hcEdit.solo===cam?null:cam;
 const grid=$('hcImages');grid.classList.toggle('hcSolo',hcEdit.solo!==null);
 grid.querySelectorAll('figure').forEach(f=>f.classList.toggle('hcFocused',Number(f.dataset.cam)===hcEdit.solo));
 for(let c=0;c<4;c++)hcEditRing(c,null);
 if(hcEdit.solo!==null)grid.querySelector('figure[data-cam="'+cam+'"]').scrollIntoView({block:'nearest',behavior:'smooth'});
}

// ---- 되돌리기 ----
function hcEditSwap(from,to){
 const step=from.pop();if(!step)return;
 const live=step.filter(s=>hcEdit.cams[s.cam]===s.entry);
 if(!live.length){hcEditControls();return}
 to.push(live.map(s=>hcEditSnap(s.cam,s.entry)));
 for(const s of live){s.entry.mask.getContext('2d').putImageData(s.pixels,0,0);s.entry.dirty=s.dirty;hcEditSchedule(s.cam)}
 hcEditControls();
}
function hcEditUndo(){if(!hcEdit.saving&&!hcEdit.stroke)hcEditSwap(hcEdit.history,hcEdit.future)}
function hcEditRedo(){if(!hcEdit.saving&&!hcEdit.stroke)hcEditSwap(hcEdit.future,hcEdit.history)}
function hcEditDiscard(){
 if(!hcEditDirty()||hcEdit.saving)return;
 if(!confirm('저장하지 않은 마스크 수정을 모두 버릴까요?'))return;
 hcEditReset();
}

// ---- 저장 ----
function hcEditPng(e){
 const out=document.createElement('canvas');out.width=e.width;out.height=e.height;const ctx=out.getContext('2d');
 ctx.fillStyle='black';ctx.fillRect(0,0,e.width,e.height);ctx.drawImage(e.mask,0,0);
 const px=ctx.getImageData(0,0,e.width,e.height);
 for(let i=0;i<px.data.length;i+=4){const v=px.data[i]>=128?255:0;px.data[i]=px.data[i+1]=px.data[i+2]=v;px.data[i+3]=255}
 ctx.putImageData(px,0,0);return out.toDataURL('image/png').split(',')[1];
}
async function hcSaveEdits(){
 const cams=hcEditDirtyCams();if(!cams.length||hcEdit.saving||hcBusy)return true;
 if(hcApproved()&&!confirm('수정한 마스크를 저장하면 기존 승인이 해제되고 다시 검수해야 합니다. 저장할까요?'))return false;
 hcStop();hcEdit.saving=true;hcEditControls();hcEditMessage('수정 내용과 원본 변경 여부를 검사하고 저장하는 중…');
 const frame=hcFrame,messages=[];let ok=true;
 try{
  for(const cam of cams){
   const e=hcEdit.cams[cam];
   // 저장할 때마다 검수 기준값(basis)이 바뀌므로 카메라마다 최신 값을 다시 받아 저장합니다.
   const fresh=await hcRequest(hcPath+'/edit?'+new URLSearchParams({cam,obj:e.obj,frame:e.frame}));
   if(fresh.file_sha256!==e.data.file_sha256)throw Error('카메라 '+(cam+1)+' 마스크가 다른 곳에서 변경됐습니다. 수정을 버리고 다시 불러오세요.');
   const payload={cam,obj:e.obj,frame:e.frame,basis:fresh.basis,file_sha256:e.data.file_sha256,mask_png:hcEditPng(e)};
   const result=await hcRequest(hcPath+'/edit',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':hcToken},body:JSON.stringify(payload)});
   e.dirty=false;messages.push('카메라 '+(cam+1)+': '+result.message);
  }
 }catch(err){ok=false;hcEditMessage(err.message,true)}
 finally{hcEdit.saving=false}
 try{const meta=await hcRequest(hcPath);if(meta&&hcMeta&&meta.episode===hcMeta.episode)hcMeta=meta}catch(_){}
 // 저장된 마스크로 서버 이미지를 새로 받습니다. 새 이미지가 뜰 때까지는 수정한 화면을 그대로 보여 줘 깜빡임이 없습니다.
 if(ok){hcImageRev++;hcStatus(messages.join(' · '));await hcShowFrame(frame,true,true);if(typeof loadJobs==='function')loadJobs()}
 else hcEditControls();
 hcEnable();return ok;
}
// 저장하지 않은 수정이 있을 때 프레임 이동 등을 하기 전에 호출합니다. true면 진행해도 됩니다.
async function hcEditResolve(action='이동'){
 if(!hcEditDirty())return true;
 if(!confirm('저장하지 않은 마스크 수정이 있습니다.\n\n확인: 저장하고 '+action+'\n취소: 그대로 머무르기'))return false;
 return hcSaveEdits();
}
function hcEditLeaveOk(){
 if(hcBatch.running){if(!confirm('마법봉 일괄 적용 중입니다. 중지하고 나갈까요? (이미 저장한 프레임은 유지됩니다)'))return false;hcBatch.stop=true}
 return !hcEditDirty()||confirm('저장하지 않은 마스크 수정이 있습니다. 버리고 나갈까요?')
}

// ---- 도구 ----
function hcSetTool(tool){
 $('hcBrushMode').value=tool;
 for(const [id,t] of [['hcBrush','add'],['hcEraser','erase'],['hcWand','wand']])$(id).setAttribute('aria-pressed',String(tool===t));
 $('hcWandTools').hidden=tool!=='wand';$('hcBrushTools').hidden=tool==='wand';
 document.querySelectorAll('.hcEditCanvas').forEach(c=>c.style.cursor=tool==='wand'?'copy':'crosshair');
 for(let c=0;c<4;c++)hcEditRing(c,null);
}
$('hcBrush').onclick=()=>hcSetTool('add');$('hcEraser').onclick=()=>hcSetTool('erase');$('hcWand').onclick=()=>hcSetTool('wand');
$('hcRadius').oninput=()=>$('hcRadiusLabel').textContent=$('hcRadius').value+'px';
$('hcEditUndo').onclick=hcEditUndo;$('hcEditRedo').onclick=hcEditRedo;
$('hcDiscardEdits').onclick=hcEditDiscard;$('hcSaveEdits').onclick=()=>hcSaveEdits();
$('hcWandApply').onclick=hcWandApply;$('hcBatchApply').onclick=hcBatchRun;$('hcBatchStop').onclick=()=>{hcBatch.stop=true;hcWandMessage('일괄 적용을 중지하는 중… 지금 처리 중인 프레임까지만 저장합니다.')};$('hcWandClear').onclick=hcWandClear;$('hcWandClearAll').onclick=hcWandClearAll;
$('hcWandText').oninput=()=>{hcEdit.names[Number($('hcEditObj').value)]=$('hcWandText').value};
$('hcWandText').onchange=hcWandRerun;$('hcWandPrompt').onchange=()=>{$('hcWandText').disabled=$('hcWandPrompt').value!=='name';hcWandRerun()};$('hcWandThr').onchange=hcWandRerun;
let hcEditObjValue=$('hcEditObj').value;
$('hcEditObj').onchange=async()=>{
 const next=$('hcEditObj').value;$('hcEditObj').value=hcEditObjValue;
 if(!await hcEditResolve('대상 변경'))return;
 $('hcEditObj').value=hcEditObjValue=next;hcEditReset();hcWandSyncName();
};
hcWandSyncName();
document.addEventListener('keydown',e=>{
 if(page!=='review'||!hcMeta||hcStage!=='mask'||$('hcWorkspace').classList.contains('hidden')||document.querySelector('dialog[open]')||e.isComposing||e.target.closest('input,textarea,select,[contenteditable="true"]'))return;
 const key=e.key.toLowerCase();
 if((e.ctrlKey||e.metaKey)&&key==='z'){e.preventDefault();e.shiftKey?hcEditRedo():hcEditUndo()}
 else if(e.ctrlKey&&key==='y'){e.preventDefault();hcEditRedo()}
 else if(!e.ctrlKey&&!e.metaKey&&!e.altKey&&(key==='b'||key==='e'||key==='w')){e.preventDefault();hcSetTool({b:'add',e:'erase',w:'wand'}[key])}
 else if(key==='enter'&&hcWandReadyCams().length){e.preventDefault();hcWandApply()}
 else if(key==='escape'){if(hcWandClear())e.preventDefault();else if(hcEdit.solo!==null){e.preventDefault();hcToggleSolo(hcEdit.solo)}}
});
window.addEventListener('beforeunload',e=>{if(hcEditDirty()||hcBatch.running){e.preventDefault();e.returnValue=''}});

// ---- 마법봉 일괄 적용 ----
// 한 프레임에서 점(포함/제외)과 이름으로 결과를 확인한 뒤, 같은 카메라·같은 대상(손/물체)에서 마스크가 비어 있는
// 프레임들에 같은 프롬프트를 차례로 적용하고 바로 저장합니다. 점은 화면 비율 좌표라 모든 프레임에 같은 자리로 들어갑니다.
// 마스크가 기준 px 이상 있는 프레임은 건드리지 않고(기준 미만 잡음은 새 결과로 교체), SAM3가 아무것도 찾지 못한 프레임은 건너뜁니다.
function hcPngPixels(b64,width,height){
 return hcEditImage('data:image/png;base64,'+b64).then(im=>{
  const c=document.createElement('canvas');c.width=width||im.width;c.height=height||im.height;
  const ctx=c.getContext('2d',{willReadFrequently:true});ctx.drawImage(im,0,0,c.width,c.height);
  return {canvas:c,ctx,px:ctx.getImageData(0,0,c.width,c.height)};
 });
}
// 마스크 픽셀 수. 화면에서는 비어 보여도 SAM3 잡음으로 몇 px가 남은 프레임이 많아 '빈 마스크'는 기준 px 미만으로 판단합니다.
async function hcPngArea(b64){const {px}=await hcPngPixels(b64);let n=0;for(let i=0;i<px.data.length;i+=4)if(px.data[i]>0)n++;return n}
// 기본 64px: 추적 자동 QC(tracking_qc_policy.json의 minimum_mask_pixels)가 '대상 없음'으로 보는 기준과 같습니다.
function hcBatchMinPx(){const v=Math.round(Number($('hcBatchMinPx').value));return Number.isFinite(v)&&v>=1?v:64}
// 마법봉 결과 중 다른 대상(손↔물체)의 기존 마스크 안에 들어가는 비율. 크면 다른 대상을 잡은 것으로 봅니다.
async function hcOverlapRatio(resultB64,otherB64,width,height){
 const [a,b]=await Promise.all([hcPngPixels(resultB64,width,height),hcPngPixels(otherB64,width,height)]);
 let area=0,inside=0;
 for(let i=0;i<a.px.data.length;i+=4)if(a.px.data[i]>127){area++;if(b.px.data[i]>0)inside++}
 return area?inside/area:0;
}
const HC_BATCH_MAX_OVERLAP=0.5;
// 마법봉 결과를 원본 해상도의 0/255 불투명 PNG로 바꿉니다(저장 API 형식).
async function hcBinaryPng(b64,width,height){
 const {canvas,ctx,px}=await hcPngPixels(b64,width,height);
 for(let i=0;i<px.data.length;i+=4){const v=px.data[i]>127?255:0;px.data[i]=px.data[i+1]=px.data[i+2]=v;px.data[i+3]=255}
 ctx.putImageData(px,0,0);return canvas.toDataURL('image/png').split(',')[1];
}
function hcBatchRange(){
 const n=hcMeta.frames,num=id=>{const v=$(id).value.trim();return v===''?NaN:Math.round(Number(v))};
 let a=num('hcBatchFrom'),b=num('hcBatchTo');
 if(!Number.isFinite(a)||a<1)a=1;if(!Number.isFinite(b)||b>n)b=n;if(a>n)a=n;if(b<1)b=1;if(a>b)[a,b]=[b,a];
 return [a-1,b-1];
}
async function hcBatchRun(){
 if(hcBatch.running||hcEdit.saving||hcBusy)return;
 const blocked=hcEditBlocked();if(blocked){hcWandMessage(blocked,true);return}
 const e=hcWandEntry();
 if(!e?.wand?.points.some(x=>x[2]===1)){hcWandMessage('먼저 기준이 될 카메라·프레임에서 마법봉 점을 찍어 결과를 확인하세요. 그 점으로 일괄 적용합니다.',true);return}
 if(e.wand.busy){hcWandMessage('분할이 끝난 뒤 일괄 적용할 수 있습니다.',true);return}
 // 저장하지 않은 수정을 저장하면 프레임을 다시 불러와 점이 지워지므로, 프롬프트를 먼저 복사해 둡니다.
 const {cam,points,threshold}=hcWandRequestBody(e),obj=e.obj,path=hcPath,token=hcToken,episode=hcMeta.episode;
 const [from,to]=hcBatchRange(),minPx=hcBatchMinPx(),objName=obj===0?'물체':'손',otherName=obj===0?'손':'물체';
 // 손·물체 구분: 일괄 적용은 '이름으로 찾기'를 반드시 씁니다. 클릭만으로는 점 아래 무엇이든(손이면 천도) 잡히기 때문입니다.
 // 이름은 대상별 이름(손 = hand, 물체 = 이 에피소드의 SAM3 프롬프트)을 씁니다.
 const text=String(hcEdit.names[obj]||(obj===1?'hand':'')).trim();
 if(!text){hcWandMessage(objName+' 이름을 물체 이름 칸에 입력한 뒤 일괄 적용하세요. 일괄 적용은 이름으로 대상을 찾은 결과만 저장합니다.',true);return}
 if(!confirm('카메라 '+(cam+1)+' · '+objName+' 마스크\n프레임 '+(from+1)+'~'+(to+1)+' 중 마스크가 비어 있는(없거나 '+minPx+'px 미만) 프레임에, 현재 마법봉 점 '+points.length+'개'+(text?' + 이름 "'+text+'"':'')+'로 SAM3 분할을 일괄 적용하고 바로 저장합니다.\n\n· "'+text+'"(으)로 찾은 결과만 저장합니다. 이름으로 못 찾아 클릭만으로 분할된 프레임, 결과가 '+otherName+' 마스크와 대부분 겹치는 프레임은 건너뜁니다.\n· '+minPx+'px 미만으로 남은 잡음은 새 결과로 바꿉니다. 그 이상 마스크가 있는 프레임은 바꾸지 않습니다.\n· 점은 모든 프레임에 같은 화면 위치로 들어갑니다. 물체가 크게 움직인 프레임은 결과를 꼭 확인하세요.'+(hcApproved()?'\n· 저장하면 기존 승인이 해제되고 다시 검수해야 합니다.':'')))return;
 if(!await hcEditResolve('일괄 적용'))return;
 if(!hcMeta||hcMeta.episode!==episode||hcBatch.running)return;
 hcBatch.running=true;hcBatch.stop=false;hcStop();hcEditReset();hcEdit.saving=true;hcEditControls();
 const headers={'Content-Type':'application/json','X-CSRF-Token':token},targets=[],failed=[];let saved=0,notFound=0,changed=0,clickOnly=0,otherTarget=0;
 try{
  // 1) 범위 안에서 마스크가 비어 있거나 없는 프레임 찾기
  for(let f=from;f<=to&&!hcBatch.stop;f++){
   hcWandMessage('일괄 적용 · 카메라 '+(cam+1)+' 빈 '+objName+' 마스크 찾는 중 '+(f-from+1)+'/'+(to-from+1));
   const d=await hcRequest(path+'/edit?'+new URLSearchParams({cam,obj,frame:f}));
   if(d.missing||await hcPngArea(d.mask_png)<minPx)targets.push(f);
  }
  // 2) 프레임마다 SAM3 분할 → 저장 직전에 다시 비어 있는지 확인 → 저장
  for(const [i,f] of targets.entries()){
   if(hcBatch.stop)break;
   hcWandMessage('일괄 적용 · 카메라 '+(cam+1)+' 프레임 '+(f+1)+' 분할 중 ('+(i+1)+'/'+targets.length+') · 저장 '+saved+(hcEdit.wandLoaded?'':' · 처음 한 번은 모델을 불러오느라 10~20초 걸립니다'));
   try{
    const r=await hcRequest(path+'/wand',{method:'POST',headers,body:JSON.stringify({cam,frame:f,points,text,threshold})});
    if(hcBatch.stop)break;hcEdit.wandLoaded=true;
    if(!r.area){notFound++;continue}
    // 이름으로 찾지 못해 클릭만으로 분할한 결과는 다른 대상일 수 있으므로 저장하지 않습니다.
    if(!String(r.method||'').startsWith('text')){clickOnly++;continue}
    const [fresh,other]=await Promise.all([hcRequest(path+'/edit?'+new URLSearchParams({cam,obj,frame:f})),hcRequest(path+'/edit?'+new URLSearchParams({cam,obj:1-obj,frame:f}))]);
    // 결과가 다른 대상(손↔물체)의 기존 마스크와 대부분 겹치면 그 대상을 잡은 것이므로 건너뜁니다.
    if(!other.missing&&await hcOverlapRatio(r.mask_png,other.mask_png,fresh.width,fresh.height)>HC_BATCH_MAX_OVERLAP){otherTarget++;continue}
    if(!(fresh.missing||await hcPngArea(fresh.mask_png)<minPx)){changed++;continue}  // 그 사이 다른 곳에서 수정됨
    const png=await hcBinaryPng(r.mask_png,fresh.width,fresh.height);
    const res=await hcRequest(path+'/edit',{method:'POST',headers,body:JSON.stringify({cam,obj,frame:f,basis:fresh.basis,file_sha256:fresh.file_sha256,mask_png:png})});
    if(res.changed===false)notFound++;else saved++;
   }catch(err){failed.push((f+1)+'('+err.message.slice(0,60)+')')}
  }
 }catch(err){failed.push(err.message)}
 finally{hcBatch.running=false;hcEdit.saving=false}
 // 3) 결과 반영: 승인 상태와 현재 프레임 이미지를 새로 받습니다.
 if(hcMeta&&hcMeta.episode===episode){
  try{const meta=await hcRequest(path);if(meta&&hcMeta&&meta.episode===episode)hcMeta=meta}catch(_){}
  hcImageRev++;await hcShowFrame(hcFrame,true);hcEnable();if(typeof loadJobs==='function')loadJobs();
 }
 hcEditControls();
 const summary='일괄 적용 '+(hcBatch.stop?'중지':'완료')+' · 카메라 '+(cam+1)+' '+objName+' · 빈 프레임('+minPx+'px 미만) '+targets.length+'개 중 저장 '+saved+'개'+
  (notFound?' · SAM3 미검출 '+notFound+'개':'')+(clickOnly?' · "'+text+'"(으)로 못 찾아 건너뜀 '+clickOnly+'개':'')+(otherTarget?' · '+otherName+'과 겹쳐 건너뜀 '+otherTarget+'개':'')+(changed?' · 그 사이 수정돼 건너뜀 '+changed+'개':'')+(failed.length?' · 실패: '+failed.join(', '):'')+
  (targets.length?'':' (범위 안에 빈 마스크 프레임이 없습니다)');
 hcWandMessage(summary,failed.length>0);hcBatch.stop=false;
}

// ---- 빈 마스크 프레임으로 이동 ----
// 현재 대상(손/물체) 마스크가 4개 카메라 중 하나라도 비어 있는(파일 없음 또는 '빈 마스크 기준' px 미만) 프레임을
// 현재 프레임 다음(또는 이전)부터 찾아 그 프레임으로 이동합니다. 조회한 픽셀 수는 저장 전까지 기억해 다시 찾을 때 빠릅니다.
function hcEmptyNavControls(){
 const off=hcEmptyNav.running||hcBatch.running||!hcMeta||hcStage!=='mask';
 $('hcEmptyPrev').disabled=off;$('hcEmptyNext').disabled=off;$('hcEmptyStop').hidden=!hcEmptyNav.running;
}
async function hcMaskArea(cam,obj,frame){
 // 저장할 때마다 hcImageRev가 올라가므로 키에 넣어 저장 후에는 새로 조회합니다.
 const key=[hcPath,hcImageRev,obj,cam,frame].join('|');
 if(hcEmptyNav.cache.has(key))return hcEmptyNav.cache.get(key);
 const d=await hcRequest(hcPath+'/edit?'+new URLSearchParams({cam,obj,frame}));
 const area=d.missing?-1:await hcPngArea(d.mask_png);
 if(hcEmptyNav.cache.size>4000)hcEmptyNav.cache.clear();
 hcEmptyNav.cache.set(key,area);return area;
}
async function hcGoEmpty(step){
 if(hcEmptyNav.running||hcBatch.running||!hcMeta||hcStage!=='mask')return;
 if(hcEditDirty()&&!await hcEditResolve('이동'))return;
 const obj=Number($('hcEditObj').value),objName=obj===0?'물체':'손',minPx=hcBatchMinPx(),episode=hcMeta.episode,start=hcFrame,n=hcMeta.frames;
 hcEmptyNav.running=true;hcEmptyNav.stop=false;hcStop();hcEmptyNavControls();
 let found=null,checked=0;
 try{
  for(let f=start+step;f>=0&&f<n&&!hcEmptyNav.stop;f+=step){
   if(!hcMeta||hcMeta.episode!==episode)return;
   hcStatus(objName+' 빈 마스크 찾는 중… 프레임 '+(f+1)+' / '+n+(step>0?' (다음 방향)':' (이전 방향)'));
   const empty=[];
   for(let cam=0;cam<4;cam++){const a=await hcMaskArea(cam,obj,f);if(a<minPx)empty.push({cam,a})}
   checked++;
   if(empty.length){found={frame:f,empty};break}
  }
 }catch(err){hcStatus('빈 마스크를 찾지 못했습니다: '+err.message,true);return}
 finally{hcEmptyNav.running=false;hcEmptyNavControls()}
 if(hcEmptyNav.stop){hcStatus('빈 마스크 찾기를 중지했습니다.');return}
 if(!found){hcStatus('프레임 '+(start+1)+' '+(step>0?'다음':'이전')+'에는 '+objName+' 빈 마스크(기준 '+minPx+'px 미만)가 없습니다.');return}
 await hcShowFrame(found.frame);
 if(hcFrame===found.frame)hcStatus('프레임 '+(found.frame+1)+' · '+objName+' 마스크가 비어 있는 카메라: '+found.empty.map(x=>'카메라 '+(x.cam+1)+(x.a<0?'(파일 없음)':'('+x.a+'px)')).join(', '));
}
$('hcEmptyPrev').onclick=()=>hcGoEmpty(-1);$('hcEmptyNext').onclick=()=>hcGoEmpty(1);
$('hcEmptyStop').onclick=()=>{hcEmptyNav.stop=true};

// ---- 승인 거부(누락·빈 마스크) 위치로 이동 ----
// 서버 메시지 끝의 [빈마스크:카메라,대상,프레임;...](0부터)를 읽습니다.
function hcParseMaskSpots(text){
 const m=/\[빈마스크:([^\]]*)\]/.exec(String(text||''));if(!m)return [];
 return m[1].split(';').map(x=>x.split(',').map(Number)).filter(x=>x.length===3&&x.every(Number.isInteger)).map(([cam,obj,frame])=>({cam,obj,frame}));
}
let hcMaskSpots=[],hcMaskSpotIndex=0;
function hcSpotLabel(s){return '카메라 '+(s.cam+1)+' · '+(s.obj===0?'물체':'손')+' · 프레임 '+(s.frame+1)}
// 대상(손/물체)을 위치에 맞추고, 원본 + 마스크 표시로 그 프레임을 연 뒤 해당 카메라를 크게 보여 줍니다.
async function hcGoMaskSpot(spots,index){
 hcMaskSpots=spots;hcMaskSpotIndex=Math.max(0,Math.min(spots.length-1,index));const s=spots[hcMaskSpotIndex];
 hcRenderMaskSpots();
 if(!s||!hcMeta||hcStage!=='mask')return;
 if(hcBatch.running){hcStatus('마법봉 일괄 적용이 끝난 뒤 이동할 수 있습니다.',true);return}
 if(hcEditDirty()&&!await hcEditResolve('이동'))return;
 if($('hcEditObj').value!==String(s.obj)){$('hcEditObj').value=hcEditObjValue=String(s.obj);hcEditReset();hcWandSyncName()}
 $('hcMaskMode').value='overlay';
 await hcShowFrame(s.frame,true);
 if(hcFrame!==s.frame)return;
 if(hcEdit.solo!==s.cam)hcToggleSolo(s.cam);  // 다시 더블클릭하거나 Esc로 2×2로 돌아갑니다.
 hcRenderMaskSpots();
}
// 승인 결과 아래에 위치 목록을 버튼으로 보여 줍니다. 버튼을 누르면 그 카메라·대상·프레임으로 바로 이동합니다.
function hcRenderMaskSpots(){
 const box=$('hcResult'),s=hcMaskSpots[hcMaskSpotIndex];if(!s)return;
 let nav=$('hcSpotNav');
 if(!nav){nav=document.createElement('div');nav.id='hcSpotNav';nav.className='hcSpotNav';box.after(nav)}
 const shown=hcMaskSpots.slice(0,60);
 nav.innerHTML='<div class="hcSpotHead"><strong>누락·빈 마스크 '+hcMaskSpots.length+'건</strong><span>현재 '+(hcMaskSpotIndex+1)+'번째: '+escape(hcSpotLabel(s))+'</span>'+
  '<button class="primary" data-go="'+hcMaskSpotIndex+'">이 위치로 이동</button><button class="smallbtn" data-go="'+(hcMaskSpotIndex-1)+'"'+(hcMaskSpotIndex?'':' disabled')+'>◀ 이전</button><button class="smallbtn" data-go="'+(hcMaskSpotIndex+1)+'"'+(hcMaskSpotIndex<hcMaskSpots.length-1?'':' disabled')+'>다음 ▶</button></div>'+
  '<div class="hcSpotList">'+shown.map((x,i)=>'<button class="smallbtn'+(i===hcMaskSpotIndex?' current':'')+'" data-go="'+i+'">'+escape(hcSpotLabel(x))+'</button>').join('')+(hcMaskSpots.length>shown.length?'<span class="help">외 '+(hcMaskSpots.length-shown.length)+'건</span>':'')+'</div>'+
  '<p class="help">비어 있는 게 맞으면(예: 손에 가려 물체가 보이지 않음) 검수 의견에 사유를 적고 승인하세요. 아니면 브러시·마법봉으로 칠하고 저장하세요.</p>';
 nav.querySelectorAll('[data-go]').forEach(b=>b.onclick=()=>hcGoMaskSpot(hcMaskSpots,Number(b.dataset.go)));
}
