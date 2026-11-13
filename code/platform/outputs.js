let outputRoot='project',outputPath='',outputListing=null,outputSeq=0,outputRoots=[],outputRootsReady=false;
function outputUrl(action,values={}){return '/api/outputs/'+action+'?'+new URLSearchParams({worker_id:action.startsWith('preview')?selectedWorker():($('outputWorker').value||selectedWorker()),...values})}
const fileSize=n=>n==null?'—':n<1024?n+' B':n<1048576?(n/1024).toFixed(1)+' KB':n<1073741824?(n/1048576).toFixed(1)+' MB':(n/1073741824).toFixed(2)+' GB';
const fileDate=t=>new Date(t*1000).toLocaleString('ko-KR',{timeZone:'Asia/Seoul'});
async function loadOutputCatalog(){
 const wid=$('outputWorker').value;const tree=$('outputTree');tree.textContent='서버 결과 폴더를 읽는 중…';
 try{const data=await api(outputUrl('catalog',{root:outputRoot}));if(wid!==$('outputWorker').value)return;$('dateBasis').textContent=data.date_basis+' 기준 · 생성된 결과만 표시';
 tree.innerHTML=data.groups.map((group,i)=>'<details '+(i===0?'open':'')+'><summary>📁 '+escape(group.date)+'</summary>'+group.episodes.map(ep=>'<details class="episodeFolder"><summary>📁 '+escape(ep.episode)+'</summary>'+ep.folders.map(f=>'<button class="folderLink" data-root="'+escape(f.root)+'" data-path="'+escape(f.path)+'">📁 '+escape(f.label)+'</button>').join('')+'</details>').join('')+'</details>').join('')||'<p class="help">아직 생성된 결과가 없습니다.</p>';
 tree.querySelectorAll('[data-path]').forEach(button=>button.onclick=()=>browseOutputs(button.dataset.path,button.dataset.root));
 }catch(e){tree.textContent=e.message}
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
function updateLaunch(){
 const mode='pipeline';
 const selected=rawEpisodes.find(e=>e.episode===$('episode').value);
 $('episodeOwner').textContent=!selected?'RAW 표에서 에피소드를 선택하세요.':selected?.operator?(selected.can_run?'내가 선택한 데이터입니다.':selected.operator+' 작업자가 선택한 데이터입니다. 이미지 확인만 가능하며 중복 실행할 수 없습니다.'):'아직 다른 작업자가 선택하지 않은 데이터입니다.';
 const ready=mode!=='pipeline'||(previewEpisode===$('episode').value&&previewLoaded.size===2&&$('confirmed').checked&&$('prompt').value.trim());
 $('launch').disabled=busy||!connected||!ready||(mode!=='test'&&(!$('episode').value||selected?.can_run===false));
}
async function loadEpisodePreview(){
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
function renderRawTable(){
 $('rawRows').innerHTML=rawEpisodes.map((e,i)=>'<tr data-raw-index="'+i+'" class="'+($('episode').value===e.episode?'rawSelected':'')+'"><td>'+(e.can_run?'<input type="radio" name="rawSelection" aria-label="'+escape(e.episode)+' 선택" '+($('episode').value===e.episode?'checked':'')+'>':'<span class="help">조회만</span>')+'</td><td>'+rawCaptureDate(e.episode)+'</td><td>'+escape(e.episode)+'</td><td>'+escape(e.operator||'—')+'</td><td>'+stageLabel(e.progress|| '단계 확인 중')+'</td><td><button class="smallbtn" data-raw-preview="'+i+'">미리보기</button></td></tr>').join('')||'<tr><td colspan="6">RAW 에피소드가 없습니다.</td></tr>';
 $('rawRows').querySelectorAll('[data-raw-index]').forEach(row=>row.onclick=event=>{const e=rawEpisodes[+row.dataset.rawIndex];$('episode').value=e.episode;$('episode').dispatchEvent(new Event('change'));renderRawTable();if(event.target.closest('[data-raw-preview]'))revealSamPreview()});
}
async function refreshRawBoard(){
 if(rawBoardBusy||!currentUser)return;rawBoardBusy=true;
 const wid=selectedWorker();try{const d=await api('/api/raw-episodes?worker_id='+encodeURIComponent(selectedWorker()));if(wid!==selectedWorker())return;rawEpisodes=d.episodes;const selected=$('episode').value;$('episode').innerHTML=rawEpisodes.map(e=>'<option value="'+escape(e.episode)+'">'+escape(e.episode)+'</option>').join('');$('episode').value=rawEpisodes.some(e=>e.episode===selected)?selected:'';renderRawTable();updateLaunch()}
 catch(e){notice(e.message,true)}finally{rawBoardBusy=false}
}
$('refreshRaw').onclick=refreshRawBoard;
setInterval(()=>{if(currentUser&&page==='run')refreshRawBoard()},15000);

$('outputWorker').onchange=()=>{outputSeq++;outputPath='';outputRoot='project';outputRootsReady=false;openOutputExplorer()};
