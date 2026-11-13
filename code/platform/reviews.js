const HC_BASE='/kaist-review/dags/kaist_revising_gpu_4090_a';
// 마스크를 저장하면 올립니다. 이미지 URL에 붙여 브라우저가 같은 URL의 예전 이미지를 다시 쓰지 않게 합니다.
let hcImageRev=0;
let hcShownMode='overlay',hcFrameLoading=false,hcMeta=null,hcPath='',hcStage='mask',hcEpoch=0,hcFrame=0,hcVisited=[],hcBusy=false,hcTimer=null,hcToken='',activeDetailRun=null,hcLoadGeneration=0;
const hcVideos=()=>[$('hcObject'),$('hcHand')];
async function hcRequest(url,options={}){
 const response=await fetch(url,{credentials:'same-origin',cache:'no-store',...options});
 return readUiJson(response);
}
function hcStatus(text,error=false){$('hcStatus').textContent=text;$('hcStatus').className=error?'hcMessage error':'hcMessage'}
function hcStop(){clearInterval(hcTimer);hcTimer=null;if($('hcPlay'))$('hcPlay').textContent='▶ 재생';hcVideos().forEach(v=>v.pause())}
function hcApproved(){return hcMeta&&hcMeta.approval.status==='approved'}
function hcRequiredFrames(){return Array.from({length:hcMeta.frames},(_,i)=>i)}
function hcEnable(){
 if(!hcMeta)return;
 const approved=hcApproved();$('hcDecision').classList.toggle('hcReadonly',approved);$('hcDecision').querySelector('h2').textContent=approved?'승인 기록':'검수 결정';if(approved)$('hcTips').textContent='이미 승인된 결과입니다. 승인자: '+(hcMeta.approval.name||hcMeta.reviewer)+(hcMeta.approval.approved_at?' · '+new Date(hcMeta.approval.approved_at).toLocaleString('ko-KR'):'');$('hcState').textContent=approved?'승인 완료 · 결과 조회':'HC 검수 필요';$('hcState').className='badge '+(approved?'success':'hcNeeded');let complete=false;
 if(hcStage==='mask'){
  const frames=hcRequiredFrames();
  for(let cam=0;cam<4;cam++){
   const ready=frames.every(f=>hcVisited[cam].has(f));const check=$('hcCam'+cam);check.disabled=hcBusy||approved||!ready;
   if(!ready&&!approved)check.checked=false;
   check.title=ready?'전체 프레임 확인 완료':'모든 프레임의 마스크를 확인하면 체크할 수 있습니다.';
  }
  const missing=frames.filter(f=>hcVisited.some(v=>!v.has(f)));
  $('hcMissing').hidden=approved||!missing.length;
  $('hcMissing').disabled=hcFrameLoading;
  $('hcMissing').textContent='미확인 프레임 '+missing.length+'개 · 첫 미확인으로 이동';
  if(!approved)$('hcTips').textContent=missing.length?'이미지 로딩이 끝나지 않았거나 건너뛴 프레임이 있습니다.': '모든 프레임을 확인했습니다. 카메라 1~4 검수 완료를 체크하면 승인할 수 있습니다.';
  complete=hcVisited.length===4&&hcVisited.every(visited=>frames.every(f=>visited.has(f)))&&[...document.querySelectorAll('#hcChecks input')].every(x=>x.checked);
 }else{
  const reviewed=$('hcTrackingReviewed'),events=[...document.querySelectorAll('.hcEventCheck')];
  complete=Boolean(reviewed&&reviewed.checked)&&events.every(x=>x.checked)&&(!hcMeta.events.length||$('hcNote').value.trim());
 }
 $('hcApprove').disabled=hcBusy||approved||!complete;$('hcHold').disabled=hcBusy||approved;$('hcNote').disabled=hcBusy||approved;
 $('hcApprove').textContent=approved?'승인 완료':hcStage==='mask'?'마스크 승인 · 추적 진행':'추적 승인 · 복원 진행';
}
let hcQueueFilter='pending',hcQueueGeneration=0,hcReturnPage='review';
const hcQueueLabels={'pending':'검수 대기','mask-approved':'마스크 승인 완료','tracking-approved':'추적 승인 완료'};
function selectHcQueue(filter){
 hcQueueFilter=filter;hcLoadGeneration++;hcStop();if(page==='review')rememberPage('review');
 $('hcWorkspace').classList.add('hidden');$('hcQueuePanel').classList.remove('hidden');
 document.querySelectorAll('[data-review-filter]').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.reviewFilter===filter)));
 $('hcQueueTitle').textContent=hcQueueLabels[filter];
 $('hcQueueHelp').textContent=filter==='pending'?'확인이 필요한 마스크·추적 결과입니다.':'에피소드를 클릭하면 승인된 '+(filter==='mask-approved'?'마스크 이미지와':'추적 영상과')+' 승인 기록을 확인할 수 있습니다.';
 loadHcQueue();
}
let pendingHcQueue=null,shownHcFilter=null,hcHiddenLoaded=false;
async function loadHcQueue(){
 const generation=++hcQueueGeneration,filter=hcQueueFilter;
 if(shownHcFilter!==filter)$('hcQueue').textContent='검수 목록을 확인하는 중…';
 try{
  if(!pendingHcQueue)pendingHcQueue=hcRequest(HC_BASE+'/queue').finally(()=>{pendingHcQueue=null});
  const d=await pendingHcQueue;if(generation!==hcQueueGeneration)return;
  shownHcFilter=filter;
  if(!hcHiddenLoaded){hcHiddenLoaded=true;await loadHidden('review');if(generation!==hcQueueGeneration)return}
  const all=d.reviews.filter(x=>filter==='pending'?x.status!=='approved':x.status==='approved'&&x.stage===(filter==='mask-approved'?'mask':'tracking'));
  const hkey=x=>JSON.stringify([x.episode,x.stage]),hiddenCount=all.filter(x=>isHidden('review',hkey(x))).length;
  const rows=all.filter(x=>showHidden.review||!isHidden('review',hkey(x)));
  // 항목마다 NAS에 올리기 · 강제 종료 · 목록에서 삭제(숨김)
  $('hcQueue').innerHTML=(hiddenCount||showHidden.review?'<p>'+hiddenToggleHtml('review',hiddenCount)+'</p>':'')+(rows.map((x,i)=>'<div class="hcQueueRow'+(isHidden('review',hkey(x))?' hiddenRow':'')+'"><button class="hcQueueItem" data-index="'+i+'"><span><strong>'+escape(x.episode)+'</strong><small>'+escape(x.label)+' · '+escape(x.worker_label||'PC 1')+' · '+x.frames+'프레임</small></span><span class="badge '+(x.status==='approved'?'success':'hcNeeded')+'">'+(x.status==='approved'?'승인 완료 · 결과 보기':'HC 검수 필요')+' →</span></button>'+episodeActionsHtml({episode:x.episode,scope:'review',key:hkey(x),active:null,hidden:isHidden('review',hkey(x))})+'</div>').join('')||'<div class="hcEmpty">'+(hiddenCount?'모든 항목을 목록에서 숨겼습니다.':filter==='pending'?'대기 중인 검수가 없습니다.':'승인 완료된 '+(filter==='mask-approved'?'마스크':'추적')+' 결과가 없습니다.')+'</div>');
  $('hcQueue').querySelectorAll('button.hcQueueItem').forEach(b=>b.onclick=()=>openReview(rows[+b.dataset.index].episode,rows[+b.dataset.index].stage));
  bindEpisodeActions($('hcQueue'),()=>loadHcQueue());
 }catch(e){if(generation===hcQueueGeneration)$('hcQueue').textContent=e.message}
}
async function openReview(episode,stage='mask'){
 if(page!=='review')hcReturnPage=page;
 else if(!$('hcQueuePanel').classList.contains('hidden'))hcReturnPage='review';
 $('hcBack').textContent=hcReturnPage==='review'?'← 검수 목록':hcReturnPage==='jobs'?(currentUser.role==='admin'?'← 전체 작업 현황':'← 내 작업 현황'):'← 이전 화면';
 const generation=++hcLoadGeneration;hcStop();hcFrameLoading=false;hcMeta=null;hcStage=stage;hcEpoch++;$('hcQueuePanel').classList.add('hidden');$('hcWorkspace').classList.remove('hidden');show('review');rememberPage('review/'+encodeURIComponent(episode)+'/'+stage);$('hcMedia').classList.add('hidden');$('hcDecision').classList.add('hidden');$('hcResult').textContent='';hcFollowStop();$('hcNote').value='';
 $('hcState').textContent='검수 대상 확인 중';$('hcState').className='badge';$('hcEpisode').textContent=episode;$('hcTitle').textContent=stage==='mask'?'마스크 HC 검수':'추적 HC 검수';hcStatus('검수 대상과 최신 승인 상태를 확인하는 중…');
 try{
  const path=HC_BASE+'/episodes/'+encodeURIComponent(episode)+(stage==='tracking'?'/tracking':'');
  const [session,incoming]=await Promise.all([fetch('/kaist-review/?view='+stage+'&episode_id='+encodeURIComponent(episode),{credentials:'same-origin'}),hcRequest(path)]);if(!session.ok)throw Error('검수 세션 연결에 실패했습니다.');
  const html=await session.text();if(generation!==hcLoadGeneration)return;const tokenMatch=html.match(/const token='([^']+)'/);if(!tokenMatch)throw Error('검수 연결을 다시 열어주세요.');hcToken=tokenMatch[1];
  if(generation!==hcLoadGeneration)return;hcPath=path;hcMeta=incoming;hcStage=stage;
  $('hcMeta').textContent='';
  $('hcState').textContent=hcApproved()?'승인 완료 · 결과 조회':'HC 검수 필요';$('hcState').className='badge '+(hcApproved()?'success':'hcNeeded');
  $('hcMedia').classList.remove('hidden');$('hcDecision').classList.remove('hidden');$('hcChecks').classList.toggle('hidden',stage!=='mask');$('hcViewChecks').classList.toggle('hidden',stage!=='tracking');$('hcMask').classList.toggle('hidden',stage!=='mask');$('hcTracking').classList.toggle('hidden',stage!=='tracking');
  $('hcTips').textContent=stage==='mask'?'카메라별 모든 프레임을 확인하면 검수 완료를 체크할 수 있습니다.':'천·손의 추적점이 배경으로 벗어나거나 미끄러지는지 확인하세요. 표시된 이상 후보 구간도 함께 검수하세요.';
  if(stage==='mask'){
   hcVisited=[new Set(),new Set(),new Set(),new Set()];$('hcMaskMode').value='overlay';$('hcSlider').max=hcMeta.frames-1;
   $('hcImages').classList.remove('hcSolo');hcEdit.solo=null;
   $('hcImages').innerHTML=[0,1,2,3].map(cam=>'<figure data-cam="'+cam+'"><figcaption><strong>카메라 '+(cam+1)+'</strong><span class="hcCamEdit" id="hcCamEdit'+cam+'" hidden></span><span class="hcSoloHint">더블클릭: 크게 보기 / 2×2로 돌아가기</span></figcaption><div class="hcStage"><img id="hcImage'+cam+'" alt="카메라 '+(cam+1)+' 마스크"><canvas class="hcEditCanvas" id="hcEdit'+cam+'" aria-label="카메라 '+(cam+1)+' 마스크 수정"></canvas><div class="hcBrushRing" id="hcRing'+cam+'" hidden></div></div></figure>').join('');
   [0,1,2,3].forEach(hcEditBind);hcEditReset();
   $('hcChecks').innerHTML=[0,1,2,3].map(cam=>'<label><input id="hcCam'+cam+'" type="checkbox" disabled>카메라 '+(cam+1)+' 검수 완료</label>').join('');
   $('hcChecks').onchange=hcEnable;hcEnable();await hcShowFrame(0);
  }else{
   $('hcMissing').hidden=true;
   $('hcViewChecks').innerHTML='<label><input id="hcTrackingReviewed" type="checkbox" '+(hcApproved()?'disabled':'')+'>추적 영상 확인 완료</label>';
   $('hcViewChecks').onchange=hcEnable;
   $('hcObject').src=hcPath+'/video/cotracker';$('hcHand').src=hcPath+'/video/cotracker_hand';
   const reason={outside_mask:'마스크 밖으로 벗어난 추적점',outside_image:'영상 밖으로 벗어난 추적점',jump:'급격한 점 이동',low_visibility:'가림 또는 낮은 가시성',low_coverage:'낮은 추적 범위'};
   $('hcEvents').innerHTML=hcMeta.events.map((event,i)=>'<div class="hcEvent"><strong>'+escape(event.group==='cotracker_hand'?'손':'천')+' · 카메라 '+event.camera+'</strong><p>'+escape(reason[event.reason]||event.reason)+' · 프레임 '+event.start_frame+'–'+event.end_frame+'</p><button class="smallbtn" data-event="'+i+'">이 구간 보기</button><label><input class="hcEventCheck" type="checkbox" '+(hcApproved()?'disabled':'')+'>구간 확인 완료</label></div>').join('')||'<p class="help">자동 검사에서 이상 후보가 표시되지 않았습니다. 4개 카메라는 직접 확인하세요.</p>';
   // 자동 검사에서 표시된 이상 구간을 검수 의견 칸에 영어로 미리 적어 둡니다(비어 있고 아직 승인 전일 때만). 검수자가 고쳐 쓸 수 있습니다.
   const reasonEn={outside_mask:'Tracking points outside the mask',outside_image:'Tracking points outside the image',jump:'Sudden point jump',low_visibility:'Occlusion or low visibility',low_coverage:'Low tracking coverage'};
   if(!hcApproved()&&!$('hcNote').value.trim()&&hcMeta.events.length)$('hcNote').value=hcMeta.events.map(event=>(event.group==='cotracker_hand'?'Hand':'Object')+' · Camera '+event.camera+' · '+(reasonEn[event.reason]||event.reason)+' · Frames '+event.start_frame+'–'+event.end_frame).join('\n');
   $('hcEvents').querySelectorAll('[data-event]').forEach(b=>b.onclick=()=>hcSeek(hcMeta.events[+b.dataset.event].start_frame));$('hcEvents').onchange=hcEnable;
  }
  hcStatus('');hcEnable();
  // 이미 승인된 검수를 열면 그 뒤 진행 중인 단계의 로그를 바로 보여 줍니다.
  if(hcApproved())hcFollowStart();else hcFollowStop();
 }catch(e){if(generation!==hcLoadGeneration)return;hcMeta=null;hcStatus(e.message,true);$('hcDecision').classList.add('hidden')}
}
async function hcShowFrame(value,skipEditCheck=false,keepEditsUntilLoaded=false){
 if(!hcMeta||hcFrameLoading)return;
 if(!skipEditCheck&&hcEditDirty()){hcStop();if(!await hcEditResolve()){$('hcSlider').value=hcFrame;$('hcMaskMode').value=hcShownMode;return}if(!hcMeta||hcFrameLoading)return}
 if(!keepEditsUntilLoaded)hcEditReset();hcShownMode=$('hcMaskMode').value;hcFrameLoading=true;hcFrame=Math.max(0,Math.min(hcMeta.frames-1,Number(value)));$('hcSlider').value=hcFrame;$('hcFrameLabel').textContent=(hcFrame+1)+' / '+hcMeta.frames+'프레임';['hcPrevFrame','hcNextFrame','hcFirst','hcMiddle','hcLast','hcSlider','hcMaskMode'].forEach(id=>$(id).disabled=true);
 const epoch=++hcEpoch,frame=hcFrame,mode=$('hcMaskMode').value;
 await Promise.all([0,1,2,3].map(cam=>new Promise(resolve=>{
  const img=$('hcImage'+cam),url=hcPath+'/image?'+new URLSearchParams({cam,frame,mode,rev:hcImageRev});
  img.onload=()=>{if(img.naturalWidth)img.parentElement.style.aspectRatio=img.naturalWidth+'/'+img.naturalHeight;if(epoch===hcEpoch&&mode==='overlay'){hcVisited[cam].add(frame);hcEnable()}resolve()};
  img.onerror=()=>{if(epoch!==hcEpoch){resolve();return}hcStatus('카메라 '+(cam+1)+' 이미지를 불러오지 못했습니다. 새로 불러오기를 눌러주세요.',true);hcStop();resolve()};
  img.src=url;img.onclick=()=>window.open(url,'_blank','noopener');
 })));
 if(epoch!==hcEpoch)return;hcFrameLoading=false;if(keepEditsUntilLoaded)hcEditReset();
 ['hcFirst','hcMiddle','hcLast','hcSlider','hcMaskMode'].forEach(id=>$(id).disabled=false);
 $('hcPrevFrame').disabled=hcFrame===0;$('hcNextFrame').disabled=hcFrame===hcMeta.frames-1;hcEnable();
}
function hcSeek(frame){hcStop();if(!hcMeta)return;hcVideos().forEach(v=>{if(v.readyState>=1)v.currentTime=Math.min(frame/hcMeta.fps,Math.max(0,v.duration-.03))})}
let hcConfirmPending=false;
function hcConfirmDecision(decision){
 const dialog=$('hcConfirmDialog');
 $('hcConfirmTitle').textContent=decision==='approve'?'검수 승인':'검수 보류';
 $('hcConfirmEpisode').textContent=hcMeta.episode+' · '+(hcStage==='mask'?'마스크 검수':'추적 검수');
 $('hcConfirmText').textContent=decision==='approve'?'검수한 결과를 승인하고 다음 단계로 진행할까요?':'이 결과를 보류하고 후속 단계를 대기시킬까요?';
 $('hcConfirmAccept').textContent=decision==='approve'?'승인하고 다음 단계':'보류하기';
 dialog.returnValue='';
 return new Promise(resolve=>{dialog.addEventListener('close',()=>resolve(dialog.returnValue==='confirm'),{once:true});dialog.showModal();$('hcConfirmCancel').focus()});
}
$('hcConfirmCancel').onclick=()=>$('hcConfirmDialog').close('cancel');
$('hcConfirmAccept').onclick=()=>$('hcConfirmDialog').close('confirm');
$('hcConfirmDialog').addEventListener('cancel',e=>{e.preventDefault();$('hcConfirmDialog').close('cancel')});
async function hcDecide(decision){
 if(!hcMeta||hcBusy||hcConfirmPending||hcApproved())return;
 if(decision==='approve'&&$('hcApprove').disabled)return;
 if(decision==='hold'&&!$('hcNote').value.trim()){hcStatus('카메라 번호, 프레임 번호와 문제 내용을 적어주세요.',true);return}
 const generation=hcLoadGeneration;hcStop();hcConfirmPending=true;
 let confirmed=false;try{confirmed=await hcConfirmDecision(decision)}finally{hcConfirmPending=false}
 if(!confirmed||generation!==hcLoadGeneration||!hcMeta||hcApproved())return;
 if(decision==='approve'&&$('hcApprove').disabled)return;
 hcStop();hcBusy=true;hcEnable();$('hcResult').textContent='검수 기준과 변경 여부를 확인하고 기록하는 중…';
 const data={decision,basis:hcMeta.basis,note:$('hcNote').value};
 if(hcStage==='mask')data.checked=[0,1,2,3].map(cam=>$('hcCam'+cam).checked);
 else{data.checked_views=Array(8).fill($('hcTrackingReviewed').checked);data.checked=[...document.querySelectorAll('.hcEventCheck')].map(c=>c.checked)}
 try{
  const result=await hcRequest(hcPath+'/decision',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':hcToken},body:JSON.stringify(data)});
  $('hcResult').textContent=result.message;hcMeta.approval.status=result.status;if(result.status==='approved')hcFollowStart();hcStatus(result.status==='approved'?'승인 완료. 중앙 Airflow가 결과를 확인한 뒤 다음 단계를 진행합니다.':'보류되었습니다. 후속 단계는 대기합니다.');loadJobs();
 }catch(e){$('hcResult').textContent=e.message}
 finally{hcBusy=false;hcEnable()}
}
function returnToReviewQueue(){hcLoadGeneration++;hcEpoch++;hcStop();hcMeta=null;$('hcWorkspace').classList.add('hidden');$('hcQueuePanel').classList.remove('hidden');show('review')}
$('hcBack').onclick=()=>{if(!hcEditLeaveOk())return;hcEditReset();if(hcReturnPage==='review'){returnToReviewQueue();return}hcLoadGeneration++;hcEpoch++;hcStop();show(hcReturnPage)};
$('hcList').onclick=()=>{if(!hcEditLeaveOk())return;hcEditReset();returnToReviewQueue()};
$('hcReload').onclick=()=>{if(!hcEditLeaveOk())return;hcEditReset();if($('hcEpisode').textContent)openReview($('hcEpisode').textContent,hcStage)};
$('hcRefreshQueue').onclick=loadHcQueue;document.querySelectorAll('[data-review-filter]').forEach(b=>b.onclick=()=>selectHcQueue(b.dataset.reviewFilter));
$('hcApprove').onclick=()=>hcDecide('approve');$('hcHold').onclick=()=>hcDecide('hold');$('hcNote').oninput=hcEnable;
$('hcFirst').onclick=()=>{hcStop();hcShowFrame(0)};$('hcMiddle').onclick=()=>{hcStop();hcShowFrame(Math.floor(hcMeta.frames/2))};$('hcLast').onclick=()=>{hcStop();hcShowFrame(hcMeta.frames-1)};
$('hcSlider').oninput=e=>{hcStop();hcShowFrame(e.target.value)};$('hcMaskMode').onchange=()=>hcShowFrame(hcFrame);
$('hcPlay').onclick=async()=>{if(!hcMeta)return;if(hcTimer){hcStop();return}if(!await hcEditResolve('재생'))return;let loading=false;$('hcPlay').textContent='❚❚ 일시정지';hcTimer=setInterval(async()=>{if(loading)return;if(hcFrame>=hcMeta.frames-1){hcStop();return}loading=true;await hcShowFrame(hcFrame+1);loading=false},350)};
$('hcTrackPlay').onclick=()=>Promise.allSettled(hcVideos().map(v=>v.play()));$('hcTrackPause').onclick=hcStop;
$('hcTrackFirst').onclick=()=>hcSeek(0);$('hcTrackMiddle').onclick=()=>hcSeek(Math.floor(hcMeta.frames/2));$('hcTrackLast').onclick=()=>hcSeek(hcMeta.frames-1);
hcVideos().forEach((v,i)=>{v.ontimeupdate=()=>{if(!hcMeta||hcStage!=='tracking')return;$('hcTrackFrame').textContent=Math.min(hcMeta.frames,Math.floor(v.currentTime*hcMeta.fps)+1)+' / '+hcMeta.frames+'프레임';const other=hcVideos()[1-i];if(other.readyState>=1&&Math.abs(v.currentTime-other.currentTime)>.2)other.currentTime=v.currentTime};v.onplay=()=>{const other=hcVideos()[1-i];if(other.paused)other.play().catch(()=>{})};v.onpause=()=>{const other=hcVideos()[1-i];if(!other.paused)other.pause()}});

setInterval(()=>{if(!document.hidden&&!pendingHcQueue&&page==='review'&&!$('hcQueuePanel').classList.contains('hidden'))loadHcQueue()},20000);

function hcStepFrame(delta){if(!hcMeta||hcStage!=='mask')return;hcStop();hcShowFrame(hcFrame+delta)}
$('hcPrevFrame').onclick=()=>hcStepFrame(-1);
$('hcNextFrame').onclick=()=>hcStepFrame(1);
document.addEventListener('keydown',e=>{
 if(page!=='review'||!hcMeta||hcStage!=='mask'||$('hcWorkspace').classList.contains('hidden')||document.querySelector('dialog[open]')||e.target.closest('input,textarea,select,[contenteditable="true"]')||e.ctrlKey||e.metaKey||e.altKey)return;
 if(e.key==='ArrowLeft'||e.key==='ArrowRight'){e.preventDefault();hcStepFrame(e.key==='ArrowLeft'?-1:1)}
});

$('hcMissing').onclick=()=>{if(!hcMeta||hcFrameLoading)return;hcStop();$('hcMaskMode').value='overlay';const missing=hcRequiredFrames().find(f=>hcVisited.some(v=>!v.has(f)));if(missing!==undefined)hcShowFrame(missing)};

// ---- 승인 후 다음 단계 진행 로그 ----
// 승인 메시지 아래에 이 에피소드 실행의 단계 흐름과 현재 단계 로그를 3초마다 보여 줍니다(작업 현황 로그와 같은 API).
const hcFollow={timer:null,episode:'',generation:0,job:null,state:{stick:true},busy:false};
function hcFollowStop(){clearInterval(hcFollow.timer);hcFollow.timer=null;hcFollow.job=null;$('hcFollow').hidden=true}
function hcFollowStart(){
 hcFollowStop();if(!hcMeta)return;
 hcFollow.episode=hcMeta.episode;hcFollow.generation=hcLoadGeneration;hcFollow.state={stick:true};
 const box=$('hcFollowLog'),follow=box.querySelector('.jobLogFollow input');follow.checked=true;
 follow.onchange=()=>{hcFollow.state.stick=follow.checked;if(follow.checked)paintJobLog(box,hcFollow.state)};
 $('hcFollowFlow').textContent='';$('hcFollowTitle').textContent='다음 단계 로그';box.querySelector('.jobLogText').textContent='실행 상태를 확인하는 중…';
 box.querySelector('.jobLogState').textContent='';box.querySelector('.jobLogBars').hidden=true;
 $('hcFollow').hidden=false;hcFollowTick();hcFollow.timer=setInterval(hcFollowTick,3000);
}
// 로그를 보여 줄 단계: 실행 중 → 실패 → 마지막 완료 순. 승인 확인 단계는 로그가 없으므로 고르지 않습니다.
function hcFollowStage(tasks){
 const work=orderedTasks(tasks).filter(t=>!['mask_approved','tracking_approved'].includes(t.task));
 const active=work.find(t=>['running','queued'].includes(taskDisplayState(t))||t.worker_state==='running');
 return active||work.find(t=>t.state==='failed')||[...work].reverse().find(t=>t.state==='success')||null;
}
function hcFollowFlowText(job){
 const mark={success:'✓',failed:'✗',upstream_failed:'–',skipped:'–'};
 return orderedTasks(job.tasks||[]).map(t=>{
  const s=taskDisplayState(t),m=mark[s]||(s==='running'||t.worker_state==='running'?'▶':s==='review_waiting'||t.worker_state==='waiting'?'⏸':'·');
  return (s==='running'||t.worker_state==='running'?'<b>':'')+escape(taskName[t.task]||t.task)+' '+m+(s==='running'||t.worker_state==='running'?'</b>':'');
 }).join(' → ');
}
async function hcFollowTick(){
 if(hcFollow.busy)return;
 if(page!=='review'||$('hcWorkspace').classList.contains('hidden')||hcFollow.generation!==hcLoadGeneration||!hcMeta||hcMeta.episode!==hcFollow.episode){hcFollowStop();return}
 if(document.hidden)return;
 hcFollow.busy=true;const box=$('hcFollowLog');
 try{
  // 1) 이 에피소드의 가장 최근 파이프라인 실행을 찾고, 그 실행만 단계 정보까지 자세히 받습니다.
  let job=hcFollow.job;
  if(!job){
   const rows=await api('/api/jobs?summary=1');
   job=rows.filter(r=>r.episode===hcFollow.episode&&r.mode!=='raw'&&r.mode!=='test'&&!r.legacy).sort((a,b)=>String(b.start||'').localeCompare(String(a.start||'')))[0];
   if(!job){box.querySelector('.jobLogText').textContent='이 에피소드의 실행을 찾지 못했습니다. 본인이 실행한 작업만 표시됩니다.';return}
  }
  const rows=await api('/api/jobs?'+new URLSearchParams([['summary','1'],['expanded',jobKey(job)]]));
  job=rows.find(r=>r.dag_id===job.dag_id&&r.run_id===job.run_id)||job;hcFollow.job=job;
  if(hcFollow.generation!==hcLoadGeneration)return;
  $('hcFollowFlow').innerHTML='실행 '+escape(stateName(job.state))+' · '+hcFollowFlowText(job);
  // 2) 현재 단계 로그
  const task=hcFollowStage(job.tasks||[]);
  if(!task){box.querySelector('.jobLogText').textContent='승인을 확인하고 다음 단계를 준비하는 중입니다…';return}
  $('hcFollowTitle').textContent='로그 · '+(taskName[task.task]||task.task);
  const d=await api('/api/jobs/log?'+new URLSearchParams({dag_id:job.dag_id,run_id:job.run_id,stage:task.task}));
  if(hcFollow.generation!==hcLoadGeneration||$('hcFollow').hidden)return;
  hcFollow.state.data=d;paintJobLog(box,hcFollow.state);
  // 실행이 끝나면(완료·실패) 마지막 로그를 남겨 두고 갱신을 멈춥니다.
  if(['success','failed'].includes(job.state)){clearInterval(hcFollow.timer);hcFollow.timer=null}
 }catch(e){box.querySelector('.jobLogText').textContent='로그를 불러오지 못했습니다: '+e.message}
 finally{hcFollow.busy=false}
}
