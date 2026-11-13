from qc_sync_ui import PANEL, CSS as SYNC_CSS, JS as SYNC_JS
"""Portable QC review page. Decisions are persisted by serve_qc.py."""
import html,json,os,subprocess,shutil,threading
VIDEO_LOCK=threading.Lock()
def prepare_media(case,out):
 if not shutil.which("ffmpeg"):return
 with VIDEO_LOCK:
  for cam in range(1,5):
   source=case/f"cam_{cam}.mp4";dest=out/f"cam_{cam}_review.webm"
   if dest.exists() or not source.exists():continue
   temp=out/f"cam_{cam}_review.tmp.webm"
   try:
    subprocess.run(["ffmpeg","-v","error","-y","-i",str(source),"-an","-c:v","libvpx-vp9","-deadline","realtime","-cpu-used","8","-crf","32","-b:v","0",str(temp)],check=True,timeout=120,capture_output=True)
    temp.replace(dest)
   except (subprocess.SubprocessError,OSError):
    temp.unlink(missing_ok=True)

CHECKS={
 'rgb':'RGB 전체 구간 영상을 확인했고, 손·물체의 잘림 / 가림 / 흔들림 / 노출 문제를 확인했습니다.',
 'depth':'4개 카메라의 Depth를 RGB와 비교하여 손·물체 영역의 깊이 누락을 확인했습니다.',
 'timing':'각 카메라에 같은 작업 구간이 있는지, 중간 끊김과 동작의 시간 차이를 확인했습니다.',
 'geometry':'카메라 정합·좌표계 자동 결과와 제한 사항을 확인했습니다. 필요한 후속 정렬은 메모했습니다.',
 'warnings':'자동 검사 경고와 미검사 구간을 확인하고 진행 근거 또는 문제점을 메모했습니다.'}

HELP={
 'rgb':('각 RGB 영상을 처음부터 끝까지 재생하고 손끝·천 가장자리와 화면 경계를 보세요. 화면 밖으로 나가면 잘림, 화면 안에서 다른 대상 뒤에 숨으면 가림입니다.', '필요한 부위와 동작을 다른 시점에서도 확인할 수 있고 남은 문제가 없으면 승인 검토. 한 카메라에서만 잘렸다는 이유만으로 불합격은 아닙니다.', '중요한 부위가 여러 시점에서 계속 안 보이거나 심한 흔들림·노출로 판단할 수 없으면 보류. 다른 카메라에 보인다는 사실만으로 3D 복원 가능을 보장하지 않습니다.'),
 'depth':('RGB의 손·천 위치와 Depth의 빨간 영역을 비교하세요. 빨강은 무효 깊이값입니다. 배경 테두리와 복원 대상 내부의 누락을 구분하세요.', '대상 내부 깊이가 확인되고, 누락 부분의 다른 시점 정보와 사용 가능 근거를 설명할 수 있으면 승인 검토.', '손·천 내부가 넓게 비거나 다른 시점도 누락되면 보류. 공통 시간축을 움직여 문제 구간의 Depth를 확인하세요. 공통 구간 밖이나 원인을 판단할 수 없는 누락은 추가 확인 전까지 보류하세요.'),
 'timing':('4캠에서 천을 잡거나 놓는 같은 동작을 찾아 앞뒤 순서, 영상 정지·누락을 확인하세요. 각 영상의 00:02가 동일한 촬영 시각이라는 뜻은 아닙니다.', '같은 작업 구간이 존재하고 자동 시간 차이와 육안 확인에서 설명되지 않는 어긋남이 없으면 승인 검토.', '빠른 동작이 서로 대응하지 않거나 영상이 끊기면 보류. 동시 화면의 프레임 시간 차이도 확인하세요. host_time_s 정렬은 실제 노출 시각의 하드웨어 동기화를 보장하지 않습니다.'),
 'geometry':('자동 결과의 카메라 간 잔차·겹침률과 월드 좌표계 설명을 읽으세요. 서로 다른 카메라의 2D 화면에서 픽셀 위치가 같은지를 비교하는 항목은 아닙니다.', 'FAIL 없이 테이블 정합을 확인했고 reorient 필요 여부를 메모했다면 승인 검토. 좌표 재정렬 전이라는 이유만으로 보류할 필요는 없습니다.', '정합 FAIL, 비정상 배치, 원인을 설명할 수 없는 WARN은 보류. 의심되면 정합된 점군을 추가 확인하세요. 현재 미리보기로 전체 3D 정합을 확인할 수는 없습니다.'),
 'warnings':('자동 PASS/WARN/FAIL, 누락된 검사와 아직 확인하지 못한 구간을 확인하세요. 메모에는 카메라·시간·대상·문제·다른 시점 확인 결과를 적으세요.', '모든 항목을 확인하고 미해결 문제가 없으며 WARN의 진행 근거까지 기록한 경우 승인하세요.', '미확인 항목, 판단 불가, 해결되지 않은 문제가 하나라도 남으면 보류하세요. 보류는 재촬영 확정이 아니라 추가 확인·보완 대기입니다.')}
GUIDE = """<section id="criteria"><h2>작업자 판단 안내</h2><p>체크는 <b>직접 확인했다</b>는 뜻입니다. 문제가 없다는 표시가 아니며, 확인한 항목이 모두 체크돼도 미해결 문제가 있으면 보류하세요.</p><div class="terms">
<div><h3>잘림 · Truncation</h3><p>손·물체 일부가 <b>화면 경계 밖</b>으로 나가 보이지 않는 상태.</p><p class="muted">예: 손끝이나 천 끝이 영상 바깥으로 나감.</p></div>
<div><h3>가림 · Occlusion</h3><p>대상은 화면 안에 있지만 <b>다른 대상 뒤</b>에 숨은 상태.</p><p class="muted">예: 손바닥 뒤에 천이 가려짐, 접힌 천 뒤에 손끝이 숨음.</p></div>
<div><h3>깊이 누락 · Missing depth</h3><p>RGB에는 보이지만 그 위치에 <b>유효한 깊이값이 없는</b> 상태. Depth에서 빨간색으로 표시됩니다.</p><p class="muted">예: 손·천 표면 일부가 빨갛게 비어 있음. 세 문제는 함께 생길 수도 있습니다.</p></div></div>
<div class="terms decisions"><div><h3>승인 검토</h3><p>사용할 동작 구간의 필요한 부위를 확인했고 미해결 문제가 없을 때. 일부 시점의 잘림·가림은 다른 시점까지 확인한 근거를 메모하세요. 단순히 다른 카메라에 보인다는 이유만으로 승인하지 않습니다.</p></div><div><h3>보류</h3><p>중요 부위의 정보가 부족하거나, 경고 원인이 불명확하거나, 추가 확인이 필요할 때. 필요한 조치를 메모하세요: 해당 구간 추가 확인 / 사용 구간 조정 검토 / 캘리브레이션 확인 / 재촬영 검토.</p></div></div>
<p class="notice">이 안내는 작업자 판단 가이드입니다. 허용 잘림 비율·지속 시간·최소 가시 카메라 수의 확정된 수치 기준은 아직 없습니다. 판단이 애매하면 보류하고 담당자와 확인하세요. 자동 FAIL/ERROR는 승인할 수 없습니다.</p>
<p><b>메모 예시 (복사 후 실제 내용으로 수정):</b><br>Camera 2 · 00:02–00:03 · 손끝 화면 밖으로 잘림. Camera 1/3의 대응 동작에서 손끝 확인. 사용 구간 및 깊이 상태: … / 승인 근거 또는 추가 확인할 사항: …</p></section>"""

def page(ep,case,out,res):
 e=html.escape
 rows=''.join('<tr><td>'+e(c.get('label',c['name']))+'</td><td><b>'+e(c['status'])+'</b></td><td>'+e(c.get('detail',''))+'</td></tr>' for c in res.get('checks',[]))
 cams=[]
 for cam in range(1,5):
  previews={p['kind']:p for p in res.get('previews',[]) if p['camera']==cam}
  images=''.join('<figure><a target="_blank" href="'+e(p['file'],quote=True)+'"><img src="'+e(p['file'],quote=True)+'" alt="Camera '+str(cam)+' '+e(k)+'"></a><figcaption>'+e(k)+' '+e(p.get('source',''))+'</figcaption></figure>' for k,p in previews.items())
  preview=out/f'cam_{cam}_review.webm'
  video=e(preview.name if preview.exists() else os.path.relpath(case/f'cam_{cam}.mp4',out),quote=True)
  cams.append(f'<article class="camera" id="cam{cam}"><h3>Camera {cam}</h3><video controls preload="metadata" src="{video}"></video>{images}</article>')
 boxes=''.join(f'<div class="review-item"><label class="check"><input type="checkbox" name="{k}"><span>{e(v)}</span></label><div class="item-help"><p><b>무엇을 보나요</b><br>{e(HELP[k][0])}</p><p><b>승인 검토</b><br>{e(HELP[k][1])}</p><p><b>보류할 때</b><br>{e(HELP[k][2])}</p></div></div>' for k,v in CHECKS.items())
 return '''<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>02 · QC 작업자 검수</title>
<style>*{box-sizing:border-box}body{margin:0;background:#eef2f6;color:#17283c;font:15px/1.65 system-ui}main{max-width:1500px;margin:auto;padding:28px}h1{font-size:28px;margin:8px 0}h2{font-size:21px}h3{margin:8px 0}section,aside{background:white;border:1px solid #dae2eb;border-radius:16px;padding:22px;margin:18px 0}.badge{display:inline-block;background:#e8eef6;border-radius:20px;padding:5px 14px}table{border-collapse:collapse;width:100%;font-size:14px}td,th{padding:12px;text-align:left;border-bottom:1px solid #dce3eb;vertical-align:top}.workspace{display:grid;grid-template-columns:minmax(0,1fr) 390px;gap:20px;align-items:start}.cameras{display:grid;grid-template-columns:1fr 1fr;gap:16px}.camera{min-width:0;background:#f5f7fa;padding:12px;border-radius:12px}video,img{display:block;width:100%;border-radius:6px}figure{margin:14px 0}figcaption{font-size:12px;color:#4b5f75}aside{position:static}.terms{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}.terms>div{background:#f3f6fa;border-radius:10px;padding:16px}.decisions{grid-template-columns:1fr 1fr;margin-top:14px}.item-help{font-size:13px;color:#42576d;padding:0 10px 12px}.item-help p{margin:10px 0}.review-item{border-bottom:1px solid #dae2eb;margin-bottom:12px}nav{position:sticky;top:0;z-index:5;background:#17283c;padding:10px 16px;border-radius:10px;display:flex;gap:14px;flex-wrap:wrap}nav a{color:white;text-decoration:none}section,aside{scroll-margin-top:65px}.check{display:flex;gap:10px;padding:12px 0;border-bottom:1px solid #edf0f4;cursor:pointer}input[type=checkbox]{width:20px;height:20px;flex-shrink:0}textarea,input[type=text]{width:100%;padding:10px;border:1px solid #a8b6c8;border-radius:7px;font:inherit}textarea{min-height:110px}button{padding:12px 16px;border:0;border-radius:8px;font:inherit;cursor:pointer;background:#dce6f2}button.primary{background:#17614e;color:white}button:disabled{opacity:.45;cursor:not-allowed}.actions{display:flex;gap:8px;margin-top:14px}a{color:#185cad}.muted{color:#53667c}.notice{background:#fff3d9;padding:12px;border-radius:8px}#message{white-space:pre-wrap}summary{cursor:pointer}pre{white-space:pre-wrap;overflow-wrap:anywhere}@media(max-width:1050px){.workspace{grid-template-columns:1fr}aside{position:static}}@media(max-width:650px){.cameras,.terms,.decisions{grid-template-columns:1fr}main{padding:12px}}'''+SYNC_CSS+'''</style><main>
<a href="/overview.html">← 전체 작업 현황</a><h1>02 · QC 작업자 검수 — '''+e(ep)+'''</h1><p class="muted">자동 검사 → RGB · Depth 확인 → 체크리스트 → 승인 / 보류 저장</p>
<nav><a href="#automatic">자동 결과</a><a href="#criteria">판단 안내</a><a href="#media">RGB · Depth</a><a href="#review">검수 · 저장</a></nav><section id="automatic"><h2>1. 자동 검사 결과 <span class="badge">'''+e(res.get('overall','ERROR'))+'''</span></h2><p>스크립트가 계산한 결과입니다. 자동 PASS는 작업자 승인이 아닙니다.</p><p>'''+e(res.get('error',''))+'''</p><table><thead><tr><th>검사 항목</th><th>자동 판정</th><th>측정 결과</th></tr></thead><tbody>'''+rows+'''</tbody></table><details><summary>자동 검사의 범위와 한계</summary><p>RGB는 전체 구간에서 8장, 깊이는 앞쪽 1/3에서 8장씩 검사합니다. 깊이 유효율은 중앙 가로·세로 60% 영역 평균이며 손·물체별 품질을 보장하지 않습니다. 동기화 수치는 호스트 타임스탬프 기준이고, 테이블 정합 PASS는 좌표 재정렬 완료를 뜻하지 않습니다.</p></details><a href="qc.json">전체 수치 JSON</a> · <a href="output.log">검사 로그</a></section>
'''+GUIDE+PANEL+'''<div class="workspace"><section id="original"><details><summary>공통 구간 밖 확인 · 카메라별 원본 전체 영상과 대표 이미지</summary><h2>카메라별 전체 구간</h2><p class="notice">Depth 빨강 = 무효 깊이값, 회색 = 0–2m. 이미지를 누르면 확대됩니다. RGB는 첫·중간·마지막, Depth는 중간 프레임입니다. 카메라별 독립 재생이며 동기화된 비교가 아닙니다. 동영상은 원본 전체 구간의 브라우저용 변환본입니다. 깊이 미리보기는 한 장이며 전체 Depth 검사는 아닙니다.</p><div class="cameras">'''+''.join(cams)+'''</div></details></section>
<aside id="review"><h2>3. 작업자 검수</h2><p id="saved" class="badge">저장 상태 확인 중</p><p class="muted">각 항목의 안내를 읽고 직접 확인한 경우에만 체크하세요. 문제가 남으면 모두 체크했더라도 보류하세요. <a href="#criteria">용어와 승인·보류 기준 보기</a></p>'''+boxes+'''<p><label>작업자 이름<input id="operator" type="text" maxlength="100" placeholder="이름 또는 작업자 ID"></label></p><label>검수 메모 (필수)<textarea id="note" maxlength="5000" placeholder="예: Camera 1 · 00:02 손 경계 깊이 누락. 다른 카메라 확인 결과 / 진행 근거 또는 보류 사유"></textarea></label><p id="count">0 / 5 항목 확인</p><div class="actions"><button id="accept" class="primary" disabled>승인 저장</button><button id="hold" disabled>보류 저장</button></div><p class="muted">승인: 모든 항목 확인 + 메모 필요. FAIL/ERROR는 승인할 수 없습니다. 보류: 체크가 미완료여도 사유를 남겨 저장할 수 있습니다. 다음 단계는 자동 실행되지 않습니다.</p><p id="message" role="status" aria-live="polite"></p><details><summary>저장된 검수 이력</summary><pre id="history">아직 없음</pre></details></aside></div></main>
<script>
const report=decodeURIComponent(location.pathname).replace(/\\/report\\.html$/, ''), boxes=[...document.querySelectorAll('input[type=checkbox]')];
let token='', revision='', busy=false;
const $=id=>document.getElementById(id);
function ready(){const n=boxes.filter(x=>x.checked).length;$('count').textContent=`${n} / ${boxes.length} 항목 확인`;
const valid=token&&!busy&&$('operator').value.trim()&&$('note').value.trim();$('accept').disabled=!(valid&&n===boxes.length&&'''+json.dumps(res.get('overall') in ['PASS','WARN'] and bool(res.get('ok')))+''');$('hold').disabled=!valid;}
for(const el of [...boxes,$('operator'),$('note')])el.addEventListener('input',()=>{$('saved').textContent='변경사항 미저장';ready();});
function show(r){revision=r.revision;const v=r.review;$('saved').textContent=v?(v.decision==='accept'?'승인 저장됨':'보류 저장됨')+' · '+v.time:'검수 대기 · 아직 저장되지 않음';
 if(v){$('operator').value=v.operator||'';$('note').value=v.note||'';for(const b of boxes)b.checked=!!(v.checklist||{})[b.name];}
 $('history').textContent=v?JSON.stringify(v.history,null,2):'아직 없음';ready();}
async function boot(){try{const r=await fetch('/api/review?report='+encodeURIComponent(report),{cache:'no-store'});const d=await r.json();if(!r.ok)throw Error(d.error);token=d.token;show(d);}catch(e){$('saved').textContent='저장 서버 연결 필요';$('message').textContent='python3 serve_qc.py 로 서버를 시작하고 해당 HTTP 주소에서 여세요. '+e.message;}}
async function save(decision){busy=true;ready();$('message').textContent='검사 결과와 RAW 무결성을 확인하고 저장 중…';try{const r=await fetch('/api/review',{method:'POST',headers:{'Content-Type':'application/json','X-QC-Token':token},body:JSON.stringify({report,decision,operator:$('operator').value.trim(),note:$('note').value.trim(),checklist:Object.fromEntries(boxes.map(b=>[b.name,b.checked])),revision})});const d=await r.json();if(!r.ok)throw Error(d.error);show(d);$('message').textContent='프로젝트의 operator_review.json에 저장했습니다. 전체 작업 현황에도 반영됩니다.';}catch(e){$('message').textContent='저장 실패: '+e.message;}finally{busy=false;ready();}}
$('accept').onclick=()=>save('accept');$('hold').onclick=()=>save('reject');boot();
'''+SYNC_JS+'''</script></html>'''
