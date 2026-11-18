async function applySession(user){
 currentUser=user;const admin=user.role==='admin';
 $('identity').textContent=user.admin_login?'관리자 · '+user.name+' 화면':user.name+' · '+(admin?'관리자':'작업자');
 if(admin)adminPcSwitch(user);$('adminNav').classList.toggle('hidden',!admin);
 $('chooseOutputFolder').classList.toggle('hidden',!admin);$('outputRoot').disabled=!admin;
 $('myOperator').value=user.name;$('myOperator').readOnly=!admin;
 $('myJobsNav').classList.toggle('hidden',admin);
 document.querySelector('nav [data-page="outputs"]').textContent=admin?'📁　전체 결과물':'📁　내 작업 결과물';
 document.querySelector('nav [data-page="review"]').textContent=admin?'☑　전체 검수':'☑　내 검수';
 $('loginDialog').close();
 // 새로고침해도 보던 화면에 그대로 남습니다: #jobs, #outputs, #review, #review/<에피소드>/<mask|tracking>
 let saved='';try{saved=sessionStorage.getItem('kaistPage')||''}catch(e){}
 const [want,ep,stage]=decodeURIComponent(location.hash.slice(1)||saved).split('/');
 const pages=['run','jobs','review','outputs'].concat(admin?['intake','mywork','fleet','dashboard']:[]);
 const start=pages.includes(want)?want:(admin?'dashboard':'run');
 show(start==='review'&&ep?'run':start);await init();
 if(start==='review'&&ep&&/^ep_[A-Za-z0-9_]+$/.test(ep))openReview(ep,stage==='tracking'?'tracking':'mask');
}
// 관리자: 왼쪽 위 "관리자 · PCn 화면"에서 다른 PC 화면으로 바로 전환(NAS 1회용 토큰으로 관리자 로그인 유지).
async function adminPcSwitch(user){
 const box=$('identity');
 const here=String(user.name||'').toLowerCase();
 let list={me:here,pcs:['pc1','pc2','pc3','pc4','pc5','pc6'].map(pc=>({pc,online:pc===here}))},known=false;
 try{list=await api('/api/fleet/pcs');known=true}catch(e){}
 box.innerHTML='<span class="idRole">관리자</span><label class="idSwitch" title="다른 PC 화면으로 전환합니다. 관리자로 바로 들어갑니다."><select id="pcSwitch" aria-label="관리할 PC 화면">'+
  list.pcs.map(p=>'<option value="'+p.pc+'"'+(p.pc===list.me?' selected':'')+(!p.online&&known?' disabled':'')+'>'+p.pc.toUpperCase()+' 화면'+(p.pc===list.me?'':!p.online&&known?' · 연결 끊김':'')+'</option>').join('')+'</select></label>';
 $('pcSwitch').onchange=e=>{const pc=e.target.value;if(pc===list.me)return;
  if(typeof hcEditLeaveOk==='function'&&page==='review'&&!hcEditLeaveOk(()=>{const s=$('pcSwitch');s.value=pc;s.onchange({target:s})})){e.target.value=list.me;return}
  e.target.disabled=true;box.querySelector('.idRole').textContent=pc.toUpperCase()+' 화면으로 이동 중…';
  location.href='/api/fleet/open?pc='+encodeURIComponent(pc)};
}
async function restoreSession(){try{const user=await api('/api/session');if(user.name){await applySession(user);return}}catch(e){$('loginError').textContent=e.message}$('loginDialog').showModal()}
$('loginDialog').addEventListener('cancel',e=>e.preventDefault());
function loginRoleUi(){const admin=$('loginRole').value==='admin';$('adminPasswordBox').classList.toggle('hidden',!admin);$('loginNameBox').classList.toggle('hidden',admin);$('loginName').required=!admin;if(admin)setTimeout(()=>$('adminPassword').focus(),0)}
$('loginRole').onchange=loginRoleUi;loginRoleUi();
$('loginForm').onsubmit=async e=>{e.preventDefault();$('loginError').textContent='';try{const user=await api('/api/login',{method:'POST',body:JSON.stringify({name:$('loginRole').value==='admin'?'':$('loginName').value,role:$('loginRole').value,password:$('adminPassword').value})});if(user.redirect){$('loginError').textContent=user.name+' 화면으로 이동합니다…';location.href=user.redirect;return}$('adminPassword').value='';await applySession(user)}catch(err){$('loginError').textContent=err.message}};
$('logout').onclick=async()=>{try{await api('/api/logout',{method:'POST'});location.reload()}catch(e){notice(e.message,true)}};
