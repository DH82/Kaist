async function applySession(user){
 currentUser=user;const admin=user.role==='admin';
 $('identity').textContent=user.name+' · '+(admin?'관리자':'작업자');$('adminNav').classList.toggle('hidden',!admin);
 $('chooseOutputFolder').classList.toggle('hidden',!admin);$('outputRoot').disabled=!admin;
 $('myOperator').value=user.name;$('myOperator').readOnly=!admin;
 $('myJobsNav').classList.toggle('hidden',admin);
 document.querySelector('nav [data-page="outputs"]').textContent=admin?'📁　전체 결과물':'📁　내 작업 결과물';
 document.querySelector('nav [data-page="review"]').textContent=admin?'☑　전체 검수':'☑　내 검수';
 $('loginDialog').close();show('run');await init();
}
async function restoreSession(){try{const user=await api('/api/session');if(user.name){await applySession(user);return}}catch(e){$('loginError').textContent=e.message}$('loginDialog').showModal()}
$('loginDialog').addEventListener('cancel',e=>e.preventDefault());
$('loginRole').onchange=()=>{$('adminPasswordBox').classList.toggle('hidden',$('loginRole').value!=='admin')};
$('loginForm').onsubmit=async e=>{e.preventDefault();$('loginError').textContent='';try{const user=await api('/api/login',{method:'POST',body:JSON.stringify({name:$('loginName').value,role:$('loginRole').value,password:$('adminPassword').value})});$('adminPassword').value='';await applySession(user)}catch(err){$('loginError').textContent=err.message}};
$('logout').onclick=async()=>{try{await api('/api/logout',{method:'POST'});location.reload()}catch(e){notice(e.message,true)}};
