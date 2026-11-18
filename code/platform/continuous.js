// ==== SAM3 연속 모드 ====
// 작업자: 분배·배정받은 RAW를 하나씩 띄워 카메라 1~4 첫 프레임에 포인트(카메라당 최대 20개)와 분할 대상을 넣고 '완료'하면
// 대기열에 쌓이고 바로 다음 RAW가 나옵니다. 대기열은 서버가 완료한 순서대로 하나씩(앞 RAW 전처리~SAM3가 끝나면 다음) 실행합니다.
// 관리자: PC 현황 화면에서 남은 RAW를 고른 PC(작업자)들에 균등 분배·회수합니다.
let contDeferInit=false,contData=null,contEp='',contPoints={},contHistory=[],contLoaded=new Set(),contGen=0,contBusy=false,contSubmitting=false;
const contSkipped=[];  // 이번 화면에서 '건너뛰기'한 RAW(목록 뒤로)
const CONT_MAX_POINTS=20;
function contTodo(){
 const list=(contData&&contData.todo||[]).map(t=>t.episode);
 return list.filter(e=>!contSkipped.includes(e)).concat(contSkipped.filter(e=>list.includes(e)));
}
function contObject(ep){const t=(contData&&contData.todo||[]).find(x=>x.episode===ep);return t?t.object:null}
function contReady(){return contEp&&contLoaded.size===4&&[1,2,3,4].every(c=>(contPoints[c]||[]).some(p=>p[2]===1))&&$('contPrompt').value.trim()}
function contStatus(text){$('contPickStatus').textContent=text||''}
function contButtons(){
 $('contDone').disabled=contSubmitting||!contReady();$('contUndo').disabled=!contHistory.length;
 const done=[1,2,3,4].filter(c=>(contPoints[c]||[]).some(p=>p[2]===1)).length;
 if(!contEp)return;
 contStatus(contLoaded.size<4?'첫 프레임 사진을 불러오는 중…':done<4?'포인트 '+done+'/4 · 카메라마다 물체 위를 한 번 이상 클릭하세요.':!$('contPrompt').value.trim()?'분할 대상(텍스트)을 입력하세요.':'준비 완료 · "완료 · 다음"을 누르면 대기열에 넣고 다음 RAW로 넘어갑니다.');
}
function contLabel(c){const pts=contPoints[c]||[],pos=pts.filter(p=>p[2]===1).length;return pts.length?'<b>포함 '+pos+' · 제외 '+(pts.length-pos)+'</b> <button type="button" class="linkbtn" data-cont-clear="'+c+'">지우기</button>':'포인트 없음'}
function contPaint(c){
 const canvas=$('contImages').querySelector('canvas[data-cam="'+c+'"]');if(!canvas)return;
 paintBox(canvas,null,contPoints[c]||[]);
 const label=$('contImages').querySelector('[data-cont-label="'+c+'"]');if(label)label.innerHTML=contLabel(c);
}
function contPrefetch(ep){if(!ep)return;for(const cam of [1,2,3,4]){const i=new Image();i.src=outputUrl('preview',{episode:ep,cam,position:0})}}
async function contShow(ep){
 contEp=ep||'';contPoints={};contHistory=[];contLoaded=new Set();const gen=++contGen;
 const todo=contTodo();
 $('contPicker').hidden=!contEp;$('contEmpty').hidden=!!contEp;
 if(!contEp){contStatus('');contButtons();return}
 const idx=todo.indexOf(contEp);
 $('contEpName').textContent=contEp;$('contEpPos').textContent='포인트 찍기 남은 '+todo.length+'개 중 '+(idx+1)+'번째'+(contSkipped.includes(contEp)?' · 건너뛴 항목':'');
 $('contPrompt').value=contObject(contEp)||'';
 if(!$('contPrompt').value){
  api('/api/continuous/object?'+new URLSearchParams({episode:contEp})).then(d=>{if(gen===contGen&&!$('contPrompt').value&&d.object){$('contPrompt').value=d.object;contButtons()}}).catch(()=>{});
 }
 $('contImages').innerHTML=[1,2,3,4].map(c=>'<figure><div class="bboxStage"><img alt="카메라 '+c+' 첫 프레임" data-cam="'+c+'"><canvas data-cam="'+c+'" role="img" aria-label="카메라 '+c+' 포인트 찍기"></canvas></div><figcaption>카메라 '+c+' · <span data-cont-label="'+c+'">포인트 없음</span></figcaption></figure>').join('');
 for(const img of $('contImages').querySelectorAll('img')){
  const cam=+img.dataset.cam,canvas=img.nextElementSibling;
  img.onload=()=>{if(gen!==contGen)return;contLoaded.add(cam);contPaint(cam);contButtons()};
  img.onerror=()=>{if(gen!==contGen)return;contStatus('카메라 '+cam+' 첫 프레임을 불러오지 못했습니다. 건너뛰거나 새로고침하세요.')};
  canvas.oncontextmenu=e=>e.preventDefault();canvas.style.cursor='copy';
  canvas.onpointerdown=e=>{
   if(!contLoaded.has(cam)||(e.button!==0&&e.button!==2))return;e.preventDefault();
   const pts=contPoints[cam]||(contPoints[cam]=[]),neg=e.button===2||e.shiftKey;
   if(neg&&!pts.some(p=>p[2]===1)){contStatus('카메라 '+cam+': 먼저 물체 위를 왼쪽 클릭해 포함 포인트를 찍으세요.');return}
   if(pts.length>=CONT_MAX_POINTS){contStatus('카메라 '+cam+': 포인트는 '+CONT_MAX_POINTS+'개까지입니다.');return}
   const r=canvas.getBoundingClientRect();
   pts.push([Math.min(1,Math.max(0,(e.clientX-r.left)/r.width)),Math.min(1,Math.max(0,(e.clientY-r.top)/r.height)),neg?0:1]);
   contHistory.push(cam);contPaint(cam);contButtons();
  };
  img.src=outputUrl('preview',{episode:contEp,cam,position:0});
 }
 contPrefetch(todo[idx+1]);contButtons();
}
$('contImages').addEventListener('click',e=>{const b=e.target.closest('[data-cont-clear]');if(!b)return;const c=+b.dataset.contClear;contPoints[c]=[];contHistory=contHistory.filter(x=>x!==c);contPaint(c);contButtons()});
$('contUndo').onclick=()=>{const c=contHistory.pop();if(c==null)return;(contPoints[c]||[]).pop();contPaint(c);contButtons()};
$('contClearAll').onclick=()=>{contPoints={};contHistory=[];[1,2,3,4].forEach(contPaint);contButtons()};
$('contPrompt').oninput=contButtons;
$('contSkip').onclick=()=>{
 if(!contEp)return;const i=contSkipped.indexOf(contEp);if(i>=0)contSkipped.splice(i,1);contSkipped.push(contEp);
 const todo=contTodo();contShow(todo.find(e=>e!==contEp)||todo[0]);
};
$('contDone').onclick=async()=>{
 if(!contReady()||contSubmitting)return;contSubmitting=true;contButtons();
 const ep=contEp,points={};for(const c of [1,2,3,4])points[String(c-1)]=contPoints[c].map(([x,y,l])=>[Math.round(x*10000)/10000,Math.round(y*10000)/10000,l]);
 try{
  await api('/api/continuous/submit',{method:'POST',body:JSON.stringify({episode:ep,sam_prompt:$('contPrompt').value.trim(),points,defer_mano:!$('contDeferBox').hidden&&$('contDefer').checked})});
  const i=contSkipped.indexOf(ep);if(i>=0)contSkipped.splice(i,1);
  if(contData)contData.todo=contData.todo.filter(t=>t.episode!==ep);
  notice(ep+' · 연속 모드 대기열에 넣었습니다. 순서대로 RAW 전처리 → SAM3를 실행합니다.');
  contShow(contTodo()[0]);loadContinuous(true);
 }catch(e){notice(e.message,true)}
 finally{contSubmitting=false;contButtons()}
};
window.addEventListener('resize',()=>[1,2,3,4].forEach(contPaint));
function contQueueHtml(q){
 if(!q.length)return '<p class="help">대기열이 비어 있습니다.</p>';
 return q.map(r=>{
  const head='<b>'+escape(r.raw_episode)+'</b> <span class="help">· '+escape(r.sam_prompt)+'</span>';
  let st='',act='';
  if(r.state==='waiting'){st='<span class="badge">대기 '+r.position+'번째</span>';if(r.mine)act='<button type="button" class="smallbtn" data-cont-cancel="'+r.id+'" title="대기열에서 빼고 포인트를 다시 찍습니다">빼기</button>'}
  else if(r.state==='failed'){st='<span class="badge failed">시작 실패</span> <span class="help">'+escape(r.error||'')+'</span>';if(r.mine)act='<button type="button" class="smallbtn" data-cont-retry="'+r.id+'">다시 시도</button><button type="button" class="smallbtn" data-cont-cancel="'+r.id+'">빼기</button>'}
  else st='<span class="badge running">시작됨</span> <span class="help">'+escape((r.episode&&r.episode!==r.raw_episode?r.episode+' · ':'')+(r.stage||'작업 현황에서 확인'))+'</span>';
  return '<div class="contQRow"><div>'+head+'<div>'+st+'</div></div><div class="contQAct">'+act+'</div></div>';
 }).join('');
}
async function loadContinuous(force=false){
 if((contBusy&&!force)||!currentUser||document.hidden)return;contBusy=true;
 try{
  const d=await api('/api/continuous?worker_id='+encodeURIComponent(selectedWorker()));
  $('contCard').hidden=!d.available;if(!d.available){contData=null;return}
  contData=d;
  const q=d.queue||[],wait=q.filter(r=>r.state==='waiting').length,fail=q.filter(r=>r.state==='failed').length,started=q.filter(r=>r.state==='started').length;
  $('contSummary').textContent='포인트 찍기 남음 '+d.todo.length+'개 · 실행 대기 '+wait+'개 · 오늘 시작 '+started+'개'+(fail?' · 시작 실패 '+fail+'개':'')+(d.distributed?' · 이 PC 분배 '+d.distributed+'개':'');
  const html=contQueueHtml(q);if($('contQueue').dataset.snap!==html){$('contQueue').dataset.snap=html;$('contQueue').innerHTML=html}
  $('contDeferBox').hidden=$('pc1DayOption').hidden;
  if(!contDeferInit){contDeferInit=true;$('contDefer').checked=$('deferMano').checked}
  const todo=contTodo(),open=$('contPicker').dataset.open==='1';
  if(contEp&&!todo.includes(contEp)){notice(contEp+'는 더 이상 내 연속 모드 목록에 없습니다(회수·다른 PC 실행 등). 다음 RAW로 넘어갑니다.',true);contShow(todo[0])}
  else if(!contEp&&open&&todo.length)contShow(todo[0]);
  if(!contEp){$('contEmpty').hidden=false;$('contEmpty').textContent=todo.length?'"연속 모드 시작"을 누르면 첫 RAW부터 포인트를 찍습니다.':open?'포인트를 모두 찍었습니다. 대기열 순서대로 실행합니다.':'포인트를 찍을 RAW가 없습니다. 관리자가 PC 현황에서 분배하면 여기에 나타납니다.'}
  $('contStart').hidden=open||!todo.length;
  loadContMano();
 }catch(e){$('contSummary').textContent=e.message}
 finally{contBusy=false}
}
$('contStart').onclick=()=>{$('contPicker').dataset.open='1';$('contStart').hidden=true;$('contClose').hidden=false;contShow(contTodo()[0])};
$('contClose').onclick=()=>{$('contPicker').dataset.open='';contEp='';$('contPicker').hidden=true;$('contClose').hidden=true;loadContinuous(true)};
$('contRefresh').onclick=()=>{loadContinuous(true);loadContMano(true)};
$('contQueue').addEventListener('click',async e=>{
 const c=e.target.closest('[data-cont-cancel]'),r=e.target.closest('[data-cont-retry]');if(!c&&!r)return;
 const b=c||r;b.disabled=true;
 try{
  if(c){const d=await api('/api/continuous/cancel',{method:'POST',body:JSON.stringify({id:+c.dataset.contCancel})});notice(d.episode+' · 대기열에서 뺐습니다. 포인트 찍기 목록으로 돌아갑니다.')}
  else{await api('/api/continuous/retry',{method:'POST',body:JSON.stringify({id:+r.dataset.contRetry})});notice('대기열 맨 뒤에 다시 넣었습니다.')}
  await loadContinuous(true);
 }catch(err){notice(err.message,true);b.disabled=false}
});
setInterval(()=>{if(page==='run')loadContinuous()},10000);
$('gpu').addEventListener('change',()=>{contSkipped.length=0;contEp='';$('contPicker').dataset.open='';$('contPicker').hidden=true;$('contClose').hidden=true;loadContinuous(true)});
setTimeout(()=>loadContinuous(true),2000);

// ---- 관리자: 남은 RAW 균등 분배 (PC 현황 화면) ----
let contDistBusy=false;
async function loadContDist(){
 if(contDistBusy||!currentUser||currentUser.role!=='admin')return;contDistBusy=true;
 try{
  const d=await api('/api/continuous/distribution');
  if(!d.available){$('contDistBody').innerHTML='<p class="help">NAS 관제를 사용할 수 없어 분배할 수 없습니다.</p>';return}
  const checked=new Set([...$('contDistBody').querySelectorAll('[data-dist-pc]:checked')].map(i=>i.dataset.distPc));
  const first=!$('contDistBody').querySelector('[data-dist-pc]');
  const date=$('contDistDate')?$('contDistDate').value:'';
  $('contDistBody').innerHTML='<p class="help">남은 RAW(어느 PC도 배정·시작·제외·분배하지 않은 것, 촬영일 기준) <b>'+d.pool+'개</b>. 고른 PC마다 시작 전 분배 개수가 같아지도록 오래된 촬영부터 나눕니다. 개수 제한은 없습니다.</p>'+
   '<div class="contDistRow"><label>촬영일 <select id="contDistDate"><option value="">전체 ('+d.pool+'개)</option>'+Object.entries(d.dates).map(([k,v])=>'<option value="'+k+'"'+(k===date?' selected':'')+'>'+k+' ('+v+'개)</option>').join('')+'</select></label></div>'+
   '<table class="dbTable contDistTable"><thead><tr><th>분배</th><th>PC</th><th>시작 전</th><th>시작함</th><th></th></tr></thead><tbody>'+
   d.pcs.map(p=>'<tr><td><input type="checkbox" data-dist-pc="'+p.pc+'"'+((first?p.online:checked.has(p.pc))?' checked':'')+'></td><td><b>'+p.pc.toUpperCase()+'</b>'+(p.online?'':' <span class="help">연결 끊김</span>')+'</td><td>'+p.waiting+'</td><td>'+p.started+'</td><td>'+(p.waiting?'<button type="button" class="smallbtn" data-dist-recall="'+p.pc+'">시작 전 회수</button>':'')+'</td></tr>').join('')+
   '</tbody></table><button type="button" class="primary" id="contDistGo" style="width:auto">고른 PC에 균등 분배</button>';
  $('contDistGo').onclick=contDistribute;
 }catch(e){$('contDistBody').innerHTML='<p class="help">'+escape(e.message)+'</p>'}
 finally{contDistBusy=false}
}
async function contDistribute(){
 const pcs=[...$('contDistBody').querySelectorAll('[data-dist-pc]:checked')].map(i=>i.dataset.distPc),date=$('contDistDate').value;
 if(!pcs.length){notice('분배할 PC를 고르세요.',true);return}
 if(!confirm((date||'전체 날짜')+'의 남은 RAW를 '+pcs.map(p=>p.toUpperCase()).join(', ')+'에 균등 분배합니다.\n\n· 분배한 RAW는 각 PC 작업자의 SAM3 연속 모드 목록에 나타납니다.\n· 시작 전이면 PC별로 회수할 수 있습니다.\n\n진행할까요?'))return;
 $('contDistGo').disabled=true;
 try{const r=await api('/api/continuous/distribute',{method:'POST',body:JSON.stringify({pcs,date})});
  notice('균등 분배: 총 '+r.total+'개 · '+Object.entries(r.given).map(([p,n])=>p.toUpperCase()+' +'+n).join(', ')+' (시작 전 합계 '+Object.entries(r.load).map(([p,n])=>p.toUpperCase()+' '+n).join(', ')+')')}
 catch(e){notice(e.message,true)}
 await loadContDist();
}
$('contDistBody').addEventListener('click',async e=>{
 const b=e.target.closest('[data-dist-recall]');if(!b)return;const pc=b.dataset.distRecall;
 if(!confirm(pc.toUpperCase()+'에 분배한 RAW 중 아직 시작하지 않은 것을 모두 회수합니다.\n그 PC에서 포인트를 찍어 대기 중인 항목도 시작하지 않습니다.\n\n회수할까요?'))return;
 b.disabled=true;
 try{const r=await api('/api/continuous/recall',{method:'POST',body:JSON.stringify({pc})});notice(pc.toUpperCase()+' · '+r.recalled+'개 회수했습니다.')}catch(err){notice(err.message,true)}
 await loadContDist();
});
$('contDistRefresh').onclick=loadContDist;
setInterval(()=>{if(page==='fleet'&&!document.hidden)loadContDist()},15000);
document.querySelector('nav [data-page="fleet"]').addEventListener('click',()=>setTimeout(loadContDist,0));
setTimeout(()=>{if(page==='fleet')loadContDist()},2500);

// ---- SAM 이후 · MANO 일괄 실행: 3D 복원까지 끝나 MANO 대기가 된 실행을 오늘 자/전체로 바로 일괄 실행 ----
let contMano={today:[],all:[],at:0},contManoBusy=false,contManoStarting=false;
async function loadContMano(force=false){
 if(contManoBusy||$('contCard').hidden||(!force&&Date.now()-contMano.at<30000))return;contManoBusy=true;
 try{
  const [rows,st]=await Promise.all([api('/api/pc1/mano'),api('/api/pc1/mano/status').catch(()=>null)]);
  const today=localDateKey(new Date()),todayRows=rows.filter(r=>localDateKey(r.ready_at)===today);
  contMano={today:todayRows.map(r=>r.run),all:rows.map(r=>r.run),at:Date.now()};
  const s=st&&st.selected,running=!!(s&&s.can_stop);
  let state;
  if(running){const c=s.current;state='<b>MANO 일괄 실행 진행 중</b>'+(c?' · ['+c.index+'] '+escape(c.episode)+' · '+escape(c.label||'')+' '+escape(stateName(c.state==='not_started'?'none':c.state)):'')+(s.queue&&s.queue.length?' · 남은 '+s.queue.length+'개':'')}
  else if(s){const b=(st.batches||[]).find(x=>x.run_id===s.run_id)||{};state='마지막 일괄 실행: '+(b.start?new Date(b.start).toLocaleString('ko-KR'):'—')+' · '+(b.count||0)+'개 · '+escape(stateName(b.state))}
  else state='아직 MANO 일괄 실행 기록이 없습니다.';
  $('contManoState').innerHTML=state+'<br>MANO 대기: 오늘 자 <b>'+todayRows.length+'개</b> · 전체 '+rows.length+'개';
  const started=(contData&&contData.queue||[]).filter(r=>r.state==='started').length;
  $('contManoFlow').textContent=started?'오늘 연속 모드로 시작 '+started+'개 → 검수·3D 복원을 마치면 MANO 대기(오늘 자)로 들어옵니다.':'';
  $('contManoToday').disabled=running||contManoStarting||!todayRows.length;$('contManoAll').disabled=running||contManoStarting||!rows.length;
  $('contManoToday').textContent='▶ 오늘 자 MANO 일괄 실행 ('+todayRows.length+'개)';$('contManoAll').textContent='▶ MANO 대기 전체 실행 ('+rows.length+'개)';
  if(running){$('contManoToday').title=$('contManoAll').title='MANO 일괄 실행이 진행 중입니다. 끝난 뒤 시작할 수 있습니다.'}else{$('contManoToday').title=$('contManoAll').title=''}
 }catch(e){$('contManoState').textContent='MANO 대기 목록을 불러오지 못했습니다: '+e.message}
 finally{contManoBusy=false}
}
async function contManoRun(list,what){
 const runs=list.slice(0,100);if(!runs.length||contManoStarting)return;
 const redo=$('contManoRedo').checked;
 if(!confirm(what+' '+runs.length+'개를 MANO 일괄 실행합니다.'+(list.length>runs.length?'\n(한 번에 최대 100개 · 나머지 '+(list.length-runs.length)+'개는 다음에 실행하세요)':'')+(redo?'\n업데이트 전 코드로 만든 3D 결과는 3D 복원부터 다시 합니다(이전 결과 보관).':'')+'\n\n실행할까요?'))return;
 contManoStarting=true;$('contManoToday').disabled=$('contManoAll').disabled=true;
 try{
  const r=await api('/api/pc1/mano',{method:'POST',body:JSON.stringify({runs,redo_3d:redo})});
  notice(r.count+'개 에피소드의 MANO 일괄 실행을 시작했습니다.'+(r.redo_3d?' · 업데이트 전 3D 결과는 3D 복원부터 다시 합니다.':''));
  if(typeof loadMano==='function'&&$('manoQueue').open)loadMano();
 }catch(e){notice(e.message,true)}
 finally{contManoStarting=false;loadContMano(true)}
}
$('contManoToday').onclick=()=>contManoRun(contMano.today,'오늘('+localDateKey(new Date())+') MANO 대기가 된 에피소드');
$('contManoAll').onclick=()=>contManoRun(contMano.all,'MANO 대기 에피소드 전체');
$('contManoOpen').onclick=()=>{show('jobs');$('manoQueue').open=true;if(typeof loadMano==='function'){loadMano();loadManoLog()}};
setInterval(()=>{if(page==='run'&&!document.hidden)loadContMano()},10000);
