let outputRoot='project',outputPath='',outputListing=null,outputSeq=0,outputRoots=[],outputRootsReady=false;
function outputUrl(action,values={}){return '/api/outputs/'+action+'?'+new URLSearchParams({worker_id:action.startsWith('preview')?selectedWorker():($('outputWorker').value||selectedWorker()),...values})}
const fileSize=n=>n==null?'—':n<1024?n+' B':n<1048576?(n/1024).toFixed(1)+' KB':n<1073741824?(n/1048576).toFixed(1)+' MB':(n/1073741824).toFixed(2)+' GB';
const fileDate=t=>new Date(t*1000).toLocaleString('ko-KR',{timeZone:'Asia/Seoul'});
let outputCatalog=null;
async function loadOutputCatalog(){
 const wid=$('outputWorker').value;const tree=$('outputTree');tree.textContent='서버 결과 폴더를 읽는 중…';outputCatalog=null;
 try{const [data]=await Promise.all([api(outputUrl('catalog',{root:outputRoot})),loadHidden('outputs')]);if(wid!==$('outputWorker').value)return;$('dateBasis').textContent=data.date_basis+' 기준 · 생성된 결과만 표시';
 outputCatalog=data;renderOutputTree();
 }catch(e){tree.textContent=e.message}
}
// 에피소드 폴더마다 NAS에 올리기 · 강제 종료 · 목록에서 삭제(숨김) 버튼 (파이프라인 결과에서만)
function renderOutputTree(){
 const data=outputCatalog,tree=$('outputTree');if(!data)return;
 const manage=outputRoot==='project',first=!tree.querySelector('details');
 const open=new Set([...tree.querySelectorAll('details[open]')].map(d=>d.dataset.k));
 let hiddenCount=0;
 const groups=data.groups.map(g=>({...g,episodes:g.episodes.filter(ep=>{const h=manage&&isHidden('outputs',ep.episode);if(h)hiddenCount++;return !h||showHidden.outputs})})).filter(g=>g.episodes.length);
 tree.innerHTML=(manage&&(hiddenCount||showHidden.outputs)?'<p>'+hiddenToggleHtml('outputs',hiddenCount)+'</p>':'')+(groups.map((group,i)=>'<details data-k="'+escape('d:'+group.date)+'" '+((first?i===0:open.has('d:'+group.date))?'open':'')+'><summary>📁 '+escape(group.date)+'</summary>'+group.episodes.map(ep=>{const hidden=manage&&isHidden('outputs',ep.episode);
  return '<details class="episodeFolder'+(hidden?' hiddenRow':'')+'" data-k="'+escape('e:'+ep.episode)+'" '+(open.has('e:'+ep.episode)?'open':'')+'><summary>📁 '+escape(ep.episode)+'</summary>'+(manage?'<div class="outputEpActions">'+episodeActionsHtml({episode:ep.episode,scope:'outputs',key:ep.episode,active:null,hidden})+'</div>':'')+ep.folders.map(f=>'<button class="folderLink" data-root="'+escape(f.root)+'" data-path="'+escape(f.path)+'">📁 '+escape(f.label)+'</button>').join('')+'</details>'}).join('')+'</details>').join('')||'<p class="help">'+(hiddenCount?'모든 결과를 목록에서 숨겼습니다.':'아직 생성된 결과가 없습니다.')+'</p>');
 tree.querySelectorAll('[data-path]').forEach(button=>button.onclick=()=>browseOutputs(button.dataset.path,button.dataset.root));
 if(manage)bindEpisodeActions(tree,renderOutputTree);
}
async function browseOutputs(path='',root=outputRoot){
 const seq=++outputSeq;$('outputRows').innerHTML='<tr><td colspan="4">파일 목록을 불러오는 중…</td></tr>';$('outputPreview').innerHTML='<p class="help">파일을 클릭하면 여기에서 확인할 수 있습니다.</p>';
 try{const data=await api(outputUrl('list',{root,path}));if(seq!==outputSeq)return;outputListing=data;outputPath=data.path;outputRoot=root;
 const parts=data.path?data.path.split('/'):[];
 $('outputBreadcrumb').innerHTML='<button class="crumb" data-path="">결과 저장소</button>'+parts.map((name,i)=>'<span> › </span><button class="crumb" data-path="'+escape(parts.slice(0,i+1).join('/'))+'">'+escape(name)+'</button>').join('');
 $('outputBreadcrumb').querySelectorAll('button').forEach(b=>b.onclick=()=>browseOutputs(b.dataset.path));
 $('outputCount').textContent=data.total+'개 항목'+(data.truncated?' · 앞 2,000개 표시':'');
 $('outputRows').innerHTML=data.entries.map((file,i)=>'<tr class="fileRow"><td><button class="fileButton" data-index="'+i+'" '+(!file.accessible?'disabled':'')+'>'+({folder:'📁',image:'▧',video:'▶',text:'▤',pdf:'▤',binary:'◇'}[file.kind])+' '+escape(file.name)+'</button>'+(!file.accessible?'<small> · 외부 링크</small>':'')+'</td><td>'+escape(fileDate(file.modified))+'</td><td>'+fileSize(file.size)+'</td></tr>').join('')||'<tr><td colspan="3">이 폴더는 비어 있습니다.</td></tr>';
 $('outputRows').querySelectorAll('button').forEach(b=>b.onclick=()=>{const f=data.entries[+b.dataset.index];if(f.directory)browseOutputs(f.path);else previewOutput(f)});
 }catch(e){$('outputRows').innerHTML='<tr><td colspan="3">'+escape(e.message)+'</td></tr>'}
}
async function previewOutput(file){
 const box=$('outputPreview'),root=outputRoot;
 box.scrollIntoView({behavior:'smooth',block:'start'});
 const url=outputUrl('file',{root,path:file.path});const download=outputUrl('file',{root,path:file.path,download:'true'});
 box.innerHTML='<div class="previewHeader"><div><strong>'+escape(file.name)+'</strong><p class="help">'+fileSize(file.size)+' · '+escape(fileDate(file.modified))+'</p></div><a class="smallbtn" href="'+escape(download)+'">다운로드</a></div>';
 if(file.kind==='image')box.innerHTML+='<a href="'+escape(url)+'" target="_blank" rel="noopener"><img class="outputImage" alt="'+escape(file.name)+'" src="'+escape(url)+'"></a>';
 else if(file.kind==='video')box.innerHTML+='<video controls preload="metadata" src="'+escape(url)+'"></video>';
 else if(file.kind==='pdf')box.innerHTML+='<iframe title="PDF 결과" src="'+escape(url)+'"></iframe>';
 else if(file.kind==='text'){
  const pre=document.createElement('pre');pre.textContent='내용을 읽는 중…';box.appendChild(pre);
  try{const data=await api(outputUrl('text',{root,path:file.path}));pre.textContent=data.text;if(data.truncated)pre.textContent+='\n\n… 미리보기는 256KB까지 표시합니다. 전체 내용은 다운로드하세요.'}catch(e){pre.textContent=e.message}
 }else box.innerHTML+='<p class="help">'+escape(file.name.split('.').pop().toUpperCase())+' 데이터 파일입니다. 파일명·크기·위치를 확인하거나 다운로드할 수 있습니다.</p>';
}
$('outputRoot').onchange=async()=>{outputRoot=$('outputRoot').value;outputPath='';outputLocation();try{await api('/api/output-roots/select?worker_id='+encodeURIComponent($('outputWorker').value),{method:'POST',body:JSON.stringify({root:outputRoot})})}catch(e){notice(e.message,true)}loadOutputCatalog();browseOutputs('')};
$('refreshOutputs').onclick=()=>openOutputExplorer();

// Episode selection must remain visible while checking the SAM3 target.
let previewEpisode='',previewLoaded=new Set(),previewGeneration=0;
let launchError='',launchErrorFor='';
function updateLaunch(){
 const mode='pipeline';
 const selected=rawEpisodes.find(e=>e.episode===$('episode').value);
 $('episodeOwner').textContent=!selected?'RAW 표에서 에피소드를 선택하세요.':selected?.operator?(selected.can_run?'내가 선택한 데이터입니다.':selected.operator+' 작업자가 진행 중인 데이터입니다. 이미지 확인만 가능하며 중복 실행할 수 없습니다.'):selected?.assigned_to?(selected.can_run?'나에게 배정된 데이터입니다.':selected.assigned_to+' 작업자에게 배정된 데이터입니다. 이미지 확인만 가능합니다.'):(selected?.can_run?'아직 배정되지 않은 데이터입니다.':'아직 배정되지 않은 데이터입니다. 나에게 배정된 데이터만 실행할 수 있습니다.');
 const ready=mode!=='pipeline'||(previewEpisode===$('episode').value&&previewLoaded.size===2&&$('confirmed').checked&&$('prompt').value.trim()&&objectModeReady());
 $('launch').disabled=busy||!connected||!ready||(mode!=='test'&&(!$('episode').value||selected?.can_run===false));
 // 버튼이 왜 눌리지 않는지, 마지막 실행 요청이 왜 거절됐는지 버튼 바로 아래에 보여 줍니다.
 const ep=$('episode').value;if(launchErrorFor!==ep){launchError='';launchErrorFor=ep}
 const reason=busy?'실행 요청 중…':!connected?'서버 연결을 확인하는 중입니다. 잠시 후 다시 시도하세요.':!ep?'RAW 표에서 에피소드를 선택하세요.':selected?.can_run===false?'나에게 배정된 데이터가 아니라 실행할 수 없습니다.'
  :previewEpisode!==ep||previewLoaded.size!==2?'사진 2장을 불러온 뒤 실행할 수 있습니다.':!$('prompt').value.trim()?'SAM3 분할 대상을 입력하세요.':!objectModeReady()?objectModeHint():!$('confirmed').checked?'"사진과 분할 대상을 확인했습니다"를 체크하세요.':'';
 const hint=$('launchHint');hint.textContent=launchError?'실행 거절: '+launchError:reason;hint.classList.toggle('error',!!launchError);
}
async function loadEpisodePreview(){
 loadBboxPicker();
 const episode=$('episode').value;const generation=++previewGeneration;previewEpisode='';previewLoaded=new Set();$('confirmed').checked=false;$('confirmed').disabled=true;updateLaunch();
 if(!episode){$('episodeImages').textContent='에피소드를 선택하세요.';return}
 $('episodeImages').textContent='이미지를 불러오는 중…';
 try{
  const info=await api(outputUrl('preview-info',{episode}));if(generation!==previewGeneration)return;
  $('previewEpisodeName').textContent=episode;$('previewSource').textContent=info.label+(info.frames?' · '+info.frames+'프레임':'');
  $('episodeImages').innerHTML=info.cameras.slice(0,2).map((cam,i)=>'<figure><a target="_blank" rel="noopener"><img alt="사진 '+(i+1)+'" data-cam="'+cam+'"></a><figcaption>사진 '+(i+1)+'</figcaption></figure>').join('');
  for(const img of $('episodeImages').querySelectorAll('img')){
   const cam=+img.dataset.cam;const url=outputUrl('preview',{episode,cam,position:0.5});img.parentNode.href=url;
   img.onload=()=>{if(generation!==previewGeneration)return;previewLoaded.add(cam);if(previewLoaded.size===2){previewEpisode=episode;$('confirmed').disabled=false;$('previewStatus').textContent='사진 2장을 보고 분할 대상을 입력하세요.'}updateLaunch()};
   img.onerror=()=>{if(generation!==previewGeneration)return;$('previewStatus').textContent='일부 이미지를 불러오지 못했습니다. 다시 불러오기를 눌러주세요.';updateLaunch()};
   img.src=url;
  }
  $('previewStatus').textContent='사진 2장을 불러오는 중…';
 }catch(e){if(generation!==previewGeneration)return;$('episodeImages').textContent=e.message;$('previewStatus').textContent='이미지를 확인한 뒤 전체 파이프라인을 실행할 수 있습니다.'}
}
$('episode').onchange=()=>{$('prompt').value='';loadEpisodePreview()};
$('refreshPreview').onclick=loadEpisodePreview;
$('confirmed').onchange=updateLaunch;$('prompt').oninput=updateLaunch;

// 선택 옵션: 첫 프레임 bbox 또는 포인트 + 텍스트 → 단일 물체 추적. RAW cam_1~4 첫 프레임에 그린 bbox(x,y,w,h)나
// 클릭한 포인트([x,y,label], label 1=포함 0=제외)를 0~1로 정규화해 보냅니다.
let objectBoxes={},objectPoints={},handPoints={},pickTarget='object',bboxLoaded=new Set(),bboxEpisode='',bboxGeneration=0;
// 분할 방식(선택 상자 값): text / bbox_track / point_track(물체 포인트 + 손 텍스트) / point_track_hand(물체 포인트 + 손 포인트).
// 서버에는 물체 방식(sam_object_mode)과 손 방식(sam_hand_mode)으로 나눠 보냅니다.
function samMode(){return $('objectMode').value}
function handMode(){return samMode()==='point_track_hand'}
function objectMode(){return handMode()?'point_track':samMode()}
function camPointsReady(c){return (objectPoints[c]||[]).some(p=>p[2]===1)}
function camHandReady(c){return (handPoints[c]||[]).some(p=>p[2]===1)}
function objectModeReady(){
 const m=objectMode();if(m==='text')return true;if(bboxEpisode!==$('episode').value)return false;
 return [1,2,3,4].every(c=>(m==='bbox_track'?objectBoxes[c]:camPointsReady(c))&&(!handMode()||camHandReady(c)));
}
function objectModeHint(){return handMode()?'카메라 4개 모두 첫 프레임에서 물체와 손을 각각 클릭하세요(포함 포인트 1개 이상씩).':objectMode()==='point_track'?'카메라 4개 모두 첫 프레임에서 물체를 클릭하세요(포함 포인트 1개 이상).':'카메라 4개 모두 첫 프레임에 bbox를 그리세요.'}
function objectBboxPayload(){
 if(objectMode()!=='bbox_track')return null;
 const result={};for(const c of [1,2,3,4])result[String(c-1)]=objectBoxes[c].map(v=>Math.round(v*10000)/10000);return result;
}
function pointPayload(store){const result={};for(const c of [1,2,3,4])result[String(c-1)]=store[c].map(([x,y,l])=>[Math.round(x*10000)/10000,Math.round(y*10000)/10000,l]);return result}
function objectPointPayload(){return objectMode()==='point_track'?pointPayload(objectPoints):null}
function handPointPayload(){return handMode()?pointPayload(handPoints):null}
// 물체 포인트: 초록(포함)/빨강(제외) 원. 손 포인트: 파랑(포함)/주황(제외) 네모.
function paintBox(canvas,box,points,hands){
 const r=canvas.getBoundingClientRect(),dpr=window.devicePixelRatio||1;canvas.width=Math.round(r.width*dpr);canvas.height=Math.round(r.height*dpr);
 const ctx=canvas.getContext('2d');ctx.clearRect(0,0,canvas.width,canvas.height);
 if(box){
  const [x,y,w,h]=box;ctx.fillStyle='rgba(255,210,0,.15)';ctx.strokeStyle='#ffd200';ctx.lineWidth=2*dpr;
  ctx.fillRect(x*canvas.width,y*canvas.height,w*canvas.width,h*canvas.height);ctx.strokeRect(x*canvas.width,y*canvas.height,w*canvas.width,h*canvas.height);
 }
 const rad=6*dpr;
 const mark=(x,y,l,hand)=>{
  const cx=x*canvas.width,cy=y*canvas.height;
  ctx.beginPath();if(hand)ctx.rect(cx-rad,cy-rad,rad*2,rad*2);else ctx.arc(cx,cy,rad,0,Math.PI*2);
  ctx.fillStyle=hand?(l?'#3b82f6':'#f97316'):(l?'#22c55e':'#ef4444');ctx.fill();ctx.lineWidth=2*dpr;ctx.strokeStyle='white';ctx.stroke();
  ctx.beginPath();ctx.moveTo(cx-rad*.55,cy);ctx.lineTo(cx+rad*.55,cy);if(l){ctx.moveTo(cx,cy-rad*.55);ctx.lineTo(cx,cy+rad*.55)}ctx.stroke();
 };
 for(const [x,y,l] of points||[])mark(x,y,l,false);
 for(const [x,y,l] of hands||[])mark(x,y,l,true);
}
function paintCam(canvas){const c=+canvas.dataset.cam;paintBox(canvas,objectMode()==='bbox_track'?objectBoxes[c]:null,objectMode()==='point_track'?objectPoints[c]:null,handMode()?handPoints[c]:null)}
function bboxStatus(){
 const point=objectMode()==='point_track',hand=handMode(),done=[1,2,3,4].filter(c=>point?camPointsReady(c):objectBoxes[c]).length,handDone=[1,2,3,4].filter(camHandReady).length,word=point?'포인트':'bbox';
 $('bboxStatus').textContent=bboxLoaded.size<4?'첫 프레임 사진을 불러오는 중…'
  :hand&&(done<4||handDone<4)?'물체 '+done+'/4 · 손 '+handDone+'/4 · 카메라마다 물체와 손을 각각 클릭해야 실행할 수 있습니다. 지금 찍는 대상: '+(pickTarget==='hand'?'손':'물체')+' (H 키로 전환)'
  :done<4?word+' '+done+'/4 · 남은 카메라에도 '+(point?'물체를 클릭해야':'그려야')+' 실행할 수 있습니다.'
  :hand?'물체 4/4 · 손 4/4 완료. 손은 텍스트 분할 없이 클릭한 손을 추적합니다. 결과는 마스크 검수에서 확인합니다.'
  :word+' 4/4 완료. SAM3에서 카메라별 초기 마스크를 고른 뒤 그 물체만 추적합니다. 결과는 마스크 검수에서 확인합니다.';
 updateLaunch();
}
function pointCount(pts){const pos=pts.filter(p=>p[2]===1).length;return '포함 '+pos+' · 제외 '+(pts.length-pos)}
function pointLabel(c){
 const pts=objectPoints[c]||[],hands=handPoints[c]||[];
 if(!handMode())return pts.length?'<b>'+pointCount(pts)+'</b> <button type="button" class="linkbtn" data-clear="'+c+'">지우기</button>':'포인트 없음';
 return '물체 '+(pts.length?'<b>'+pointCount(pts)+'</b>':'없음')+' · 손 '+(hands.length?'<b>'+pointCount(hands)+'</b>':'없음')+(pts.length||hands.length?' <button type="button" class="linkbtn" data-clear="'+c+'">지우기</button>':'');
}
function setPickTarget(target){
 pickTarget=target==='hand'&&handMode()?'hand':'object';
 for(const b of $('pickTarget').querySelectorAll('[data-target]')){const on=b.dataset.target===pickTarget;b.classList.toggle('current',on);b.setAttribute('aria-pressed',on)}
 if(bboxLoaded.size===4)bboxStatus();
}
$('pickTarget').addEventListener('click',e=>{const b=e.target.closest('[data-target]');if(b)setPickTarget(b.dataset.target)});
// H 키: 찍는 대상(물체 ↔ 손) 전환. 입력 칸에 글을 쓰는 중에는 무시합니다.
document.addEventListener('keydown',e=>{
 if(e.key.toLowerCase()!=='h'||e.ctrlKey||e.metaKey||e.altKey||/^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName))return;
 if(page==='run'&&handMode()&&!$('bboxPicker').classList.contains('hidden'))setPickTarget(pickTarget==='hand'?'object':'hand');
});
function loadBboxPicker(){
 const episode=$('episode').value,mode=objectMode(),on=mode!=='text',point=mode==='point_track',hand=handMode(),generation=++bboxGeneration;
 objectBoxes={};objectPoints={};handPoints={};bboxLoaded=new Set();bboxEpisode='';$('bboxPicker').classList.toggle('hidden',!on);
 $('bboxHelpBox').hidden=point;$('bboxHelpPoint').hidden=!point;$('bboxHelpHand').hidden=!hand;$('pickTarget').hidden=!hand;setPickTarget('object');
 if(!on){updateLaunch();return}
 if(!episode){$('bboxImages').textContent='에피소드를 선택하세요.';$('bboxStatus').textContent='';updateLaunch();return}
 $('bboxImages').innerHTML=[1,2,3,4].map(c=>'<figure><div class="bboxStage"><img alt="카메라 '+c+' 첫 프레임" data-cam="'+c+'"><canvas data-cam="'+c+'" role="img" aria-label="카메라 '+c+' '+(hand?'물체·손 포인트 찍기':'물체 '+(point?'포인트 찍기':'bbox 그리기'))+'"></canvas></div><figcaption>카메라 '+c+' · 첫 프레임 · <span data-box="'+c+'">'+(point?'포인트 없음':'bbox 없음')+'</span></figcaption></figure>').join('');
 for(const img of $('bboxImages').querySelectorAll('img')){
  const cam=+img.dataset.cam,canvas=img.nextElementSibling,label=$('bboxImages').querySelector('[data-box="'+cam+'"]');
  img.onload=()=>{if(generation!==bboxGeneration)return;bboxLoaded.add(cam);if(bboxLoaded.size===4)bboxEpisode=episode;paintCam(canvas);bboxStatus()};
  img.onerror=()=>{if(generation!==bboxGeneration)return;$('bboxStatus').textContent='첫 프레임 사진을 불러오지 못했습니다. 다시 불러오기를 눌러주세요.';updateLaunch()};
  const at=e=>{const r=canvas.getBoundingClientRect();return [Math.min(1,Math.max(0,(e.clientX-r.left)/r.width)),Math.min(1,Math.max(0,(e.clientY-r.top)/r.height))]};
  if(point){
   // 왼쪽 클릭 = 포함, Shift+클릭 또는 오른쪽 클릭 = 제외. 대상(물체/손)마다 카메라당 최대 20개.
   const refresh=()=>{label.innerHTML=pointLabel(cam);const b=label.querySelector('[data-clear]');if(b)b.onclick=()=>{objectPoints[cam]=[];handPoints[cam]=[];refresh()};paintCam(canvas);bboxStatus()};
   canvas.oncontextmenu=e=>e.preventDefault();
   canvas.onpointerdown=e=>{
    if(!bboxLoaded.has(cam)||(e.button!==0&&e.button!==2))return;e.preventDefault();
    const onHand=hand&&pickTarget==='hand',store=onHand?handPoints:objectPoints,what=onHand?'손':'물체';
    const pts=store[cam]||(store[cam]=[]),neg=e.button===2||e.shiftKey;
    if(neg&&!pts.some(p=>p[2]===1)){$('bboxStatus').textContent='카메라 '+cam+': 먼저 '+what+' 위를 왼쪽 클릭해 포함 포인트를 찍으세요.';return}
    if(pts.length>=20){$('bboxStatus').textContent='카메라 '+cam+': '+what+' 포인트는 20개까지입니다. 지우기 후 다시 찍으세요.';return}
    const [x,y]=at(e);pts.push([x,y,neg?0:1]);refresh();
   };
   canvas.style.cursor='copy';
  }else{
   let start=null;
   const boxOf=(a,b)=>[Math.min(a[0],b[0]),Math.min(a[1],b[1]),Math.abs(a[0]-b[0]),Math.abs(a[1]-b[1])];
   canvas.onpointerdown=e=>{if(!bboxLoaded.has(cam))return;start=at(e);canvas.setPointerCapture(e.pointerId);e.preventDefault()};
   canvas.onpointermove=e=>{if(start)paintBox(canvas,boxOf(start,at(e)))};
   canvas.onpointerup=e=>{if(!start)return;const box=boxOf(start,at(e));start=null;
    if(box[2]>=0.01&&box[3]>=0.01){objectBoxes[cam]=box;label.innerHTML='<b>bbox 지정됨</b>'}
    paintCam(canvas);bboxStatus()};
   canvas.onpointercancel=()=>{start=null;paintCam(canvas)};
  }
  img.src=outputUrl('preview',{episode,cam,position:0});
 }
 bboxStatus();
}
$('objectMode').onchange=()=>{loadBboxPicker();samDefaultView()};
// ---- 기본 분할 방식(이 PC 설정): 실행 화면을 열면 미리 선택됩니다. '기본값으로 저장'은 지금 고른 방식을 이 PC에 저장합니다. ----
let samDefault=null;
function samDefaultView(){
 if(!samDefault){$('samDefaultNote').textContent='';$('samDefaultSave').hidden=true;return}
 const same=samMode()===samDefault.mode;
 $('samDefaultNote').textContent='이 PC 기본값: '+samDefault.label+(same?'':' · 지금 선택은 이번 실행에만 적용됩니다.');
 $('samDefaultSave').hidden=same;
}
async function loadSamDefault(apply){
 try{samDefault=await api('/api/sam-default')}catch(e){samDefault=null}
 if(samDefault&&apply&&$('objectMode').value!==samDefault.mode){$('objectMode').value=samDefault.mode;loadBboxPicker()}
 samDefaultView();
 if(typeof contHandView==='function')contHandView();
}
$('samDefaultSave').onclick=async()=>{
 try{samDefault=await api('/api/sam-default',{method:'POST',body:JSON.stringify({mode:samMode()})});notice('이 PC의 기본 분할 방식을 "'+samDefault.label+'"(으)로 저장했습니다.');samDefaultView();if(typeof contHandView==='function')contHandView()}
 catch(e){notice(e.message,true)}
};
window.addEventListener('resize',()=>{for(const canvas of $('bboxImages').querySelectorAll('canvas'))paintCam(canvas)});

function revealSamPreview(){ $('samPreview').classList.remove('hidden');$('prompt').setAttribute('aria-expanded','true') }
$('prompt').onfocus=revealSamPreview;$('prompt').onclick=revealSamPreview;

function outputLocation(){const item=outputRoots.find(x=>x.id===outputRoot);$('outputLocation').textContent=(registeredWorkers.find(w=>w.id===$('outputWorker').value)?.label||'선택한 PC')+'  ›  '+(item?.path||'폴더 확인 중…')}
async function refreshOutputRoots(useSaved=false){
 const data=await api(outputUrl('roots'));outputRoots=data.roots;
 if(useSaved||!outputRoots.some(x=>x.id===outputRoot))outputRoot=outputRoots.some(x=>x.id===data.selected)?data.selected:'project';
 $('outputRoot').innerHTML=outputRoots.map(item=>'<option value="'+escape(item.id)+'">'+escape(item.label)+(item.available?'':' (연결 안 됨)')+'</option>').join('');$('outputRoot').value=outputRoot;outputRootsReady=true;outputLocation();
}
async function openOutputExplorer(){try{await refreshOutputRoots(!outputRootsReady);await Promise.all([loadOutputCatalog(),browseOutputs(outputPath)])}catch(e){notice(e.message,true)}}
let chosenFolder=null,folderParent=null,folderHomePath='',folderBusy=false,folderGeneration=0;
function folderStatus(text,error=false){$('folderStatus').textContent=text;$('folderStatus').classList.toggle('error',error)}
async function showWorkerFolder(path=''){
 if(folderBusy)return;const generation=++folderGeneration;chosenFolder=null;$('folderRegister').disabled=true;$('folderChildren').textContent='폴더 목록을 읽는 중…';folderStatus('작업자 PC의 경로를 확인하는 중…');
 try{
  const data=await api(outputUrl('picker',{path}));if(generation!==folderGeneration)return;chosenFolder=data.path;folderParent=data.parent;folderHomePath=data.home;$('folderPath').value=data.path;$('folderWorker').textContent=registeredWorkers.find(w=>w.id===$('outputWorker').value)?.label||data.server;$('folderParent').disabled=!data.parent;
  $('folderChildren').innerHTML=data.folders.map((item,i)=>'<button class="folderChild" data-folder="'+i+'">📁 '+escape(item.name)+'</button>').join('')||'<p class="help" style="padding:16px">하위 폴더가 없습니다. 현재 폴더를 등록할 수 있습니다.</p>';
  $('folderChildren').querySelectorAll('button').forEach(b=>b.onclick=()=>showWorkerFolder(data.folders[+b.dataset.folder].path));
  folderStatus(data.folders.length+'개 하위 폴더 · 현재 경로를 등록하거나 하위 폴더로 이동하세요.');$('folderRegister').disabled=false;
 }catch(e){if(generation!==folderGeneration)return;folderStatus(e.message,true);$('folderChildren').textContent='경로를 확인하고 다시 이동하세요.'}
}
$('chooseOutputFolder').onclick=async()=>{
 try{await refreshOutputRoots();$('folderLabel').value='';$('folderDialog').showModal();const selected=outputRoots.find(x=>x.id===outputRoot);await showWorkerFolder(selected?.path||'')}catch(e){notice(e.message,true)}
};
$('closeFolderDialog').onclick=()=>{if(!folderBusy)$('folderDialog').close()};$('folderDialog').oncancel=e=>{if(folderBusy)e.preventDefault()};
$('folderGo').onclick=()=>showWorkerFolder($('folderPath').value);
$('folderPath').oninput=()=>{chosenFolder=null;$('folderRegister').disabled=true};$('folderPath').onkeydown=e=>{if(e.key==='Enter'){e.preventDefault();showWorkerFolder($('folderPath').value)}};
$('folderHome').onclick=()=>showWorkerFolder(folderHomePath);$('folderDesktop').onclick=()=>showWorkerFolder(folderHomePath+'/Desktop');$('folderParent').onclick=()=>{if(folderParent)showWorkerFolder(folderParent)};
$('folderCurrent').onclick=()=>showWorkerFolder(outputRoots.find(x=>x.id===outputRoot)?.path||'');
$('folderRegister').onclick=async()=>{
 if(!chosenFolder||folderBusy)return;folderBusy=true;$('folderRegister').disabled=true;folderStatus('이 PC에 폴더를 등록하는 중…');
 try{
  const data=await api('/api/output-roots?worker_id='+encodeURIComponent($('outputWorker').value),{method:'POST',body:JSON.stringify({path:chosenFolder,label:$('folderLabel').value})});outputRoot=data.selected;outputPath='';await refreshOutputRoots();$('folderDialog').close();await Promise.all([loadOutputCatalog(),browseOutputs('')]);notice('폴더를 등록했습니다. 선택한 PC의 결과를 표시합니다.');
 }catch(e){folderStatus(e.message,true)}finally{folderBusy=false;$('folderRegister').disabled=!chosenFolder}
};

let rawBoardBusy=false,rawBoardGeneration=0;
const rawExcludeBusy=new Set();
function rawCaptureDate(ep){const m=ep.match(/^ep_(\d{4})(\d{2})(\d{2})_/);return m?m[1]+'-'+m[2]+'-'+m[3]:'—'}
// 기본값은 전날 기록(오늘 11/13 → 11/12). 📅에서 다른 날짜나 '전체 보기'로 바꿀 수 있습니다.
// 기본 RAW 날짜: 어제. 오늘이 월요일이면 지난 금요일(주말 촬영 없음).
// 예외: RAW 대기열 첫날(2026-11-16)에는 전날이 아니라 당일이 기본입니다.
let rawDateFilter=(()=>{const d=new Date();if(localDateKey(d)==='2026-11-16')return '2026-11-16';d.setDate(d.getDate()-(d.getDay()===1?3:1));return localDateKey(d)})();
const rawDateCal=createDateCalendar({toggle:$('rawDateToggle'),panel:$('rawCalendar'),keepEmpty:true,
 counts:()=>{const c={};rawEpisodes.forEach(e=>{const d=rawCaptureDate(e.episode);c[d]=(c[d]||0)+1});return c},
 get:()=>rawDateFilter,set:v=>{if(v!==rawDateFilter){rawDateFilter=v;renderRawTable()}}});
let rawTeam=null;
function rawWho(e){
 const me=rawTeam&&rawTeam.me,name=e.operator||e.assigned_to;
 if(!name&&e.elsewhere)return escape(e.elsewhere)+' <span class="help">(다른 PC · '+(e.elsewhere_started?'실행 시작':'배정·시작 전')+')</span>';
 if(!name)return '<span class="help">미배정</span>';
 const label=escape(name)+(e.operator?'':' <span class="help">(배정·시작 전)</span>');
 return name===me?'<span class="rawMe">'+label+'</span>':label;
}
function renderRawTeam(){
 const t=rawTeam,el=$('rawTeam');if(!t){el.textContent='';renderQueueCtl();return}
 const tg=n=>(t.targets&&t.targets[n]!=null)?t.targets[n]:t.target;
 const ops=t.operators.map(n=>escape(n)+' '+(t.load[n]||0)+'/'+tg(n)+(n===t.me?' (나)':'')).join(' · ');
 el.innerHTML='로그인 사용자 '+t.total_users+'명 · 작업자 '+t.operators.length+'명(관리자 '+t.admins+'명 제외) · 작업자마다 각자 정한 큐 크기(기본 '+(t.default_target??t.target)+'개)만큼 자동 배정, 첫 실행(추적 검수까지)이 끝나면 남은 RAW에서 다시 채웁니다.'+(ops?'<br>'+ops:'');
 renderQueueCtl();
}
// 내 RAW 큐 크기(각자 설정): NAS에 바로 저장되어 모든 PC 화면에 같은 값이 쓰입니다.
let queueSaving=false;
function renderQueueCtl(){
 const box=$('rawQueueCtl'),t=rawTeam,q=t&&t.my_queue;
 if(!q||!/^PC[1-6]$/.test(t.me||'')){box.hidden=true;return}
 box.hidden=false;
 const input=$('rawQueueInput');
 if(document.activeElement!==input&&!queueSaving){input.value=q.target;input.min=q.min;input.max=q.max}
 $('rawQueueNote').textContent=(q.custom?'내가 정한 값':'기본값')+' · '+(q.nas?'NAS에 저장돼 모든 PC에 적용':'NAS 미연결 · 이 PC에만 저장');
}
async function saveQueueTarget(){
 const input=$('rawQueueInput'),v=Number(input.value),q=rawTeam&&rawTeam.my_queue;
 if(!q||!Number.isInteger(v)||v<q.min||v>q.max){notice('큐 크기는 '+(q?q.min:0)+'~'+(q?q.max:30)+'개 사이 정수로 입력하세요.',true);return}
 if(v===q.target&&q.custom)return;
 if(v<q.target&&!confirm('내 큐를 '+q.target+'개 → '+v+'개로 줄입니다.\n시작하지 않은 배정 중 늦은 촬영부터 풀려 다른 작업자에게 갈 수 있습니다(시작한 RAW는 그대로).\n\n바꿀까요?')){input.value=q.target;return}
 queueSaving=true;$('rawQueueSave').disabled=true;
 try{
  const r=await api('/api/queue-target',{method:'POST',body:JSON.stringify({target:v})});
  notice('내 RAW 큐를 '+r.target+'개로 정했습니다.'+(r.nas?' NAS에 저장되어 모든 PC 화면에 바로 적용됩니다.':' (NAS 미연결 · 이 PC에만 저장)'));
  if(rawTeam)rawTeam.my_queue=r;
  input.blur();await refreshRawBoard(true);
 }catch(e){notice(e.message,true)}
 finally{queueSaving=false;$('rawQueueSave').disabled=false;renderQueueCtl()}
}
$('rawQueueSave').onclick=saveQueueTarget;
$('rawQueueInput').onkeydown=e=>{if(e.key==='Enter'){e.preventDefault();saveQueueTarget()}};
// ---- 단계 바로가기: 내가 처리할 단계(마스크·추적 승인 대기, SAM3 설정 대기, 3D 복원 대기)는 누르면 그 작업 화면으로 이동 ----
const RAW_GATE_TIPS={mask:'마스크 검수 화면 열기',tracking:'추적 검수 화면 열기',sam:'SAM3 분할 대상 입력으로 이동',batch:'3D 복원 일괄 실행으로 이동'};
function rawStageCell(e,i){
 const label=e.progress||'단계 확인 중';
 if(!e.gate||!RAW_GATE_TIPS[e.gate])return stageLabel(label);
 return '<button type="button" class="badge hcTaskButton rawGate" data-raw-gate="'+i+'" title="'+RAW_GATE_TIPS[e.gate]+'">'+escape(e.gate==='sam'?'SAM3 설정 대기':label)+' →</button>';
}
function rawGateFlash(el){if(!el)return;el.scrollIntoView({behavior:'smooth',block:'center'});el.classList.add('rawGateFlash');setTimeout(()=>el.classList.remove('rawGateFlash'),2200)}
async function rawGateOpen(e){
 const ep=e.gate_episode||e.episode;
 if(e.gate==='mask'||e.gate==='tracking'){openReview(ep,e.gate);return}
 if(e.gate==='sam'){
  const find=()=>[...$('samWaitRows').querySelectorAll('[data-sam-go]')].find(b=>b.dataset.ep===ep);
  let go=find();if(!go){samWaitSnap='';await loadSamWaiting();go=find()}
  if(!go){notice('SAM3 설정 대기 목록에서 '+ep+' 을(를) 찾지 못했습니다. 잠시 뒤 새로고침하세요.',true);rawGateFlash($('samWaitRows'));return}
  const rowEl=go.closest('.samWaitRow'),input=rowEl.querySelector('[data-sam-input]');rawGateFlash(rowEl);
  if(input&&!input.disabled)input.focus({preventScroll:true});else notice(ep+' · RAW 전처리가 끝나면 분할 대상을 입력할 수 있습니다.');
  return;
 }
 if(e.gate==='batch'){show('jobs');const q=$('manoQueue');q.open=true;if(typeof loadMano==='function')loadMano();rawGateFlash(q)}
}
// ---- 사람별 손 형상(MANO β) 프로필: RAW마다 지정하면 그 에피소드의 MANO 피팅이 프로필 β로 고정됩니다(NAS control/hand_profiles) ----
let handProfiles={profiles:[],assignments:{}},handProfilesAt=0,handProfileBusy=new Set();
async function loadHandProfiles(force=false){
 if(!force&&Date.now()-handProfilesAt<15000)return;handProfilesAt=Date.now();
 try{handProfiles=await api('/api/hand-profiles'+(force?'?fresh=1':''));renderRawTable()}catch(e){}
}
function handProfileCell(e){
 const a=(handProfiles.assignments||{})[e.episode],cur=a&&a.profile||'',ok=(handProfiles.profiles||[]).filter(p=>!p.invalid);
 if(!ok.length&&!cur)return '<span class="help">—</span>';
 const can=e.can_run!==false&&!e.elsewhere&&!handProfileBusy.has(e.episode);
 const tip=a?'지정: '+(a.by||'')+' · '+String(a.at_text||'').replace('T',' ').slice(0,16):'지정하지 않으면 에피소드마다 공유 형상 보정(기존 방식)';
 if(!can)return '<span class="help" title="'+escape(tip)+'">'+escape(cur||'기본')+'</span>';
 return '<select class="handProfileSel" data-hand-profile="'+escape(e.episode)+'" title="'+escape(tip)+'" aria-label="'+escape(e.episode)+' 손 형상 프로필"><option value="">기본 (에피소드별 보정)</option>'+
  ok.map(p=>'<option value="'+escape(p.id)+'"'+(p.id===cur?' selected':'')+'>'+escape(p.id)+(p.size?' · '+p.size.length.toFixed(0)+'mm':'')+'</option>').join('')+
  (cur&&!ok.some(p=>p.id===cur)?'<option value="'+escape(cur)+'" selected>'+escape(cur)+' (없음)</option>':'')+'</select>';
}
$('rawRows').addEventListener('change',async ev=>{
 const sel=ev.target.closest('[data-hand-profile]');if(!sel)return;ev.stopPropagation();
 const ep=sel.dataset.handProfile,profile=sel.value||null;handProfileBusy.add(ep);
 try{
  const r=await api('/api/hand-profiles/assign',{method:'POST',body:JSON.stringify({episode:ep,profile})});
  notice(ep+' · 손 형상 '+(r.profile?r.profile+'(으)로 지정했습니다. 이 에피소드의 MANO 피팅은 이 프로필의 β로 고정됩니다(이미 MANO가 끝난 실행은 바뀌지 않음).':'지정을 해제했습니다(에피소드별 공유 형상 보정).'));
  await loadHandProfiles(true);
 }catch(e){notice(e.message,true)}
 finally{handProfileBusy.delete(ep);renderRawTable()}
});
setInterval(()=>{if(!document.hidden&&currentUser)loadHandProfiles()},20000);
// 작업 현황의 MANO beta 칸: 처음 열 때 도구 화면을 불러오고, 도구 화면 높이에 맞춰 늘립니다.
$('manoBeta').addEventListener('toggle',()=>{const f=$('manoBetaFrame');if($('manoBeta').open&&!f.src)f.src='/mano-beta'});
window.addEventListener('message',ev=>{if(ev.origin!==location.origin||!ev.data||typeof ev.data.manoBetaHeight!=='number')return;$('manoBetaFrame').style.height=Math.max(500,Math.min(6000,ev.data.manoBetaHeight+20))+'px'});
function renderRawTable(){
 rawDateCal.refresh();
 const filter=rawDateFilter,groups={},me=rawTeam&&rawTeam.me;
 // 내 작업(시작 전 배정 + 3D 복원이 끝나지 않은 내 담당)은 날짜 필터와 상관없이 맨 위에 고정합니다.
 // 처음 뜨는 간이 목록(/api/info)에는 done이 없습니다. 끝났는지 모르는 행은 내 작업으로 세지 않습니다(예전에는 끝난 담당까지 모두 세어 '47 / 5개'처럼 보였음).
 const isMine=e=>!e.excluded_from_mine&&!e.run_hidden&&rawTeam&&rawTeam.role!=='admin'&&(e.assigned_to===me||(e.operator===me&&e.done===false));
 const mine=[];rawEpisodes.forEach((e,i)=>{if(isMine(e))mine.push(i)});
 rawEpisodes.forEach((e,i)=>{const d=rawCaptureDate(e.episode);if((e.excluded_from_mine||e.run_hidden)&&!$('rawShowExcluded').checked)return;if(!isMine(e)&&(e.excluded_from_mine||e.run_hidden||!filter||filter===d))(groups[d]=groups[d]||[]).push(i)});
 renderRawTeam();
 const dates=Object.keys(groups).sort((a,b)=>a==='—'?1:b==='—'?-1:b.localeCompare(a));
 const row=i=>{const e=rawEpisodes[i];return '<tr data-raw-index="'+i+'" class="'+($('episode').value===e.episode?'rawSelected':'')+'"><td>'+(e.can_run?'<input type="radio" name="rawSelection" aria-label="'+escape(e.episode)+' 선택" '+($('episode').value===e.episode?'checked':'')+'>'+(rawBatchable(e)?'<input type="checkbox" class="rawPick" data-raw-pick="'+escape(e.episode)+'" title="RAW 전처리 일괄 실행에 포함" aria-label="'+escape(e.episode)+' 일괄 실행에 포함" '+(rawBatchPicked.has(e.episode)?'checked':'')+'>':''):'<span class="help">조회만</span>')+'</td><td>'+rawCaptureDate(e.episode)+'</td><td>'+escape(e.episode)+'</td><td>'+rawWho(e)+'</td><td>'+rawStageCell(e,i)+(e.excluded_from_mine?' <span class="help">· 내 작업에서 제외</span>':'')+(e.run_hidden?' <span class="help" title="내 작업 현황의 숨긴 항목 보기에서 되돌릴 수 있습니다.">· 목록에서 삭제한 실행</span>':'')+'</td>'+handProfileCell(e)+'</td><td><button class="smallbtn" data-raw-preview="'+i+'">미리보기</button>'+((e.can_exclude||e.excluded_from_mine)?' <button type="button" class="smallbtn" data-raw-exclude="'+i+'"'+(rawExcludeBusy.has(e.episode)?' disabled':'')+'>'+(e.excluded_from_mine?'제외 취소':'내 작업에서 제외')+'</button>':'')+'</td></tr>'};
 const mineHtml=mine.length?'<tr class="rawDateGroup rawMineGroup"><td colspan="7">👤 내 작업 · '+mine.length+' / '+rawTeam.target+'개 (날짜 필터와 무관)</td></tr>'+mine.map(i=>row(i).replace('<tr ','<tr data-mine="1" ')).join(''):'';
 $('rawRows').innerHTML=mineHtml+(dates.map(d=>'<tr class="rawDateGroup"><td colspan="7">📅 '+(d==='—'?'날짜 미상':escape(d))+' · '+groups[d].length+'개</td></tr>'+groups[d].map(row).join('')).join('')||('<tr><td colspan="7">'+(filter&&rawEpisodes.length?escape(filter)+' 촬영 RAW 에피소드가 없습니다. 📅에서 다른 날짜나 전체 보기를 선택하세요.':'RAW 에피소드가 없습니다.')+'</td></tr>'));
 $('rawRows').querySelectorAll('tr[data-mine]').forEach(r=>r.classList.add('rawMine'));
 rawBatchControls();$('rawRows').querySelectorAll('[data-raw-index]').forEach(row=>row.onclick=event=>{if(event.target.closest('[data-raw-exclude],[data-raw-pick],[data-raw-gate],[data-hand-profile]'))return;const e=rawEpisodes[+row.dataset.rawIndex];if($('episode').value!==e.episode){$('episode').value=e.episode;$('episode').dispatchEvent(new Event('change'));renderRawTable()}if(event.target.closest('[data-raw-preview]'))revealSamPreview()});
}
async function refreshRawBoard(force=false){
 if((rawBoardBusy&&!force)||!currentUser)return;rawBoardBusy=true;const generation=++rawBoardGeneration;
 const wid=selectedWorker();try{const d=await api('/api/raw-episodes?worker_id='+encodeURIComponent(selectedWorker()));if(generation!==rawBoardGeneration||wid!==selectedWorker())return;rawEpisodes=d.episodes;rawTeam=d.team||null;loadHandProfiles();const selected=$('episode').value;$('episode').innerHTML=rawEpisodes.map(e=>'<option value="'+escape(e.episode)+'">'+escape(e.episode)+'</option>').join('');$('episode').value=rawEpisodes.some(e=>e.episode===selected)?selected:'';renderRawTable();updateLaunch()}
 catch(e){if(generation===rawBoardGeneration)notice(e.message,true)}finally{if(generation===rawBoardGeneration)rawBoardBusy=false}
}
$('refreshRaw').onclick=()=>refreshRawBoard();
$('rawShowExcluded').onchange=renderRawTable;
$('rawRows').addEventListener('click',async event=>{
 const gate=event.target.closest('[data-raw-gate]');if(gate){const e=rawEpisodes[+gate.dataset.rawGate];if(e)rawGateOpen(e);return}
 const button=event.target.closest('[data-raw-exclude]');if(!button)return;
 event.stopPropagation();const row=rawEpisodes[+button.dataset.rawExclude];if(!row||rawExcludeBusy.has(row.episode))return;
 const excluded=!row.excluded_from_mine,wid=selectedWorker();
 if(excluded&&!confirm(row.episode+'\n\n내 작업에서 제외할까요?\n이 RAW는 나에게 다시 자동 배정되지 않습니다. 다른 작업자에게는 배정될 수 있습니다.\n원본 파일과 실행 기록은 유지되며, 다른 RAW가 있으면 빈자리를 채웁니다.'))return;
 rawExcludeBusy.add(row.episode);button.disabled=true;
 try{
  await api('/api/raw-episodes/exclude',{method:'POST',body:JSON.stringify({episode:row.episode,worker_id:wid,excluded})});
  if(wid!==selectedWorker())return;
  if(excluded&&$('episode').value===row.episode){$('episode').value='';$('episode').dispatchEvent(new Event('change'))}
  notice(excluded?'내 작업에서 제외했습니다. 제외한 항목 보기에서 취소할 수 있습니다.':'제외를 취소했습니다. 자동 배정 조건에 따라 다시 배정됩니다.');
  await refreshRawBoard(true);
 }catch(error){notice(error.message,true)}
 finally{rawExcludeBusy.delete(row.episode);renderRawTable()}
});
setInterval(()=>{if(currentUser&&page==='run'&&!document.hidden)refreshRawBoard()},15000);
document.addEventListener('visibilitychange',()=>{if(!document.hidden&&currentUser&&page==='run')refreshRawBoard()});

$('outputWorker').onchange=()=>{outputSeq++;outputPath='';outputRoot='project';outputRootsReady=false;openOutputExplorer()};

// ---- RAW 전처리 일괄 실행: 고른 RAW마다 RAW 전처리만 하고 'SAM3 설정 대기'에서 멈춥니다 ----
const rawBatchPicked=new Set();let rawBatchBusy=false;
function rawBatchable(e){return e.can_run&&!e.operator&&!e.elsewhere}  // 아직 실행하지 않은, 내가 시작할 수 있는 RAW
function rawBatchControls(){
 const ok=new Set(rawEpisodes.filter(rawBatchable).map(e=>e.episode));
 for(const ep of [...rawBatchPicked])if(!ok.has(ep))rawBatchPicked.delete(ep);
 $('rawBatchPrep').disabled=rawBatchBusy||!rawBatchPicked.size;
 $('rawBatchPrep').textContent='선택한 RAW 전처리 일괄 실행'+(rawBatchPicked.size?' ('+rawBatchPicked.size+'개)':'');
 $('rawPickMine').disabled=!ok.size;
}
$('rawRows').addEventListener('change',e=>{const b=e.target.closest('[data-raw-pick]');if(!b)return;b.checked?rawBatchPicked.add(b.dataset.rawPick):rawBatchPicked.delete(b.dataset.rawPick);rawBatchControls()});
$('rawPickMine').onclick=()=>{const me=rawTeam&&rawTeam.me;rawEpisodes.filter(e=>rawBatchable(e)&&(!me||e.assigned_to===me||rawTeam.role==='admin')).forEach(e=>rawBatchPicked.add(e.episode));renderRawTable()};
$('rawBatchPrep').onclick=async()=>{
 if(rawBatchBusy||!rawBatchPicked.size)return;
 const eps=[...rawBatchPicked].sort();
 if(!confirm('RAW '+eps.length+'개를 전처리 일괄 실행합니다.\n\n'+eps.join('\n')+'\n\n· 각 RAW마다 복사·QC·준비까지 실행하고 \'SAM3 설정 대기\'에서 멈춥니다.\n· 분할 대상은 아래 \'SAM3 설정 대기\'에서 에피소드별로 입력하면 SAM3부터 이어집니다.\n· GPU 작업은 PC의 GPU 슬롯 순서대로 진행됩니다.\n· 처리 fps: '+targetFps()+'fps (실행 설정의 \'처리 fps\'에서 바꿉니다)\n\n진행할까요?'))return;
 rawBatchBusy=true;rawBatchControls();$('rawBatchInfo').textContent='시작하는 중…';
 try{
  const r=await api('/api/run/batch-prep',{method:'POST',body:JSON.stringify({worker_id:selectedWorker(),episodes:eps,defer_mano:selectedWorker()==='gpu_4090_a'&&$('deferMano').checked,target_fps:targetFps()})});
  r.started.forEach(x=>rawBatchPicked.delete(x.raw));
  $('rawBatchInfo').textContent='시작 '+r.started.length+'개'+(r.failed.length?' · 실패 '+r.failed.length+'개':'');
  notice('RAW 전처리 일괄 실행: '+r.started.length+'개 시작'+(r.failed.length?' · 실패: '+r.failed.map(x=>x.episode+' ('+x.error+')').join(', '):''),r.failed.length>0);
  refreshRawBoard(true);loadSamWaiting();jobsSnapshot='';if(typeof loadJobs==='function')loadJobs();
 }catch(err){notice(err.message,true);$('rawBatchInfo').textContent=''}
 finally{rawBatchBusy=false;rawBatchControls()}
};

// ---- SAM3 설정 대기: 분할 대상을 입력하면 같은 실행이 SAM3부터 이어서 진행 ----
let samWaitBusy=false,samWaitSnap='';
async function loadSamWaiting(){
 if(samWaitBusy||!currentUser)return;samWaitBusy=true;
 try{
  const d=await api('/api/run/sam-waiting?worker_id='+encodeURIComponent(selectedWorker()));
  // 입력 중인 칸은 지우지 않도록, 목록이 바뀌었을 때만 다시 그립니다.
  const snap=JSON.stringify(d.runs.map(r=>[r.run_id,r.prepared]));if(snap===samWaitSnap)return;samWaitSnap=snap;
  const typed={};$('samWaitRows').querySelectorAll('[data-sam-input]').forEach(i=>typed[i.dataset.samInput]=i.value);
  $('samWaitRows').innerHTML=d.runs.length?d.runs.map(r=>'<div class="samWaitRow" data-run="'+escape(r.run_id)+'"><strong>'+escape(r.episode)+'</strong>'+(r.prepared?'':'<span class="help">전처리 중…</span>')+
   '<button type="button" class="smallbtn" data-sam-preview="'+escape(r.raw_episode)+'">미리보기</button><input type="text" maxlength="200" placeholder="SAM3 분할 대상 (예: towel)" data-sam-input="'+escape(r.run_id)+'" value="'+escape(typed[r.run_id]||'')+'"'+(r.prepared?'':' disabled')+'>'+
   '<button type="button" class="primary" data-sam-go="'+escape(r.run_id)+'" data-dag="'+escape(r.dag_id)+'" data-ep="'+escape(r.episode)+'"'+(r.prepared?'':' disabled')+'>SAM3 시작</button></div>').join(''):'<p class="help">SAM3 설정을 기다리는 실행이 없습니다.</p>';
 }catch(e){$('samWaitRows').innerHTML='<p class="help">'+escape(e.message)+'</p>';samWaitSnap=''}
 finally{samWaitBusy=false}
}
$('samWaitRefresh').onclick=()=>{samWaitSnap='';loadSamWaiting()};
$('samWaitRows').addEventListener('keydown',e=>{if(e.key==='Enter'&&e.target.matches('[data-sam-input]')){const b=$('samWaitRows').querySelector('[data-sam-go="'+CSS.escape(e.target.dataset.samInput)+'"]');if(b)b.click()}});
$('samWaitRows').addEventListener('click',async e=>{
 const pv=e.target.closest('[data-sam-preview]');
 if(pv){const ep=pv.dataset.samPreview;if($('episode').value===ep)revealSamPreview();else if([...$('episode').options].some(o=>o.value===ep)){$('episode').value=ep;$('episode').dispatchEvent(new Event('change'))}else notice('이 RAW는 실행 목록에 없어 미리보기를 열 수 없습니다.',true);return}
 const b=e.target.closest('[data-sam-go]');if(!b)return;
 const input=$('samWaitRows').querySelector('[data-sam-input="'+CSS.escape(b.dataset.samGo)+'"]'),prompt=input.value.trim();
 if(!prompt){notice('SAM3 분할 대상을 입력하세요.',true);input.focus();return}
 if(!confirm(b.dataset.ep+'\nSAM3 분할 대상: '+prompt+'\n\n이 설정으로 SAM3부터 이어서 실행할까요? (같은 실행에서는 나중에 바꿀 수 없습니다)'))return;
 b.disabled=true;
 try{await api('/api/run/sam-settings',{method:'POST',body:JSON.stringify({dag_id:b.dataset.dag,run_id:b.dataset.samGo,sam_prompt:prompt})});
  notice(b.dataset.ep+' · SAM3 분할 대상 "'+prompt+'" 저장 · SAM3를 이어서 시작합니다.');samWaitSnap='';setTimeout(loadSamWaiting,1500);jobsSnapshot='';if(typeof loadJobs==='function')loadJobs()}
 catch(err){notice(err.message,true);b.disabled=false}
});
setInterval(()=>{if(page==='run'&&!document.hidden)loadSamWaiting()},15000);
setTimeout(loadSamWaiting,2500);
