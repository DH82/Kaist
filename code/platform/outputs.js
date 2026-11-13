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
   img.onload=()=>{if(generation!==previewGeneration)return;previewLoaded.add(cam);if(previewLoaded.size===2){previewEpisode=episode;$('confirmed').disabled=false;$('previewStatus').textContent='사진 2장를 보고 분할 대상을 입력하세요.'}updateLaunch()};
   img.onerror=()=>{if(generation!==previewGeneration)return;$('previewStatus').textContent='일부 이미지를 불러오지 못했습니다. 다시 불러오기를 눌러주세요.';updateLaunch()};
   img.src=url;
  }
  $('previewStatus').textContent='사진 2장를 불러오는 중…';
 }catch(e){if(generation!==previewGeneration)return;$('episodeImages').textContent=e.message;$('previewStatus').textContent='이미지를 확인한 뒤 전체 파이프라인을 실행할 수 있습니다.'}
}
$('episode').onchange=()=>{$('prompt').value='';loadEpisodePreview()};
$('refreshPreview').onclick=loadEpisodePreview;
$('confirmed').onchange=updateLaunch;$('prompt').oninput=updateLaunch;

// 선택 옵션: 첫 프레임 bbox 또는 포인트 + 텍스트 → 단일 물체 추적. RAW cam_1~4 첫 프레임에 그린 bbox(x,y,w,h)나
// 클릭한 포인트([x,y,label], label 1=포함 0=제외)를 0~1로 정규화해 보냅니다.
let objectBoxes={},objectPoints={},bboxLoaded=new Set(),bboxEpisode='',bboxGeneration=0;
function objectMode(){return $('objectMode').value}
function camPointsReady(c){return (objectPoints[c]||[]).some(p=>p[2]===1)}
function objectModeReady(){
 const m=objectMode();if(m==='text')return true;if(bboxEpisode!==$('episode').value)return false;
 return [1,2,3,4].every(c=>m==='bbox_track'?objectBoxes[c]:camPointsReady(c));
}
function objectModeHint(){return objectMode()==='point_track'?'카메라 4개 모두 첫 프레임에서 물체를 클릭하세요(포함 포인트 1개 이상).':'카메라 4개 모두 첫 프레임에 bbox를 그리세요.'}
function objectBboxPayload(){
 if(objectMode()!=='bbox_track')return null;
 const result={};for(const c of [1,2,3,4])result[String(c-1)]=objectBoxes[c].map(v=>Math.round(v*10000)/10000);return result;
}
function objectPointPayload(){
 if(objectMode()!=='point_track')return null;
 const result={};for(const c of [1,2,3,4])result[String(c-1)]=objectPoints[c].map(([x,y,l])=>[Math.round(x*10000)/10000,Math.round(y*10000)/10000,l]);return result;
}
function paintBox(canvas,box,points){
 const r=canvas.getBoundingClientRect(),dpr=window.devicePixelRatio||1;canvas.width=Math.round(r.width*dpr);canvas.height=Math.round(r.height*dpr);
 const ctx=canvas.getContext('2d');ctx.clearRect(0,0,canvas.width,canvas.height);
 if(box){
  const [x,y,w,h]=box;ctx.fillStyle='rgba(255,210,0,.15)';ctx.strokeStyle='#ffd200';ctx.lineWidth=2*dpr;
  ctx.fillRect(x*canvas.width,y*canvas.height,w*canvas.width,h*canvas.height);ctx.strokeRect(x*canvas.width,y*canvas.height,w*canvas.width,h*canvas.height);
 }
 const rad=6*dpr;
 for(const [x,y,l] of points||[]){
  const cx=x*canvas.width,cy=y*canvas.height;
  ctx.beginPath();ctx.arc(cx,cy,rad,0,Math.PI*2);ctx.fillStyle=l?'#22c55e':'#ef4444';ctx.fill();ctx.lineWidth=2*dpr;ctx.strokeStyle='white';ctx.stroke();
  ctx.beginPath();ctx.moveTo(cx-rad*.55,cy);ctx.lineTo(cx+rad*.55,cy);if(l){ctx.moveTo(cx,cy-rad*.55);ctx.lineTo(cx,cy+rad*.55)}ctx.stroke();
 }
}
function paintCam(canvas){const c=+canvas.dataset.cam;paintBox(canvas,objectMode()==='bbox_track'?objectBoxes[c]:null,objectMode()==='point_track'?objectPoints[c]:null)}
function bboxStatus(){
 const point=objectMode()==='point_track',done=[1,2,3,4].filter(c=>point?camPointsReady(c):objectBoxes[c]).length,word=point?'포인트':'bbox';
 $('bboxStatus').textContent=bboxLoaded.size<4?'첫 프레임 사진을 불러오는 중…':done<4?word+' '+done+'/4 · 남은 카메라에도 '+(point?'물체를 클릭해야':'그려야')+' 실행할 수 있습니다.':word+' 4/4 완료. SAM3에서 카메라별 초기 마스크를 고른 뒤 그 물체만 추적합니다. 결과는 마스크 검수에서 확인합니다.';
 updateLaunch();
}
function pointLabel(c){const pts=objectPoints[c]||[],pos=pts.filter(p=>p[2]===1).length;return pts.length?'<b>포함 '+pos+' · 제외 '+(pts.length-pos)+'</b> <button type="button" class="linkbtn" data-clear="'+c+'">지우기</button>':'포인트 없음'}
function loadBboxPicker(){
 const episode=$('episode').value,mode=objectMode(),on=mode!=='text',point=mode==='point_track',generation=++bboxGeneration;
 objectBoxes={};objectPoints={};bboxLoaded=new Set();bboxEpisode='';$('bboxPicker').classList.toggle('hidden',!on);
 $('bboxHelpBox').hidden=point;$('bboxHelpPoint').hidden=!point;
 if(!on){updateLaunch();return}
 if(!episode){$('bboxImages').textContent='에피소드를 선택하세요.';$('bboxStatus').textContent='';updateLaunch();return}
 $('bboxImages').innerHTML=[1,2,3,4].map(c=>'<figure><div class="bboxStage"><img alt="카메라 '+c+' 첫 프레임" data-cam="'+c+'"><canvas data-cam="'+c+'" role="img" aria-label="카메라 '+c+' 물체 '+(point?'포인트 찍기':'bbox 그리기')+'"></canvas></div><figcaption>카메라 '+c+' · 첫 프레임 · <span data-box="'+c+'">'+(point?'포인트 없음':'bbox 없음')+'</span></figcaption></figure>').join('');
 for(const img of $('bboxImages').querySelectorAll('img')){
  const cam=+img.dataset.cam,canvas=img.nextElementSibling,label=$('bboxImages').querySelector('[data-box="'+cam+'"]');
  img.onload=()=>{if(generation!==bboxGeneration)return;bboxLoaded.add(cam);if(bboxLoaded.size===4)bboxEpisode=episode;paintCam(canvas);bboxStatus()};
  img.onerror=()=>{if(generation!==bboxGeneration)return;$('bboxStatus').textContent='첫 프레임 사진을 불러오지 못했습니다. 다시 불러오기를 눌러주세요.';updateLaunch()};
  const at=e=>{const r=canvas.getBoundingClientRect();return [Math.min(1,Math.max(0,(e.clientX-r.left)/r.width)),Math.min(1,Math.max(0,(e.clientY-r.top)/r.height))]};
  if(point){
   // 왼쪽 클릭 = 포함(초록), Shift+클릭 또는 오른쪽 클릭 = 제외(빨강). 카메라당 최대 20개.
   const refresh=()=>{label.innerHTML=pointLabel(cam);const b=label.querySelector('[data-clear]');if(b)b.onclick=()=>{objectPoints[cam]=[];refresh()};paintCam(canvas);bboxStatus()};
   canvas.oncontextmenu=e=>e.preventDefault();
   canvas.onpointerdown=e=>{
    if(!bboxLoaded.has(cam)||(e.button!==0&&e.button!==2))return;e.preventDefault();
    const pts=objectPoints[cam]||(objectPoints[cam]=[]),neg=e.button===2||e.shiftKey;
    if(neg&&!pts.some(p=>p[2]===1)){$('bboxStatus').textContent='카메라 '+cam+': 먼저 물체 위를 왼쪽 클릭해 포함 포인트를 찍으세요.';return}
    if(pts.length>=20){$('bboxStatus').textContent='카메라 '+cam+': 포인트는 20개까지입니다. 지우기 후 다시 찍으세요.';return}
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
$('objectMode').onchange=loadBboxPicker;
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

let rawBoardBusy=false;
function rawCaptureDate(ep){const m=ep.match(/^ep_(\d{4})(\d{2})(\d{2})_/);return m?m[1]+'-'+m[2]+'-'+m[3]:'—'}
// 기본값은 전날 기록(오늘 11/13 → 11/12). 📅에서 다른 날짜나 '전체 보기'로 바꿀 수 있습니다.
let rawDateFilter=(()=>{const d=new Date();d.setDate(d.getDate()-1);return localDateKey(d)})();
const rawDateCal=createDateCalendar({toggle:$('rawDateToggle'),panel:$('rawCalendar'),keepEmpty:true,
 counts:()=>{const c={};rawEpisodes.forEach(e=>{const d=rawCaptureDate(e.episode);c[d]=(c[d]||0)+1});return c},
 get:()=>rawDateFilter,set:v=>{if(v!==rawDateFilter){rawDateFilter=v;renderRawTable()}}});
let rawTeam=null;
function rawWho(e){
 const me=rawTeam&&rawTeam.me,name=e.operator||e.assigned_to;if(!name)return '<span class="help">미배정</span>';
 const label=escape(name)+(e.operator?'':' <span class="help">(배정·시작 전)</span>');
 return name===me?'<span class="rawMe">'+label+'</span>':label;
}
function renderRawTeam(){
 const t=rawTeam,el=$('rawTeam');if(!t){el.textContent='';return}
 const ops=t.operators.map(n=>escape(n)+' '+(t.load[n]||0)+'/'+t.target+(n===t.me?' (나)':'')).join(' · ');
 el.innerHTML='로그인 사용자 '+t.total_users+'명 · 작업자 '+t.operators.length+'명(관리자 '+t.admins+'명 제외) · 작업자마다 '+t.target+'개씩 자동 배정, 3D 복원이 끝나면 남은 RAW에서 다시 채웁니다.'+(ops?'<br>'+ops:'');
}
function renderRawTable(){
 rawDateCal.refresh();
 const filter=rawDateFilter,groups={},me=rawTeam&&rawTeam.me;
 // 내 작업(시작 전 배정 + 3D 복원이 끝나지 않은 내 담당)은 날짜 필터와 상관없이 맨 위에 고정합니다.
 const isMine=e=>rawTeam&&rawTeam.role!=='admin'&&(e.assigned_to===me||(e.operator===me&&!e.done));
 const mine=[];rawEpisodes.forEach((e,i)=>{if(isMine(e))mine.push(i)});
 rawEpisodes.forEach((e,i)=>{const d=rawCaptureDate(e.episode);if(!isMine(e)&&(!filter||filter===d))(groups[d]=groups[d]||[]).push(i)});
 renderRawTeam();
 const dates=Object.keys(groups).sort((a,b)=>a==='—'?1:b==='—'?-1:b.localeCompare(a));
 const row=i=>{const e=rawEpisodes[i];return '<tr data-raw-index="'+i+'" class="'+($('episode').value===e.episode?'rawSelected':'')+'"><td>'+(e.can_run?'<input type="radio" name="rawSelection" aria-label="'+escape(e.episode)+' 선택" '+($('episode').value===e.episode?'checked':'')+'>':'<span class="help">조회만</span>')+'</td><td>'+rawCaptureDate(e.episode)+'</td><td>'+escape(e.episode)+'</td><td>'+rawWho(e)+'</td><td>'+stageLabel(e.progress|| '단계 확인 중')+'</td><td><button class="smallbtn" data-raw-preview="'+i+'">미리보기</button></td></tr>'};
 const mineHtml=mine.length?'<tr class="rawDateGroup rawMineGroup"><td colspan="6">👤 내 작업 · '+mine.length+' / '+rawTeam.target+'개 (날짜 필터와 무관)</td></tr>'+mine.map(i=>row(i).replace('<tr ','<tr data-mine="1" ')).join(''):'';
 $('rawRows').innerHTML=mineHtml+(dates.map(d=>'<tr class="rawDateGroup"><td colspan="6">📅 '+(d==='—'?'날짜 미상':escape(d))+' · '+groups[d].length+'개</td></tr>'+groups[d].map(row).join('')).join('')||('<tr><td colspan="6">'+(filter&&rawEpisodes.length?escape(filter)+' 촬영 RAW 에피소드가 없습니다. 📅에서 다른 날짜나 전체 보기를 선택하세요.':'RAW 에피소드가 없습니다.')+'</td></tr>'));
 $('rawRows').querySelectorAll('tr[data-mine]').forEach(r=>r.classList.add('rawMine'));
 $('rawRows').querySelectorAll('[data-raw-index]').forEach(row=>row.onclick=event=>{const e=rawEpisodes[+row.dataset.rawIndex];$('episode').value=e.episode;$('episode').dispatchEvent(new Event('change'));renderRawTable();if(event.target.closest('[data-raw-preview]'))revealSamPreview()});
}
async function refreshRawBoard(){
 if(rawBoardBusy||!currentUser)return;rawBoardBusy=true;
 const wid=selectedWorker();try{const d=await api('/api/raw-episodes?worker_id='+encodeURIComponent(selectedWorker()));if(wid!==selectedWorker())return;rawEpisodes=d.episodes;rawTeam=d.team||null;const selected=$('episode').value;$('episode').innerHTML=rawEpisodes.map(e=>'<option value="'+escape(e.episode)+'">'+escape(e.episode)+'</option>').join('');$('episode').value=rawEpisodes.some(e=>e.episode===selected)?selected:'';renderRawTable();updateLaunch()}
 catch(e){notice(e.message,true)}finally{rawBoardBusy=false}
}
$('refreshRaw').onclick=refreshRawBoard;
setInterval(()=>{if(currentUser&&page==='run')refreshRawBoard()},15000);

$('outputWorker').onchange=()=>{outputSeq++;outputPath='';outputRoot='project';outputRootsReady=false;openOutputExplorer()};
