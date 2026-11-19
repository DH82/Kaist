// 관리자: PC1~PC6 현황(NAS control/status/*.sqlite) · 원격 실행(관리자 확인 후 명령 전송)
let fleetTimer=null,fleetBusy=false,fleetData=null;
const FLEET_TYPES={reconstruct_redo:'3D 복원 다시 실행',raw_reset:'대기열 초기화',run_raw:'새 RAW 원격 실행',mano_export:'MANO 이관 · 업로드',mano_import:'MANO 이관 · 실행',mano_release:'이관 해제'};
const FLEET_STATES={received:'수신',running:'진행 중',done:'완료',failed:'실패'};
function fleetPage(on){clearInterval(fleetTimer);fleetTimer=null;if(on){loadFleet();fleetTimer=setInterval(()=>{if(!document.hidden)loadFleet(true)},15000)}}
function fleetMB(v){return v==null?'—':(v/1024).toFixed(1)+' GB'}
function fleetTime(t){if(!t)return '—';const d=new Date(typeof t==='number'?t*1000:t);return isNaN(d)?'—':d.toLocaleString('ko-KR',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'})}
function fleetPcOptions(exclude){return (fleetData?.pcs||[]).filter(p=>p.pc!==exclude).map(p=>'<option value="'+p.pc+'" '+(p.online?'':'disabled')+'>'+p.pc.toUpperCase()+(p.idle?' · 유휴':p.online?' · 작업 중':' · 연결 끊김')+'</option>').join('')}
function fleetBestTarget(exclude){const idle=(fleetData?.pcs||[]).find(p=>p.idle&&p.pc!==exclude);return idle?idle.pc:''}
// 실행 중 목록의 단계: 내부 이름(mask_approved 등) 대신 화면에서 쓰는 한글 이름으로 보여 줍니다.
function fleetStage(r){
 const names=typeof taskName==='object'?taskName:{};
 if(r.stage)return (names[r.stage]||r.stage)+(['sam_settings_ready','mask_approved','tracking_approved'].includes(r.stage)?' 대기':'');
 return typeof stateName==='function'?stateName(r.state):(r.state||'');
}
function fleetCard(p){
 const name=p.pc.toUpperCase();
 if(p.missing)return '<div class="fleetCard off"><h3>'+name+'<span class="fleetBadge">미설치</span></h3><p class="help">NAS에 상태 기록이 없습니다. 이 PC에 새 버전 UI를 배포하세요.</p></div>';
 if(p.error)return '<div class="fleetCard off"><h3>'+name+'<span class="fleetBadge">읽기 오류</span></h3><p class="help">'+escape(p.error)+'</p></div>';
 const g=(p.gpu||[])[0],s=p.stats||{};
 const badge=!p.online?'<span class="fleetBadge">연결 끊김 · '+Math.round(p.age/60)+'분 전</span>':p.idle?'<span class="fleetBadge idle">GPU 유휴</span>':'<span class="fleetBadge busy">작업 중</span>';
 const mem=g&&g.total_mb?Math.round(100*g.used_mb/g.total_mb):0;
 const gpu=g?'<div class="help">'+escape(g.name||'GPU')+' · 사용률 '+(g.util??'—')+'%</div><div class="fleetBar" title="GPU 사용률"><span style="width:'+(g.util||0)+'%"></span></div><div class="help">메모리 '+fleetMB(g.used_mb)+' / '+fleetMB(g.total_mb)+' · 프로세스 '+(g.processes||0)+'개</div><div class="fleetBar" title="GPU 메모리"><span style="width:'+mem+'%"></span></div>':'<p class="help">GPU 정보 없음'+(p.meta?.gpu_error?' · '+escape(p.meta.gpu_error):'')+'</p>';
 const active=(p.active_runs||[]).map(r=>'<li>'+escape(r.episode||'')+' · '+escape(fleetStage(r))+(r.operator?' · '+escape(r.operator):'')+'</li>').join('');
 return '<div class="fleetCard'+(p.online?'':' off')+'"><h3>'+name+badge+'</h3><div class="help">'+escape(p.meta?.host||'')+' · 갱신 '+fleetTime(p.meta?.updated_at)+(p.pc===fleetData?.me?' · 이 PC':p.online&&p.meta?.ui_url?' · <a href="/api/fleet/open?pc='+p.pc+'" target="_blank" rel="noopener" title="관리자로 바로 들어갑니다">원격 관리 ↗</a>':' · 화면 주소 없음')+'</div>'+gpu+
  '<dl><dt>진행 중</dt><dd>'+(s.running||0)+'개</dd><dt>최근 실행</dt><dd>성공 '+(s.success||0)+' · 실패 '+(s.failed||0)+' / '+(s.runs||0)+'</dd><dt>3D 복원 대기</dt><dd>'+(s.mano_waiting||0)+'개</dd><dt>NAS 최종 저장</dt><dd>'+(s.published||0)+'개 (오늘 '+(s.published_today||0)+')</dd></dl>'+(active?'<ul>'+active+'</ul>':'')+'<div class="help" style="margin-top:8px">시작 전 배정 '+(p.assignments||[]).length+'개'+((p.assignments||[]).length?' · '+(p.assignments||[]).map(a=>escape(a.episode)).join(', '):'')+'</div>'+(p.online?'<button class="smallbtn" style="margin-top:8px" data-fq-reset="'+p.pc+'" title="이 PC의 시작 전 배정과 내 작업 제외 기록을 모두 풉니다. 실행 기록·결과는 그대로입니다.">대기열 초기화</button>':'')+'</div>';
}
function renderFleet(){
 const d=fleetData;if(!d)return;
 const online=d.pcs.filter(p=>p.online).length,idle=d.pcs.filter(p=>p.idle).length;
 $('fleetInfo').textContent='연결 '+online+'/6대 · GPU 유휴 '+idle+'대 · 이 PC '+d.me.toUpperCase()+' · 관제 폴더 '+d.control+' · 유휴 기준: 실행 중 작업 없음, GPU 사용률 '+d.idle_rule.util_below+'% 미만, 여유 메모리 '+(d.idle_rule.free_mb_at_least/1024)+'GB 이상';
 $('fleetCards').innerHTML=d.pcs.map(fleetCard).join('');
 const kind={started:'실행',assigned:'배정(시작 전)'};
 $('fleetOverlaps').innerHTML=(d.overlaps||[]).length?d.overlaps.map(o=>'<tr><td>'+escape(o.episode)+'</td><td>'+o.holders.map(h=>h.pc.toUpperCase()+' '+kind[h.kind]).join(' · ')+'</td><td>'+(o.claim?o.claim.toUpperCase():'—')+'</td><td>'+(o.manual?'<b>수동 확인</b> (여러 PC가 실행)':o.keep.toUpperCase()+'만 남김'+(o.release.length?' · '+o.release.map(x=>x.toUpperCase()).join(', ')+' 배정 해제':''))+'</td></tr>').join(''):'<tr><td colspan="4" class="help">겹치는 RAW가 없습니다. (이전 버전 PC는 배정 목록을 올리지 않아 여기 나오지 않습니다 — 업데이트 필요)</td></tr>';
 $('fleetResolve').disabled=!(d.overlaps||[]).some(o=>!o.manual&&o.release.length);
 const moving=new Set(d.commands.filter(c=>['received','running'].includes(c.state)).map(c=>c.episode));
 $('fleetMano').innerHTML=d.mano_waiting.length?d.mano_waiting.map((m,i)=>'<tr><td>'+m.pc.toUpperCase()+'</td><td>'+escape(m.episode)+'<div class="help">'+escape(m.status||'')+'</div></td><td>'+escape(m.sam_prompt||'—')+'</td><td>'+escape(m.operator||'—')+'</td><td><select data-fm-target="'+i+'"><option value="">선택</option>'+fleetPcOptions(m.pc)+'</select></td><td><button class="smallbtn" data-fm-go="'+i+'" '+(moving.has(m.episode)?'disabled title="처리 중"':m.status&&!m.status.startsWith('MANO 대기')?'disabled title="3D 복원이 끝난(MANO 대기) 에피소드만 이관할 수 있습니다"':'')+'>이관 실행</button></td></tr>').join(''):'<tr><td colspan="6" class="help">MANO 대기 중인 에피소드가 없습니다.</td></tr>';
 d.mano_waiting.forEach((m,i)=>{const sel=document.querySelector('[data-fm-target="'+i+'"]');if(sel)sel.value=fleetBestTarget(m.pc)});
 $('fleetRaw').innerHTML=d.raw_free.length?d.raw_free.map((e,i)=>'<tr><td>'+escape(e)+'</td><td><input type="text" maxlength="200" data-fr-prompt="'+i+'" placeholder="예: towel"></td><td><select data-fr-target="'+i+'"><option value="">선택</option>'+fleetPcOptions('')+'</select></td><td><button class="smallbtn" data-fr-go="'+i+'" '+(moving.has(e)?'disabled':'')+'>원격 실행</button></td></tr>').join(''):'<tr><td colspan="4" class="help">시작 전인 RAW가 없습니다.</td></tr>';
 d.raw_free.forEach((e,i)=>{const sel=document.querySelector('[data-fr-target="'+i+'"]');if(sel)sel.value=fleetBestTarget('')});
 $('fleetCmds').innerHTML=d.commands.length?d.commands.map(c=>{
  const pcs=c.type==='mano_import'||c.type==='mano_export'?(c.source_pc||'?').toUpperCase()+' → '+(c.target_pc||'?').toUpperCase():(c.target_pc||c.source_pc||'').toUpperCase();
  const release=c.type==='mano_import'&&c.state==='failed'?'<button class="smallbtn" data-fc-release="'+escape(c.id)+'" title="원본 PC에서 다시 MANO 대기로 되돌립니다.">원본 PC로 되돌리기</button>':'';
  return '<tr><td>'+fleetTime(c.updated)+'</td><td>'+(FLEET_TYPES[c.type]||escape(c.type))+'</td><td>'+escape(c.episode||'')+'</td><td>'+pcs+'</td><td>'+(FLEET_STATES[c.state]||escape(c.state))+'</td><td>'+escape(c.message||'')+(c.requested_by?'<div class="help">요청 '+escape(c.requested_by)+'</div>':'')+'</td><td>'+release+'</td></tr>'}).join(''):'<tr><td colspan="7" class="help">원격 명령 기록이 없습니다.</td></tr>';
}
async function loadFleet(quiet){
 if(fleetBusy)return;fleetBusy=true;
 // 입력 중인 분할 대상·선택은 자동 갱신 때 지우지 않습니다.
 const typing=quiet&&document.activeElement&&document.activeElement.matches&&document.activeElement.matches('#fleet input:not([type=checkbox]):not([type=radio]),#fleet select,#fleet textarea');  // 버튼을 누른 뒤에는 갱신을 계속합니다
 try{const d=await api('/api/fleet');if(typing)return;fleetData=d;renderFleet();if(!quiet)$('fleetMsg').textContent=''}
 catch(e){$('fleetMsg').textContent=e.message;$('fleetInfo').textContent='NAS 관제 정보를 불러오지 못했습니다.'}
 finally{fleetBusy=false}
}
async function fleetSend(body,confirmText){
 if(!confirm(confirmText))return;
 try{const r=await api('/api/fleet/dispatch',{method:'POST',body:JSON.stringify(body)});$('fleetMsg').textContent='명령을 보냈습니다 → '+r.sent_to.toUpperCase()+' (최대 20초 안에 처리 시작)';setTimeout(()=>{if(document.activeElement&&document.activeElement.blur)document.activeElement.blur();loadFleet(true)},3000)}
 catch(e){$('fleetMsg').textContent=e.message}
}
$('fleetRefresh').onclick=()=>loadFleet();
$('fleetResolve').onclick=()=>{const todo=(fleetData?.overlaps||[]).filter(o=>!o.manual&&o.release.length);if(!todo.length)return;
 fleetSend({type:'resolve_overlaps'},'겹치는 RAW '+todo.length+'개를 정리합니다.\n\n'+todo.map(o=>o.episode+' → '+o.keep.toUpperCase()+'만 남김').join('\n')+'\n\n나머지 PC의 시작 전 배정만 풉니다(실행 기록·결과는 그대로). 진행할까요?')};
$('fleet').addEventListener('click',e=>{
 const b=e.target.closest('button');if(!b||!fleetData)return;
 if(b.dataset.fqReset!=null){const pc=b.dataset.fqReset;
  fleetSend({type:'queue_reset',target_pc:pc},pc.toUpperCase()+' 작업자 대기열을 초기화합니다.\n\n시작 전 배정과 \'내 작업에서 제외\' 기록을 모두 풀고 NAS 선점도 해제합니다. 실행 중이거나 실행한 RAW·결과는 그대로입니다.\n로그인한 작업자에게는 남은 RAW가 다시 자동 배정됩니다. 진행할까요?');return}
 if(b.dataset.fmGo!=null){const m=fleetData.mano_waiting[+b.dataset.fmGo],t=document.querySelector('[data-fm-target="'+b.dataset.fmGo+'"]').value;
  if(!t){$('fleetMsg').textContent='대상 PC를 고르세요.';return}
  fleetSend({type:'mano_transfer',source_pc:m.pc,target_pc:t,episode:m.episode,run_id:m.run_id},m.episode+'\n'+m.pc.toUpperCase()+' → '+t.toUpperCase()+'\n\n원본 PC 작업 폴더를 NAS에 올린 뒤 '+t.toUpperCase()+'에서 MANO · 최종 저장까지 실행합니다. 진행할까요?')}
 if(b.dataset.frGo!=null){const i=b.dataset.frGo,ep=fleetData.raw_free[+i],prompt=document.querySelector('[data-fr-prompt="'+i+'"]').value.trim(),t=document.querySelector('[data-fr-target="'+i+'"]').value;
  if(!prompt||!t){$('fleetMsg').textContent='SAM3 분할 대상과 대상 PC를 입력하세요.';return}
  fleetSend({type:'run_raw',target_pc:t,episode:ep,sam_prompt:prompt},ep+' → '+t.toUpperCase()+'\nSAM3 분할 대상: '+prompt+'\n\n'+t.toUpperCase()+'에서 전체 파이프라인을 시작합니다. 마스크 검수는 그 PC 화면에서 합니다. 진행할까요?')}
 if(b.dataset.fcRelease!=null){const c=fleetData.commands.find(x=>x.id===b.dataset.fcRelease);if(!c)return;
  fleetSend({type:'mano_release',source_pc:c.source_pc,episode:c.episode,run_id:c.run_id||''},c.episode+'\n'+(c.source_pc||'').toUpperCase()+'에서 다시 MANO 대기로 되돌릴까요?')}
});
