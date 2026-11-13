let intakeData=[];
const intakeState=r=>r.state==='ready'?(r.operator?'배정 완료':'업로드 검증 완료'):r.state==='invalid'?'입고 오류':'업로드 중 · '+r.progress+'%';
async function loadIntake(){
 try{const d=await api('/api/intake');intakeData=d.episodes;$('intakeMessage').textContent=d.configured?'파일 복사가 끝나면 입고 검사를 누르세요.':'NAS RAW 폴더 미설정 · 연결 후 노트북 업로드가 표시됩니다.';
 $('intakeRows').innerHTML=intakeData.map((r,i)=>'<tr><td>'+escape(r.metadata.capture_date||'—')+'<br><strong>'+escape(r.episode)+'</strong><br><small>업로드: '+escape(r.metadata.uploader||'—')+'</small></td><td>'+escape(r.metadata.calibration_id||'미연결')+'</td><td>'+escape(intakeState(r))+'<br><small>'+escape(r.message)+'</small></td><td>'+escape(r.operator||'미배정')+'<br>'+escape(r.worker||'—')+'</td></tr>').join('')||'<tr><td colspan="4">입고된 에피소드가 없습니다.</td></tr>';
 renderMyWork();
 }catch(e){$('intakeMessage').textContent=e.message;notice(e.message,true)}
}
function renderMyWork(){const name=$('myOperator').value.trim();localStorage.setItem('kaistOperatorFilter',name);const rows=intakeData.filter(r=>r.operator===name);
 $('myWorkRows').innerHTML=rows.map((r,i)=>'<tr><td>'+escape(r.episode)+'</td><td>'+escape(r.metadata.calibration_id||'—')+'<br>PC1 · RTX4090</td><td>'+escape(r.execution_state==='unavailable'?'실행 상태 연결 확인 필요':r.execution_state?stateName(r.execution_state):intakeState(r))+(r.stage?'<br>'+escape(taskName[r.stage]||r.stage):'')+'</td><td><button class="smallbtn" data-myrun="'+i+'">실행 설정</button> <button class="smallbtn" data-myreview="'+i+'">검수</button></td></tr>').join('')||'<tr><td colspan="4">'+(name?'이 작업자에게 배정된 데이터가 없습니다.':'작업자 이름을 입력하세요.')+'</td></tr>';
 $('myWorkRows').querySelectorAll('[data-myrun]').forEach(b=>b.onclick=()=>{const r=rows[+b.dataset.myrun];show('run');const option=[...$('episode').options].find(o=>o.value===r.episode);if(!option){notice('GPU Worker가 해당 NAS 에피소드를 읽는지 연결을 확인하세요.',true);return}$('episode').value=r.episode;$('episode').dispatchEvent(new Event('change'));notice('선택된 배정 데이터: '+r.episode+'. 이미지를 확인하고 분할 대상을 입력하세요.')});
 $('myWorkRows').querySelectorAll('[data-myreview]').forEach(b=>b.onclick=()=>{const r=rows[+b.dataset.myreview];openReview(r.episode,r.stage==='tracking_approved'?'tracking':'mask')});
}
$('intakeScan').onclick=()=>scanIntake(true);
$('myOperator').value=localStorage.getItem('kaistOperatorFilter')||'';$('myRefresh').onclick=loadIntake;
async function scanIntake(manual){const b=$('intakeScan');b.disabled=true;$('intakeMessage').textContent='NAS 파일·캘리브레이션을 검증 중입니다. 큰 에피소드는 시간이 걸릴 수 있습니다.';try{
  let status=await api('/api/intake/scan?include_manual='+manual+'&background='+manual,{method:'POST'});
  while(status.running){await new Promise(resolve=>setTimeout(resolve,2000));status=await api('/api/intake/scan-status')}
  if(status.error)throw new Error(status.error);
  await loadIntake()
 }catch(e){notice(e.message,true)}finally{b.disabled=false}};
setInterval(()=>{if(page==='mywork')loadIntake()},15000);

setInterval(()=>{if(page==='intake'&&!$('intakeScan').disabled)scanIntake(false)},20000);
