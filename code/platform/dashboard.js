// 관리자 통합 관리: PC1~PC6의 NAS 상태 파일(control/status/*.sqlite)을 모아
// 날짜별 작업량 · 에피소드별 처리 시간 · 단계별 소요 시간 · 작업 기록(로그)을 보여줍니다.
// 날짜 경계는 이 브라우저의 시간대(한국 시간) 기준입니다.
let dashTimer=null,dashBusy=false,dashData=null,dashLimit=200,dashTab='pcs';
const dashOpen=new Set();
const DASH_CHECKS=new Set(['sam_settings_ready','mask_approved','tracking_approved']);  // 검수 확인(수 초) 단계
const DASH_MAIN=['prepare_raw','sam3','preflight','track','prepare_reconstruction','reconstruct','adopt','hands','validate','publish'];
const DASH_STATES={success:'성공',failed:'실패',interrupted:'중단',running:'진행 중',queued:'GPU 대기',waiting:'검수 대기',not_started:'재시도 준비'};
// 앞 단계 종료 → 다음 단계 시작 사이 대기
const DASH_WAITS=[['mask','마스크 검수 대기','sam3','preflight'],['tracking','추적 검수 대기','track','prepare_reconstruction'],['mano','MANO 대기','reconstruct','hands']];
// 에피소드 진행 막대의 구간(색)
const DASH_PHASES=[['전처리',['prepare_raw'],'#c9c3f5'],['SAM3',['sam3'],'#8f80e8'],['추적',['preflight','track'],'#5fb3d9'],['3D 복원',['prepare_reconstruction','reconstruct'],'#4b9b7a'],['MANO',['adopt','hands'],'#e3a548'],['검사·저장',['validate','publish'],'#d9788c']];
function dashName(s){return (typeof taskName!=='undefined'&&taskName[s])||({adopt:'이관 받기'})[s]||s}
function dashPage(on){clearInterval(dashTimer);dashTimer=null;clearInterval(updTimer);updTimer=null;if(on){loadUpd(true);updTimer=setInterval(()=>{if(!document.hidden)loadUpd()},5000);if(!$('dashDate').value)$('dashDate').value=dashDay(Date.now()/1000);dashSetTab(dashTab);loadDash();dashTimer=setInterval(()=>{if(!document.hidden)loadDash(true)},30000)}}
function dashDay(t){const d=new Date(t*1000);return d.getFullYear()+'-'+String(d.getMonth()+1).padStart(2,'0')+'-'+String(d.getDate()).padStart(2,'0')}
function dashDur(s){if(s==null||!isFinite(s)||s<0)return '—';s=Math.round(s);if(s<60)return s+'초';const m=Math.floor(s/60);if(m<60)return m+'분 '+String(s%60).padStart(2,'0')+'초';const h=Math.floor(m/60);return h+'시간 '+String(m%60).padStart(2,'0')+'분'}
function dashHM(t){if(!t)return '';const d=new Date(t*1000);return String(d.getHours()).padStart(2,'0')+':'+String(d.getMinutes()).padStart(2,'0')}
function dashClock(t){if(!t)return '—';const d=new Date(t*1000);return (d.getMonth()+1)+'/'+d.getDate()+' '+dashHM(t)}
function dashEnd(j){return j.finished||(['running','queued'].includes(j.state)?Date.now()/1000:null)}
function dashLen(j){const e=dashEnd(j);return e&&j.started?e-j.started:null}
function dashMedian(a){if(!a.length)return null;const s=[...a].sort((x,y)=>x-y),m=s.length>>1;return s.length%2?s[m]:(s[m-1]+s[m])/2}
function dashBase(run){return String(run||'').replace(/^transfer__pc\d__/,'')}
function dashState(s){return '<span class="dbState '+escape(s||'')+'">'+escape(DASH_STATES[s]||s||'—')+'</span>'}
function dashPcName(pc){return String(pc||'').toUpperCase()}
function dashNum(n,bad){return '<td class="dbR'+(!n?' zero':bad?' bad':'')+'">'+n+'</td>'}
function dashSetTab(t){dashTab=t;document.querySelectorAll('[data-dash-tab]').forEach(b=>b.setAttribute('aria-selected',String(b.dataset.dashTab===t)));document.querySelectorAll('[data-dash-panel]').forEach(p=>p.hidden=p.dataset.dashPanel!==t);try{localStorage.setItem('kaistDashTab',t)}catch(e){}}
try{const t=localStorage.getItem('kaistDashTab');if(['pcs','eps','stages','log','raws'].includes(t))dashTab=t}catch(e){}

// 에피소드(실행) 단위로 묶기: 이관된 실행(transfer__<원본>__<run>)은 원본 실행과 합칩니다.
function dashEpisodes(jobs){
 const eps=new Map();
 for(const j of jobs){
  const key=j.episode+'|'+dashBase(j.run_id);let e=eps.get(key);
  if(!e){e={key,episode:j.episode,pcs:[],operator:null,sam_prompt:null,stages:{},retries:0,jobs:[]};eps.set(key,e)}
  e.jobs.push(j);if(!e.pcs.includes(j.pc))e.pcs.push(j.pc);e.operator=e.operator||j.operator;e.sam_prompt=e.sam_prompt||j.sam_prompt;
  if(j.attempt>0){e.retries++;continue}
  e.stages[j.stage]=j;
 }
 const now=Date.now()/1000;
 for(const e of eps.values()){
  const st=e.stages;
  e.start=Math.min(...e.jobs.map(j=>j.started));
  e.running=Object.values(st).some(j=>['running','queued'].includes(j.state));
  e.failed=Object.values(st).some(j=>['failed','interrupted'].includes(j.state));
  e.done=st.publish?.state==='success';
  e.to3d=st.reconstruct?.state==='success'&&st.prepare_raw?.started?st.reconstruct.finished-st.prepare_raw.started:null;
  e.total=e.done?st.publish.finished-e.start:null;
  e.work=Object.values(st).filter(j=>!DASH_CHECKS.has(j.stage)).reduce((s,j)=>s+(dashLen(j)||0),0);
  e.waits={};e.open=null;
  for(const [k,,a,b] of DASH_WAITS){const A=st[a],B=st[b];
   if(A?.state==='success'&&A.finished){if(B?.started)e.waits[k]=B.started-A.finished;else if(!e.running){e.waits[k]=now-A.finished;e.open=k}}}
  e.waitSum=Object.values(e.waits).reduce((a,b)=>a+b,0);
  const order=pc=>Math.min(...e.jobs.filter(j=>j.pc===pc).map(j=>j.started));
  e.pcs.sort((x,y)=>order(x)-order(y));
 }
 return [...eps.values()];
}
function dashEpStatus(e){
 if(e.running)return ['running','진행 중'];
 if(e.failed)return ['failed','실패'];
 if(e.done)return ['success','최종 저장'];
 return e.open?['waiting',{mask:'마스크 검수',tracking:'추적 검수',mano:'MANO 대기'}[e.open]]:['','—'];
}
function dashEpRow(e){
 const [cls,label]=dashEpStatus(e),st=e.stages;
 const segs=DASH_PHASES.map(([name,stages,color])=>{const t=stages.reduce((s,x)=>s+(st[x]?dashLen(st[x])||0:0),0);return t>0?'<i style="flex:'+t+';background:'+color+'" title="'+name+' '+dashDur(t)+'"></i>':''}).join('');
 const times='<b>'+(e.total!=null?'전체 '+dashDur(e.total):'처리 '+dashDur(e.work))+'</b><br>'+(e.total!=null?'처리 '+dashDur(e.work)+' · ':'')+(e.waitSum?'대기 '+dashDur(e.waitSum):'대기 없음');
 let detail='';
 if(dashOpen.has(e.key)){
  const cells=DASH_MAIN.filter(s=>st[s]).map(s=>{const j=st[s];return '<div class="'+(['failed','interrupted'].includes(j.state)?'bad':'')+'" title="'+escape(dashPcName(j.pc)+' · '+dashClock(j.started)+' ~ '+(j.finished?dashClock(j.finished):'진행 중'))+'"><span>'+escape(dashName(s))+'</span><span>'+(j.state==='success'?'':DASH_STATES[j.state]+' · ')+dashDur(dashLen(j))+'</span></div>'});
  for(const [k,label] of DASH_WAITS)if(e.waits[k]!=null)cells.push('<div><span>'+label+'</span><span>'+(e.open===k?'⏳ ':'')+dashDur(e.waits[k])+'</span></div>');
  if(e.to3d!=null)cells.push('<div><span>RAW → 3D 복원</span><span>'+dashDur(e.to3d)+'</span></div>');
  if(e.retries)cells.push('<div class="bad"><span>재시도</span><span>'+e.retries+'회</span></div>');
  detail='<div class="dbDetail">'+cells.join('')+'</div>';
 }
 return '<div class="dbEp" data-ep="'+escape(e.key)+'"><div><strong>'+escape(e.episode)+'</strong><span class="dbSub">'+e.pcs.map(dashPcName).join(' → ')+' · '+escape(e.sam_prompt||'분할 대상 없음')+' · '+dashClock(e.start)+' 시작</span></div><div>'+'<span class="dbState '+cls+'">'+escape(label)+'</span></div><div class="dbTrack">'+segs+'</div><div class="dbTimes">'+times+'</div>'+detail+'</div>';
}
function renderDash(){
 const d=dashData;if(!d)return;
 const day=$('dashDate').value||dashDay(Date.now()/1000),isToday=day===dashDay(Date.now()/1000);
 // PC 선택
 const pcSel=$('dashPc'),keep=pcSel.value;
 pcSel.innerHTML='<option value="">PC 전체</option>'+d.pcs.map(p=>'<option value="'+p.pc+'">'+dashPcName(p.pc)+'</option>').join('');pcSel.value=keep;
 const only=pcSel.value,mine=j=>!only||j.pc===only;
 const jobs=d.jobs.filter(j=>j.started);
 const onDay=j=>dashDay(j.started)===day,endDay=j=>j.finished&&dashDay(j.finished)===day,cur=j=>j.attempt===0;
 const dayJobs=jobs.filter(onDay);
 const work=dayJobs.filter(j=>!DASH_CHECKS.has(j.stage)&&j.state!=='waiting');
 const recon=jobs.filter(j=>cur(j)&&j.stage==='reconstruct'&&j.state==='success'&&endDay(j));
 const mano=jobs.filter(j=>cur(j)&&j.stage==='hands'&&j.state==='success'&&endDay(j));
 const pub=d.published.filter(p=>p.at&&dashDay(p.at)===day);
 const fails=dayJobs.filter(j=>['failed','interrupted'].includes(j.state));
 const starts=dayJobs.filter(j=>cur(j)&&j.stage==='prepare_raw');
 const running=jobs.filter(j=>cur(j)&&['running','queued'].includes(j.state));
 const sum=a=>a.reduce((s,j)=>s+(dashLen(j)||0),0),uniq=a=>new Set(a.map(j=>j.episode)).size;
 // 상단 정보 · 경고
 const online=d.pcs.filter(p=>p.online).length,old=d.pcs.filter(p=>p.online&&!p.has_jobs).map(p=>dashPcName(p.pc));
 $('dashInfo').textContent=(d.nas?'PC 연결 '+online+'/'+d.pcs.length+'대':'NAS 연결 없음 · '+dashPcName(d.me)+' 기록만 표시')+' · 진행 중 '+running.filter(mine).length+'단계 · MANO 대기 '+d.mano_waiting.filter(mine).length+'개 · 남은 RAW '+d.raw_free+'개'+dashLatest(d)+' · 30초마다 갱신';
 $('dashWarn').hidden=!old.length;$('dashWarn').textContent=old.length?old.join(', ')+'는 이전 버전 UI라 작업 시간 기록이 없습니다. 해당 PC의 UI를 업데이트하세요.':'';
 // 요약 카드
 const kpi=(label,val,sub,bad)=>'<div class="dbKpi'+(bad?' bad':'')+'"><span>'+label+'</span><b>'+val+'</b><small>'+sub+'</small></div>';
 const to3d=dashMedian(dashEpisodes(jobs).filter(e=>e.to3d&&mine(e.stages.reconstruct)&&endDay(e.stages.reconstruct)).map(e=>e.to3d));
 $('dashKpis').innerHTML=kpi('시작한 에피소드',uniq(starts.filter(mine)),(isToday?'오늘':day.slice(5).replace('-','/'))+' RAW 전처리 시작')+
  kpi('3D 복원 완료',recon.filter(mine).length,to3d!=null?'중앙값 '+dashDur(to3d):'완료 없음')+
  kpi('MANO 완료',mano.filter(mine).length,'손 피팅 성공')+
  kpi('최종 저장',pub.filter(mine).length,'NAS results')+
  kpi('실패',fails.filter(mine).length,'실행 시간 '+dashDur(sum(work.filter(mine))),fails.filter(mine).length>0);
 // PC별
 $('dashPcs').innerHTML=d.pcs.map(p=>{
  const of=j=>j.pc===p.pc,f=fails.filter(of).length;
  const dot=p.missing||p.error||!p.online?'':p.idle?'idle':'busy';
  const state=p.missing?'미설치':p.error?'읽기 오류':!p.online?'연결 끊김':p.idle?'GPU 유휴':'작업 중';
  const gpu=p.gpu?' · GPU '+(p.gpu.util??'—')+'% · '+((p.gpu.used_mb||0)/1024).toFixed(1)+'/'+Math.round((p.gpu.total_mb||0)/1024)+'GB':'';
  const now=running.filter(of).map(j=>escape(j.episode)+' · '+escape(dashName(j.stage))+' '+dashDur(dashLen(j))).join('<br>');
  const wait=d.mano_waiting.filter(of).length;
  return '<tr'+(dot?'':' class="off"')+'><td><b><span class="dbDot '+dot+'"></span>'+dashPcName(p.pc)+'</b>'+(p.online&&p.pc!==d.me?' <a class="dbLink" style="padding:0 4px" href="/api/fleet/open?pc='+p.pc+'" target="_blank" rel="noopener" title="'+dashPcName(p.pc)+' 화면을 관리자로 열기">원격 관리 ↗</a>':'')+'<span class="dbSub">'+state+gpu+'</span></td>'+
   dashNum(uniq(starts.filter(of)))+dashNum(recon.filter(of).length)+dashNum(mano.filter(of).length)+dashNum(pub.filter(of).length)+dashNum(f,true)+
   (sum(work.filter(of))?'<td class="dbR">'+dashDur(sum(work.filter(of)))+'</td>':'<td class="dbR zero">—</td>')+'<td class="dbNow">'+(now||'<span class="zero">—</span>')+(wait?'<span class="dbSub">MANO 대기 '+wait+'개</span>':'')+'</td><td class="dbVer">'+dashVersion(p)+'</td></tr>'}).join('');
 // 에피소드
 const all=dashEpisodes(jobs.filter(mine));
 const onlyRun=$('dashEpRunning').checked;
 const eps=all.filter(e=>(e.jobs.some(j=>onDay(j)&&!DASH_CHECKS.has(j.stage))||(isToday&&e.running))&&(!onlyRun||e.running)).sort((a,b)=>b.start-a.start);
 $('dashLegend').innerHTML=DASH_PHASES.map(([n,,c])=>'<span><i style="background:'+c+'"></i>'+n+'</span>').join('')+'<span>· 행을 누르면 단계별 시간</span>';
 $('dashEps').innerHTML=eps.length?eps.map(dashEpRow).join(''):'<p class="dbEmpty">이 날짜에 실행된 에피소드가 없습니다.</p>';
 // 단계별 시간
 const done=jobs.filter(j=>mine(j)&&j.state==='success'&&endDay(j)&&!DASH_CHECKS.has(j.stage));
 const groups=[];
 const stages=DASH_MAIN.concat([...new Set(jobs.map(j=>j.stage))].filter(s=>!DASH_MAIN.includes(s)&&!DASH_CHECKS.has(s)));
 groups.push(['처리 단계',stages.map(s=>[dashName(s),done.filter(j=>j.stage===s).map(dashLen),fails.filter(j=>mine(j)&&j.stage===s).length])]);
 const waitRows=DASH_WAITS.map(([k,label,a])=>[label,all.filter(e=>e.waits[k]!=null&&e.open!==k&&endDay(e.stages[a])).map(e=>e.waits[k]),0]);
 waitRows.push(['에피소드 전체 (시작 → 최종 저장)',all.filter(e=>e.total&&endDay(e.stages.publish)).map(e=>e.total),0]);
 groups.push(['대기 · 전체',waitRows]);
 let html='';
 for(const [title,rows] of groups){
  // 1~2초짜리 준비 단계(사전 검사·복원 준비 등)는 실패가 없으면 숨깁니다.
  const shown=rows.filter(r=>r[2]||(r[1].length&&Math.max(...r[1])>=3));if(!shown.length)continue;
  const maxMean=Math.max(1,...shown.filter(r=>r[1].length).map(r=>r[1].reduce((a,b)=>a+b,0)/r[1].length));  // 막대 길이는 묶음 안에서 비교
  html+='<tr class="dbGroup"><td colspan="6">'+title+'</td></tr>'+shown.map(([name,v,f])=>{const mean=v.length?v.reduce((a,b)=>a+b,0)/v.length:null;
   return '<tr><td>'+escape(name)+'</td>'+dashNum(v.length)+'<td><div class="dbMean"><i style="width:'+(mean?Math.max(2,Math.round(140*mean/maxMean)):0)+'px"></i><span>'+dashDur(mean)+'</span></div></td><td class="dbR">'+dashDur(dashMedian(v))+'</td><td class="dbR">'+(v.length?dashDur(Math.min(...v))+' ~ '+dashDur(Math.max(...v)):'—')+'</td>'+dashNum(f,true)+'</tr>'}).join('');
 }
 $('dashStages').innerHTML=html||'<tr><td colspan="6" class="dbEmpty">이 날짜에 끝난 단계가 없습니다.</td></tr>';
 // 작업 기록
 const fst=$('dashFStage'),keepSt=fst.value;
 fst.innerHTML='<option value="">모든 단계</option>'+[...new Set(DASH_MAIN.concat(jobs.map(j=>j.stage)))].map(s=>'<option value="'+escape(s)+'">'+escape(dashName(s))+'</option>').join('');fst.value=keepSt;
 const log=dashLogRows();
 $('dashLogInfo').textContent=log.length+'건';
 $('dashLog').innerHTML=log.length?log.slice(0,dashLimit).map(j=>{
  const q=j.created&&j.started&&j.started-j.created>1?'GPU 자리 대기 '+dashDur(j.started-j.created):'';
  return '<tr title="'+escape([j.operator&&'작업자 '+j.operator,j.sam_prompt&&'분할 대상 '+j.sam_prompt,q].filter(Boolean).join(' · '))+'"><td class="dbR" style="text-align:left">'+dashHM(j.started)+' – '+(j.finished?dashHM(j.finished):'')+'</td><td>'+dashPcName(j.pc)+'</td><td>'+escape(j.episode||'')+'</td><td>'+escape(dashName(j.stage))+(j.attempt>0?'<span class="dbSub">이전 시도</span>':'')+'</td><td>'+dashState(j.state)+'</td><td class="dbR"><b>'+dashDur(dashLen(j))+'</b></td></tr>'}).join(''):'<tr><td colspan="6" class="dbEmpty">조건에 맞는 기록이 없습니다.</td></tr>';
 $('dashMore').hidden=log.length<=dashLimit;
 renderDashRaws();
}
// ---- RAW 프레임: NAS RAW마다 카메라별 프레임 수 ----
let dashRawRetry=null;
function dashCamName(c){const n=String(c).match(/(\d+)$/);return n?'카메라 '+n[1]:c}
function dashRawRows(){
 const d=dashData;if(!d||!d.raws)return [];
 const day=$('dashDate').value,pc=$('dashPc').value,all=$('dashRawAll').checked,q=$('dashRawText').value.trim().toLowerCase();
 return d.raws.filter(r=>(all||dashDay(r.at)===day)&&(!pc||r.pc===pc)&&(!q||r.episode.toLowerCase().includes(q)||String(r.object||'').toLowerCase().includes(q)));
}
const DASH_FPS=30;
function dashSec(n){return n.toLocaleString(undefined,{maximumFractionDigits:1})+'초'}
function dashRawStats(r){const v=r.cameras.map(c=>r.frames[c]).filter(n=>n!=null);return {min:v.length?Math.min(...v):null,max:v.length?Math.max(...v):null,missing:r.cameras.length-v.length}}
function renderDashRaws(){
 if(dashData&&dashData.raws===null){$('dashRawHead').innerHTML='';$('dashRawInfo').textContent='';$('dashRawSum').innerHTML='';$('dashRaws').innerHTML='<tr><td class="dbEmpty">NAS RAW 폴더를 읽는 중입니다. 잠시 후 자동으로 표시됩니다.</td></tr>';clearTimeout(dashRawRetry);dashRawRetry=setTimeout(()=>{if(dashTimer)loadDash(true)},5000);return}
 const rows=dashRawRows(),idx=dashRunIndex(),pc=$('dashPc').value;
 const every=dashData.raws.filter(r=>!pc||r.pc===pc);
 const sum=(list,k)=>{let f=0,n=0;for(const r of list){const v=dashRawStages(r,idx)[k];if(v!=null){f+=v;n++}}return [f,n]};
 const scope=$('dashRawAll').checked?'모든 날짜':$('dashDate').value.slice(5).replace('-','/')+' 촬영';
 // 단계별 합계 카드: 큰 숫자 = 선택한 날짜(또는 모든 날짜)의 30fps 길이, 아래 = 프레임 · RAW 수 · 전체 누적
 $('dashRawSum').innerHTML=DASH_FSTAGES.map(([k,label])=>{const [f,n]=sum(rows,k),[af]=sum(every,k);
  return '<div class="dbKpi"><span>'+label+'</span><b>'+dashSec(f/DASH_FPS)+'</b><small>'+f.toLocaleString()+'프레임 · '+n+'개<br>전체 누적 '+dashSec(af/DASH_FPS)+(af/DASH_FPS>=60?' ('+dashDur(af/DASH_FPS)+')':'')+'</small></div>'}).join('');
 const warn=rows.filter(r=>r.failed.length||dashRawStats(r).missing).length;
 $('dashRawInfo').textContent=rows.length+'개 RAW · '+scope+(pc?' · '+dashPcName(pc):'')+(warn?' · 카메라 누락 '+warn+'개':'');
 $('dashRawHead').innerHTML='<tr><th>촬영</th><th>RAW</th><th>대상</th>'+DASH_FSTAGES.map(([,label])=>'<th class="dbR">'+label+'</th>').join('')+'<th>PC</th></tr>';
 $('dashRaws').innerHTML=rows.length?rows.map(r=>{
  const st=dashRawStages(r,idx),s=dashRawStats(r);
  const cams=r.cameras.map(c=>{const n=r.frames[c],bad=r.failed.includes(c)||n==null;return '<span'+(bad?' class="bad" title="'+escape(dashCamName(c))+': 촬영 실패 또는 프레임 정보 없음"':' title="'+escape(dashCamName(c))+'"')+'>'+(n==null?'×':n)+'</span>'}).join(' / ');
  const cell=(k)=>{const v=st[k];if(v==null)return '<td class="dbR zero">—</td>';
   let sub=dashSec(v/DASH_FPS);
   if(k==='raw')sub+=(s.max!==s.min?' · 최대 '+s.max:'')+'<br>'+cams;
   if(k==='masks'&&st.object!=null&&st.object<v)sub+='<br>물체 '+st.object;
   return '<td class="dbR"><b>'+v.toLocaleString()+'</b><span class="dbSub">'+sub+'</span></td>'};
  const obj=r.object?escape(r.object)+(r.interaction?'<span class="dbSub">'+escape(r.interaction)+'</span>':''):'<span class="zero">—</span>';
  // 같은 RAW를 여러 번 실행했으면 가장 멀리 진행한 실행(출력 이름)을 표시합니다.
  const run=st.runs>1||(st.run&&st.run!==r.episode)?'<span class="dbSub" title="단계별 숫자는 이 실행 기준">'+escape(st.run||'')+(st.runs>1?' · 실행 '+st.runs+'개 중':'')+'</span>':'';
  return '<tr><td class="dbR" style="text-align:left" title="'+(r.duration!=null?'촬영 길이 '+escape(dashDur(r.duration)):'')+'">'+dashClock(r.at)+'</td><td>'+escape(r.episode)+(r.source==='csv'?' <span class="zero" title="촬영 앱 기록이 없어 타임스탬프 파일 줄 수로 셌습니다.">*</span>':'')+run+'</td><td>'+obj+'</td>'+
   DASH_FSTAGES.map(([k])=>cell(k)).join('')+'<td>'+(st.pc||r.pc?dashPcName(st.pc||r.pc):'<span class="zero">미시작</span>')+'</td></tr>'}).join(''):
  '<tr><td colspan="'+(4+DASH_FSTAGES.length)+'" class="dbEmpty">'+($('dashRawAll').checked?'NAS에 RAW가 없습니다.':'이 날짜에 촬영한 RAW가 없습니다.')+'</td></tr>';
}
// 처리 단계: RAW(가장 적은 카메라) → 동기화(4대 카메라 시각 맞춤) → 전처리(앞뒤 자르기 후) → SAM3 마스크 → 추적 → 3D 복원 → MANO → 최종 저장.
// 추적 이후 단계는 전처리 프레임 전체를 그대로 쓰므로(빠지는 프레임 없음) 그 단계가 성공했으면 전처리 프레임 수입니다.
const DASH_FSTAGES=[['raw','RAW'],['synced','동기화'],['prepared','전처리'],['masks','SAM3 마스크'],['track','추적'],['recon','3D 복원'],['mano','MANO'],['final','최종 저장']];
const DASH_FDONE={track:'track',recon:'reconstruct',mano:'hands',final:'publish'};
function dashRawOf(ep){const m=String(ep||'').match(/^ep_\d{8}_\d{6}/);return m?m[0]:null}
function dashRunIndex(){
 // 출력 에피소드(실행)별: 단계 프레임 수(여러 PC 기록 합침) + 성공한 단계 + 실행한 PC
 const d=dashData,runs=new Map();
 const get=ep=>{let x=runs.get(ep);if(!x){x={episode:ep,raw:dashRawOf(ep),done:new Set(),pc:null,at:0};runs.set(ep,x)}return x};
 for(const f of d.frames||[]){const x=get(f.episode);if(f.raw_episode)x.raw=f.raw_episode;for(const k of ['synced','prepared','masks','object_frames'])if(f[k]!=null)x[k]=f[k];x.pc=x.pc||f.pc}
 for(const j of d.jobs||[]){if(!j.episode)continue;const x=get(j.episode);if(j.state==='success')x.done.add(j.stage);x.at=Math.max(x.at,j.started||0);if(j.stage==='prepare_raw')x.pc=j.pc}
 for(const p of d.published||[]){if(p.episode&&runs.has(p.episode))runs.get(p.episode).done.add('publish')}
 const byRaw=new Map();
 for(const x of runs.values()){if(!x.raw)continue;if(!byRaw.has(x.raw))byRaw.set(x.raw,[]);byRaw.get(x.raw).push(x)}
 return byRaw;
}
function dashRawStages(r,idx){
 const s=dashRawStats(r),out={raw:s.min,runs:0};
 const list=idx.get(r.episode)||[];out.runs=list.length;if(!list.length)return out;
 // 가장 멀리 진행한 실행을 보여 줍니다(같으면 최근 실행).
 const reach=x=>{let n=x.prepared!=null?3:x.synced!=null?2:0;if(x.masks!=null)n=4;['track','reconstruct','hands','publish'].forEach((k,i)=>{if(x.done.has(k))n=Math.max(n,5+i)});return n};
 const best=[...list].sort((a,b)=>reach(b)-reach(a)||b.at-a.at)[0];
 Object.assign(out,{synced:best.synced,prepared:best.prepared,masks:best.masks,object:best.object_frames,run:best.episode,pc:best.pc});
 for(const [k,stage] of Object.entries(DASH_FDONE))if(best.done.has(stage)&&best.prepared!=null)out[k]=best.prepared;
 return out;
}
// ---- 버전 · GitHub 업데이트 (각 PC의 repro/update_stack.py가 NAS control/update/<pc>/status.json에 기록) ----
const DASH_UPD={idle:'',waiting:'재시작 대기',building:'빌드 중',restarting:'재시작 중',done:'완료',failed:'실패',paused:'보류'};
function dashLatest(d){const r=d.pcs.map(p=>p.update&&p.update.status).filter(x=>x&&x.remote).sort((a,b)=>b.updated-a.updated)[0];return r?' · GitHub 최신 '+r.remote.slice(0,7):''}
function dashVersion(p){
 const u=p.update||{},st=u.status,rq=u.request;
 if(!st)return '<span class="zero">업데이트 타이머 없음</span>'+(rq?'<span class="dbVerMsg">요청 대기 중 · 이 PC에 repro/install_updater.sh 설치 필요</span><button class="dbLink" data-upd-cancel="'+p.pc+'">요청 취소</button>':'');
 const now=Date.now()/1000,stale=now-(st.updated||0)>600;
 let h='<code title="'+escape((st.subject||'')+(st.mode==='native'?' · native':''))+'">'+escape(String(st.commit||'').slice(0,7))+'</code>';
 if(st.behind>0)h+=' <span class="new" title="GitHub: '+escape(st.remote_subject||'')+'">새 버전 '+st.behind+'개</span>';
 else if(st.behind===0)h+=' <span class="zero">최신</span>';
 if(st.dirty)h+=' <span class="bad" title="이 PC에서 직접 고친 파일이 있어 업데이트가 멈춥니다.">수정됨</span>';
 const label=stale?'업데이터 응답 없음':DASH_UPD[st.state];
 if(label)h+=' <span class="'+(stale||st.state==='failed'?'bad':'new')+'">'+label+'</span>';
 if(rq&&!['building','restarting','waiting'].includes(st.state))h+=' <span class="new">요청됨</span>';
 // 지난 결과(완료·실패)는 마지막 기록을 보여줍니다.
 const msg=st.message||(st.last&&!st.last.resolved&&now-st.last.at<86400?(st.last.ok?'최근 업데이트: ':'최근 실패: ')+st.last.message:'');
 if(msg)h+='<span class="dbVerMsg">'+escape(msg)+'</span>';
 h+=rq?'<button class="dbLink" data-upd-cancel="'+p.pc+'" title="재시작 전이면 업데이트를 보류합니다. 새 실행 시작 차단도 풀립니다.">요청 취소</button>'
      :'<button class="dbLink" data-upd-pc="'+p.pc+'" title="GitHub 최신 코드를 이 PC에 반영합니다. 진행 중인 작업이 끝난 뒤 재시작하며, 그동안 이 PC에서 새 실행 시작이 막힙니다.">업데이트</button>';
 return h;
}
async function dashUpdate(pcs,action){
 if(!pcs.length){alert('업데이트할 PC가 없습니다. (업데이트 타이머가 설치되어 있고 요청 대기 중이 아닌 PC)');return}
 const names=pcs.map(dashPcName).join(', ');
 if(action==='request'&&!confirm(names+'에 GitHub 최신 코드를 반영합니다.\n\n· 각 PC는 진행 중인 GPU 작업이 끝난 뒤 서비스를 재시작합니다.\n· 업데이트가 끝날 때까지 해당 PC에서 새 RAW 실행·MANO 일괄 실행을 시작할 수 없습니다.\n\n진행할까요?'))return;
 try{const r=await api('/api/fleet/update',{method:'POST',body:JSON.stringify({pcs,action})});$('dashMsg').hidden=false;$('dashMsg').textContent=(action==='request'?'업데이트 요청: ':'요청 취소: ')+(r.pcs.map(dashPcName).join(', ')||'없음');loadDash(true);loadUpd(true)}
 catch(e){alert('업데이트 요청 실패: '+e.message)}
}
$('dashPcs').addEventListener('click',e=>{const b=e.target.closest('[data-upd-pc],[data-upd-cancel]');if(!b)return;b.dataset.updPc?dashUpdate([b.dataset.updPc],'request'):dashUpdate([b.dataset.updCancel],'cancel')});
$('dashUpdateAll').onclick=()=>{const d=dashData;if(!d)return;dashUpdate(d.pcs.filter(p=>p.update&&p.update.status&&!p.update.request).map(p=>p.pc),'request')};
// ---- 업데이트 진행 패널: 요청이 걸렸거나 최근 6시간 안에 끝난 PC. 진행 중이면 5초마다 갱신 ----
let updData=null,updTimer=null,updFetched=0,updBusy=false;
const UPD_ACTIVE=['building','restarting','waiting','rollback','paused'];
const UPD_LABEL={building:'빌드 중',restarting:'재시작 중',waiting:'재시작 대기',rollback:'되돌리는 중',paused:'보류',done:'완료',failed:'실패',idle:'대기'};
const updOpen=new Set();
function updIsActive(p){return !!p.request||UPD_ACTIVE.includes(p.status?.state)}
function updRun(p){return p.status?.run||(p.request?null:p.status?.last?.run)}
function updShown(p){const st=p.status;return updIsActive(p)||!!(st&&st.last&&st.last.run&&!st.last.resolved&&Date.now()/1000-st.last.at<6*3600)}
function updAgo(t){return t?dashDur(Date.now()/1000-t):'—'}
function updPhases(p,run){
 const st=p.status,list=[...(st.phases||[])];
 if(run.phases?.rollback)list.push(['rollback','되돌리기']);
 const keys=list.map(x=>x[0]),reached=keys.filter(k=>run.phases?.[k]),lastIdx=Math.max(-1,...reached.map(k=>keys.indexOf(k)));
 const finished=!!run.finished;
 return '<div class="dbPh">'+list.map(([k,label],i)=>{
  const ph=run.phases?.[k];
  if(ph){
   const cur=!finished&&run.phase===k,end=ph.end||(cur?null:Date.now()/1000);
   const failed=finished&&!run.ok&&run.phase===k;
   return '<span class="'+(failed?'fail':cur?'cur':'ok')+'">'+(failed?'✕ ':cur?'● ':'✓ ')+escape(label)+' '+dashDur((end||Date.now()/1000)-ph.start)+'</span>';
  }
  return i<lastIdx?'<span class="skip" title="필요 없어 건너뜀">'+escape(label)+'</span>':'<span>○ '+escape(label)+'</span>';
 }).join('')+'</div>';
}
function updBuild(groups){
 if(!groups||!groups.length)return '';
 return '<div class="dbBuild">'+groups.map(g=>{const pct=g.total?Math.round(100*g.done/g.total):0;
  return '<span>'+escape(g.label)+'</span><div class="bar"><i style="width:'+pct+'%"></i></div><span>'+g.done+'/'+g.total+(g.cached?' · 캐시 '+g.cached:'')+'</span>'}).join('')+'</div>';
}
function updRow(p){
 const st=p.status,rq=p.request,name=dashPcName(p.pc);
 if(!st)return '<div class="dbUpdRow"><div class="dbUpdTop"><b>'+name+'</b><span class="dbState failed">업데이트 타이머 없음</span><span class="dbSub">요청 '+updAgo(rq?.at)+' 전 · 이 PC에서 repro/install_updater.sh를 설치해야 진행됩니다.</span></div></div>';
 const run=updRun(p),now=Date.now()/1000,stale=now-(st.updated||0)>600;
 const state=rq&&!run?'요청 접수 대기':stale&&updIsActive(p)?'업데이터 응답 없음':(UPD_LABEL[st.state]||st.state);
 const cls=stale&&updIsActive(p)||st.state==='failed'||(run&&run.finished&&!run.ok)?'failed':st.state==='done'||(run&&run.ok)?'success':updIsActive(p)?'running':'';
 let h='<div class="dbUpdRow" data-upd-row="'+p.pc+'"><div class="dbUpdTop"><b>'+name+'</b><span class="dbState '+cls+'">'+escape(state)+'</span>';
 if(run){
  const from=String(run.from||'').slice(0,7),to=String(run.to||st.commit||'').slice(0,7);
  h+='<span class="dbSub">'+(from&&from!==to?from+' → '+to+' · ':to?to+' · ':'')+(run.finished?'소요 '+dashDur(run.finished-run.started)+' · '+dashClock(run.finished)+' 종료':'시작 '+dashHM(run.started)+' · 경과 '+updAgo(run.started))+(run.requested_by?' · 요청 '+escape(run.requested_by):'')+'</span>';
 }else if(rq)h+='<span class="dbSub">요청 '+updAgo(rq.at)+' 전 · 타이머가 3분 안에 가져갑니다</span>';
 h+='</div>';
 if(run)h+=updPhases(p,run);
 const live=!run?.finished&&st.progress;
 const groups=live&&st.progress.groups?.length?st.progress.groups:run?.build?.groups;
 if(groups&&(!run?.finished||!run.ok))h+=updBuild(groups);
 else if(groups&&run?.finished)h+='<div class="dbStep">빌드 층 '+groups.map(g=>escape(g.label)+' '+g.done+'/'+g.total+(g.cached?' (캐시 '+g.cached+')':'')).join(' · ')+'</div>';
 if(live)for(const c of st.progress.current||[])h+='<div class="dbStep">▸ ['+escape(c.label)+' '+c.i+'/'+c.n+'] '+escape(c.text)+' · '+dashDur(c.elapsed)+(c.last?'<br><code>'+escape(c.last)+'</code>':'')+'</div>';
 const msg=run?.finished?(st.last?.message||''):st.message;
 if(msg)h+='<div class="dbStep">'+escape(msg)+'</div>';
 const tail=live?st.progress.tail:null;
 if(tail&&tail.length)h+='<details'+(updOpen.has(p.pc)?' open':'')+' data-upd-log="'+p.pc+'"><summary>최근 로그 '+tail.length+'줄</summary><pre>'+escape(tail.join('\n'))+'</pre></details>';
 return h+'</div>';
}
function renderUpd(){
 const box=$('dashUpd'),pcs=(updData?.pcs||[]).filter(updShown);
 box.hidden=!pcs.length;if(!pcs.length){box.innerHTML='';return}
 const n=f=>pcs.filter(f).length;
 const running=n(p=>p.status&&['building','restarting','rollback'].includes(p.status.state));
 const waiting=n(p=>updIsActive(p)&&!(p.status&&['building','restarting','rollback'].includes(p.status.state)));
 const done=n(p=>!updIsActive(p)&&p.status?.last?.ok),failed=n(p=>!updIsActive(p)&&p.status?.last&&!p.status.last.ok&&!p.status.last.resolved);
 box.innerHTML='<div class="dbUpdHead"><b>업데이트 진행</b><span class="dbUpdSum">'+(running?'<span>진행 중 '+running+'</span>':'')+(waiting?'<span>대기 '+waiting+'</span>':'')+(done?'<span>완료 '+done+'</span>':'')+(failed?'<span class="bad">실패 '+failed+'</span>':'')+'<span>'+(pcs.some(updIsActive)?'5초마다 갱신':'최근 6시간')+'</span></span></div>'+pcs.map(updRow).join('');
}
async function loadUpd(force){
 if(updBusy)return;
 const active=(updData?.pcs||[]).some(updIsActive);
 if(!force&&!active&&Date.now()-updFetched<30000)return;
 updBusy=true;
 try{updData=await api('/api/fleet/updates');updFetched=Date.now();renderUpd()}catch(e){}
 finally{updBusy=false}
}
$('dashUpd').addEventListener('toggle',e=>{const d=e.target.closest('[data-upd-log]');if(d)d.open?updOpen.add(d.dataset.updLog):updOpen.delete(d.dataset.updLog)},true);
function dashLogRows(){
 const d=dashData;if(!d)return [];
 const day=$('dashDate').value,pc=$('dashPc').value,st=$('dashFStage').value,state=$('dashFState').value,q=$('dashFText').value.trim().toLowerCase(),checks=$('dashFChecks').checked;
 return d.jobs.filter(j=>j.started&&dashDay(j.started)===day&&(!pc||j.pc===pc)&&(!st||j.stage===st)&&(checks||st||!DASH_CHECKS.has(j.stage))
  &&(!state||(state==='failed'?['failed','interrupted'].includes(j.state):j.state===state))&&(!q||String(j.episode||'').toLowerCase().includes(q)||String(j.sam_prompt||'').toLowerCase().includes(q)))
  .sort((a,b)=>b.started-a.started);
}
async function loadDash(quiet){
 if(dashBusy)return;dashBusy=true;
 try{dashData=await api('/api/fleet/report');renderDash();$('dashMsg').hidden=true}
 catch(e){if(!quiet||!dashData){$('dashMsg').hidden=false;$('dashMsg').textContent='통합 정보를 불러오지 못했습니다: '+e.message}}
 finally{dashBusy=false}
}
function dashShift(n){const d=new Date(($('dashDate').value||dashDay(Date.now()/1000))+'T12:00:00');d.setDate(d.getDate()+n);$('dashDate').value=dashDay(d.getTime()/1000);dashLimit=200;renderDash()}
$('dashPrev').onclick=()=>dashShift(-1);$('dashNext').onclick=()=>dashShift(1);
$('dashToday').onclick=()=>{$('dashDate').value=dashDay(Date.now()/1000);dashLimit=200;renderDash()};
$('dashDate').onchange=()=>{dashLimit=200;renderDash()};
$('dashRefresh').onclick=()=>loadDash();
$('dashMore').onclick=()=>{dashLimit+=200;renderDash()};
['dashPc','dashFStage','dashFState','dashFChecks','dashEpRunning','dashRawAll'].forEach(id=>$(id).onchange=()=>renderDash());
$('dashFText').oninput=()=>renderDash();
$('dashRawText').oninput=()=>renderDashRaws();
$('dashRawCsv').onclick=()=>{
 const rows=dashRawRows();if(!rows.length)return;
 const cams=[...new Set(rows.flatMap(r=>r.cameras))].sort((a,b)=>(+String(a).replace(/\D/g,'')||0)-(+String(b).replace(/\D/g,'')||0));
 const cell=v=>{v=v==null?'':String(v);return /[",\n]/.test(v)?'"'+v.replace(/"/g,'""')+'"':v};
 const idx=dashRunIndex(),sec=v=>v==null?'':Math.round(v/DASH_FPS*100)/100;
 const head=['episode','captured','object','interaction',...cams,'raw_max','duration_sec','failed_cameras',...DASH_FSTAGES.flatMap(([k])=>[k+'_frames',k+'_sec_30fps']),'mask_object_frames','run','pc','source'];
 const lines=[head.join(',')].concat(rows.map(r=>{const s=dashRawStats(r),st=dashRawStages(r,idx);return [r.episode,new Date(r.at*1000).toLocaleString('sv-SE'),r.object,r.interaction,...cams.map(c=>r.frames[c]),
  s.max,r.duration!=null?Math.round(r.duration*10)/10:'',r.failed.join(' '),...DASH_FSTAGES.flatMap(([k])=>[st[k],sec(st[k])]),st.object,st.run,st.pc||r.pc,r.source].map(cell).join(',')}));
 const a=document.createElement('a');a.href=URL.createObjectURL(new Blob(['﻿'+lines.join('\n')],{type:'text/csv'}));a.download='kaist_raw_frames_'+($('dashRawAll').checked?'all':$('dashDate').value)+'.csv';a.click();setTimeout(()=>URL.revokeObjectURL(a.href),5000);
};
document.querySelectorAll('[data-dash-tab]').forEach(b=>b.onclick=()=>dashSetTab(b.dataset.dashTab));
$('dashEps').onclick=e=>{const row=e.target.closest('.dbEp');if(!row||e.target.closest('.dbDetail'))return;const k=row.dataset.ep;dashOpen.has(k)?dashOpen.delete(k):dashOpen.add(k);renderDash()};
$('dashCsv').onclick=()=>{
 const rows=dashLogRows();if(!rows.length)return;
 const iso=t=>t?new Date(t*1000).toLocaleString('sv-SE'):'';
 const cell=v=>{v=v==null?'':String(v);return /[",\n]/.test(v)?'"'+v.replace(/"/g,'""')+'"':v};
 const head=['pc','episode','run_id','stage','stage_name','state','attempt','queued_at','started','finished','gpu_wait_sec','duration_sec','operator','sam_prompt'];
 const lines=[head.join(',')].concat(rows.map(j=>[j.pc,j.episode,j.run_id,j.stage,dashName(j.stage),j.state,j.attempt,iso(j.created),iso(j.started),iso(j.finished),
  j.created&&j.started?Math.round(j.started-j.created):'',dashLen(j)!=null?Math.round(dashLen(j)):'',j.operator,j.sam_prompt].map(cell).join(',')));
 const a=document.createElement('a');a.href=URL.createObjectURL(new Blob(['﻿'+lines.join('\n')],{type:'text/csv'}));a.download='kaist_job_times_'+$('dashDate').value+'.csv';a.click();setTimeout(()=>URL.revokeObjectURL(a.href),5000);
};
