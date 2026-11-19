// NAS 업로드 현황 (작업 현황 화면 'NAS 업로드' 카드 · 사이드바 'NAS 업로드')
// 업로드는 한 번에 하나씩 진행됩니다: 올리는 중 1개 + 순서 대기. 10초마다 갱신합니다.
let upBusy=false,upDoneOpen=false,upWaitOpen=true,upBadOpen=true;
const upTime=t=>t?new Date(t*1000).toLocaleTimeString('ko-KR',{hour:'2-digit',minute:'2-digit'}):'—';
const upSpeed=s=>s==null?'속도 측정 중':(s/1048576).toFixed(1)+' MB/s';
const upLeft=sec=>sec==null?'':'약 '+actDur(sec)+' 남음';
function renderUploads(d){
 const s=d.summary||{},items=d.items||[];
 const cur=items.filter(i=>i.state==='uploading'),wait=items.filter(i=>i.state==='waiting'),bad=items.filter(i=>i.state==='failed'),done=items.filter(i=>i.state==='success');
 const admin=currentUser?.role==='admin',who=i=>admin&&i.operator&&i.operator!=='—'?' <small>'+escape(i.operator)+'</small>':'';
 // 사이드바: 올리는 중이거나 기다리는 것이 있을 때만
 const live=cur.length+wait.length;
 $('upNow').classList.toggle('hidden',!d.available||!live);
 if(live){
  $('upCount').textContent=wait.length?'대기 '+wait.length+'개':'';
  $('upBody').innerHTML=cur.map(i=>{const pct=i.total?Math.min(100,Math.round(100*(i.done||0)/i.total)):0;
   return '<div class="actEp">'+escape(i.episode)+'</div><div class="gpuMemBar"><i style="width:'+pct+'%"></i></div><div class="gpuMemNums"><span>'+pct+'% · '+upSpeed(i.speed)+'</span><span>'+escape(i.eta!=null?actDur(i.eta):'')+'</span></div>'}).join('')
   ||'<div class="actSub">순서를 기다리는 중</div>';
  if(s.remaining)$('upBody').innerHTML+='<div class="actSub">남은 양 '+fmtBytes(s.remaining)+'</div>';
 }
 // 작업 현황 카드
 $('uploadQueue').classList.toggle('hidden',!d.available||!items.length);
 if(!d.available||!items.length)return;
 $('uploadSummary').textContent=[cur.length?'올리는 중 '+cur.length+'개':'',wait.length?'순서 대기 '+wait.length+'개':'',bad.length?'실패 '+bad.length+'개':'','오늘 완료 '+(s.done_today||0)+'개',
  live&&s.remaining?'남은 양 '+fmtBytes(s.remaining)+(s.eta!=null?' (지금 속도면 '+upLeft(s.eta)+')':''):''].filter(Boolean).join(' · ')||'진행 중인 업로드 없음';
 if(!$('uploadQueue').open)return;
 let h='';
 h+=cur.map(i=>{const pct=i.total?Math.min(100,100*(i.done||0)/i.total):0;
  return '<div class="upCurrent"><div><b>'+escape(i.episode)+'</b>'+who(i)+' · 올리는 중'+(i.turn?' <small class="help">('+escape(i.turn.slice(5,16))+' 시작)</small>':'')+'</div><div class="upBar"><i style="width:'+pct.toFixed(1)+'%"></i></div>'
   +'<div class="upNums"><span>'+fmtBytes(i.done||0)+' / '+fmtBytes(i.total||0)+' ('+pct.toFixed(0)+'%)</span><span>'+upSpeed(i.speed)+(i.limit?' · '+escape(i.limit):'')+'</span><span>'+escape(upLeft(i.eta))+'</span></div></div>'}).join('');
 if(!cur.length&&wait.length)h+='<p class="help">다른 업로드가 끝나기를 기다리는 중입니다.</p>';
 if(wait.length)h+='<details class="upSection" data-up="wait"'+(upWaitOpen?' open':'')+'><summary>순서 대기 <small>'+wait.length+'개 · '+fmtBytes(wait.reduce((n,i)=>n+(i.total||0),0))+' · 한 번에 하나씩 올립니다</small></summary><div class="upChips">'
  +wait.map(i=>'<span>'+escape(i.episode)+who(i)+'<small>'+fmtBytes(i.total||0)+'</small></span>').join('')+'</div></details>';
 if(bad.length)h+='<details class="upSection" data-up="bad"'+(upBadOpen?' open':'')+'><summary>실패 · 중단 <small>'+bad.length+'개 · 에피소드의 \'⇪ NAS에 올리기\'로 다시 올릴 수 있습니다</small></summary><div class="upChips">'
  +bad.map(i=>'<span class="bad" title="'+escape(i.error||'')+'">'+escape(i.episode)+who(i)+'<small>'+escape((i.error||'').slice(0,60))+'</small></span>').join('')+'</div></details>';
 if(done.length)h+='<details class="upSection" data-up="done"'+(upDoneOpen?' open':'')+'><summary>최근 24시간 완료 <small>'+done.length+'개 · '+fmtBytes(done.reduce((n,i)=>n+(i.total||0),0))+'</small></summary><div class="upChips">'
  +done.map(i=>'<span title="'+(i.local_deleted?'로컬 작업 파일 정리됨':'로컬 작업 파일 유지')+'">'+escape(i.episode)+who(i)+'<small>'+upTime(i.finished)+' · '+fmtBytes(i.total||0)+'</small></span>').join('')+'</div></details>';
 $('uploadBody').innerHTML=h;
 $('uploadBody').querySelectorAll('details[data-up]').forEach(el=>el.ontoggle=()=>{if(el.dataset.up==='wait')upWaitOpen=el.open;else if(el.dataset.up==='bad')upBadOpen=el.open;else upDoneOpen=el.open});
}
let upLast=null;
async function loadUploads(){
 if(!currentUser){$('upNow').classList.add('hidden');return}
 if(document.hidden||upBusy)return;
 upBusy=true;
 try{upLast=await api('/api/uploads');renderUploads(upLast)}
 catch(e){/* 다음 주기에 다시 시도 */}
 finally{upBusy=false}
}
$('uploadQueue').ontoggle=()=>{if(upLast)renderUploads(upLast)};
function openUploads(){show('jobs');$('uploadQueue').open=true;if(upLast)renderUploads(upLast);$('uploadQueue').scrollIntoView({behavior:'smooth',block:'start'})}
$('upNow').onclick=openUploads;$('upNow').onkeydown=e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();openUploads()}};
setInterval(loadUploads,10000);document.addEventListener('visibilitychange',loadUploads);
