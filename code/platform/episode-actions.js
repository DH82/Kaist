// 목록 항목 관리 (내 작업 현황 · 내 작업 결과물 · 내 검수 공용)
//  ⇪ NAS에 올리기: 지금 GPU PC 작업 폴더에 있는 이 에피소드 파일을 NAS work/<pc>에 같은 구조로 올립니다.
//  ■ 강제 종료: 대기·실행 중인 실행을 끝내 동시 실행(최대 2개) 자리를 비웁니다. 나중에 '실패 단계 다시 실행'으로 이어갈 수 있습니다.
//  🗑 목록에서 삭제: 목록에서만 숨깁니다. 결과 파일·실행 기록은 지우지 않으며 '숨긴 항목 보기'에서 되돌릴 수 있습니다.
const hiddenKeys={jobs:new Set(),outputs:new Set(),review:new Set()};
const showHidden={jobs:false,outputs:false,review:false};
const uploadState=new Map();   // episode → 'running'
const epActionBusy=new Set();
async function loadHidden(scope){
 try{const d=await api('/api/hidden?scope='+scope);hiddenKeys[scope]=new Set(d.keys)}catch(e){}
}
function isHidden(scope,key){return hiddenKeys[scope].has(key)}
function hiddenToggleHtml(scope,count){
 return '<label class="hiddenToggle"><input type="checkbox" data-hidden-toggle="'+scope+'"'+(showHidden[scope]?' checked':'')+'>숨긴 항목 보기'+(count?' ('+count+')':'')+'</label>';
}
// o={episode, scope, key, dag_id, run_id, active(bool|null), hidden(bool)}
function episodeActionsHtml(o){
 const a=(name,label,title,extra='')=>'<button type="button" class="epAction '+name+'" data-ea="'+name+'" data-episode="'+escape(o.episode)+'" data-scope="'+escape(o.scope||'')+'" data-key="'+escape(o.key||'')+'" data-dag="'+escape(o.dag_id||'')+'" data-run="'+escape(o.run_id||'')+'" title="'+escape(title)+'"'+extra+'>'+label+'</button>';
 const uploading=uploadState.get(o.episode)==='running';
 let html='<span class="epActions">';
 html+=a('upload',uploading?'⇪ 업로드 중…':'⇪ NAS에 올리기','지금 작업 폴더의 파일을 NAS에 올립니다',uploading?' disabled':'');
 if(o.active!==false)html+=a('stop','■ 강제 종료','대기·실행 중인 작업을 끝내 동시 실행 자리를 비웁니다');
 html+=o.hidden?a('unhide','↺ 목록에 다시 표시','숨긴 항목을 목록에 다시 표시합니다'):a('hide','🗑 목록에서 삭제','목록에서만 숨깁니다 (파일·기록은 유지)');
 return html+'</span>';
}
function fmtBytes(n){return n==null?'':n<1048576?(n/1024).toFixed(1)+' KB':n<1073741824?(n/1048576).toFixed(1)+' MB':(n/1073741824).toFixed(2)+' GB'}
async function pollUpload(episode,onChange){
 for(;;){
  await new Promise(r=>setTimeout(r,3000));
  let st;try{st=await api('/api/episodes/upload-status?'+new URLSearchParams({episode}))}catch(e){continue}
  if(st.state==='running')continue;
  uploadState.delete(episode);onChange&&onChange();
  if(st.state==='success')notice(episode+' NAS 업로드 완료 · '+fmtBytes(st.bytes)+' · 검증 통과'+(st.destination?' → '+st.destination:''));
  else notice(episode+' NAS 업로드 실패: '+(st.error||st.state),true);
  return;
 }
}
// container 안의 [data-ea] 버튼에 동작을 연결합니다. onChange: 목록 다시 그리기/불러오기
function bindEpisodeActions(container,onChange){
 container.querySelectorAll('[data-ea]').forEach(b=>b.onclick=async e=>{
  e.preventDefault();e.stopPropagation();
  const d=b.dataset,ep=d.episode,id=d.ea+'|'+ep+'|'+d.key;if(epActionBusy.has(id))return;
  try{
   if(d.ea==='upload'){
    if(!confirm(ep+'\n\n지금 GPU PC 작업 폴더에 있는 이 에피소드 파일을 NAS에 올립니다.\n(NAS의 같은 에피소드 사본은 지금 상태로 맞춰집니다.)'))return;
    epActionBusy.add(id);b.disabled=true;
    const st=await api('/api/episodes/upload',{method:'POST',body:JSON.stringify({episode:ep})});
    if(st.state==='running'){uploadState.set(ep,'running');notice(ep+' NAS 업로드를 시작했습니다. 끝나면 알려 드립니다.');onChange&&onChange();pollUpload(ep,onChange)}
    else notice(ep+' NAS 업로드: '+(st.error||st.state),st.state==='failed');
   }else if(d.ea==='stop'){
    if(!confirm(ep+'\n\n대기·실행 중인 작업을 강제 종료합니다.\n동시 실행 자리가 비워지고, 진행 중이던 단계는 "실패 단계 다시 실행"으로 나중에 이어갈 수 있습니다.\n\n종료할까요?'))return;
    epActionBusy.add(id);b.disabled=true;b.textContent='종료 중…';
    const r=await api('/api/episodes/stop',{method:'POST',body:JSON.stringify({episode:ep,dag_id:d.dag,run_id:d.run})});
    notice(ep+' 강제 종료 완료'+(r.stages.length?' · 종료한 단계: '+r.stages.map(s=>(typeof taskName!=='undefined'&&taskName[s])||s).join(', '):''));
    if(typeof jobsSnapshot!=='undefined')jobsSnapshot='';onChange&&onChange();
   }else if(d.ea==='hide'||d.ea==='unhide'){
    const hide=d.ea==='hide';
    if(hide&&!confirm(ep+'\n\n목록에서 삭제합니다. 결과 파일과 실행 기록은 그대로 남고, "숨긴 항목 보기"에서 다시 표시할 수 있습니다.'))return;
    epActionBusy.add(id);b.disabled=true;
    await api('/api/hidden',{method:'POST',body:JSON.stringify({scope:d.scope,key:d.key,episode:ep,hidden:hide})});
    hide?hiddenKeys[d.scope].add(d.key):hiddenKeys[d.scope].delete(d.key);
    notice(hide?ep+'을(를) 목록에서 삭제했습니다.':ep+'을(를) 목록에 다시 표시했습니다.');onChange&&onChange();
   }
  }catch(err){notice(err.message,true);b.disabled=false}
  finally{epActionBusy.delete(id)}
 });
 container.querySelectorAll('[data-hidden-toggle]').forEach(c=>c.onchange=()=>{showHidden[c.dataset.hiddenToggle]=c.checked;onChange&&onChange(true)});
}
