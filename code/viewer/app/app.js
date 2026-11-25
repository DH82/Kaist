import * as THREE from 'three';
import { OrbitControls } from './vendor/OrbitControls.js';

const $ = id => document.getElementById(id);
let data, frameIndex = 0, playing = false, lastTime = 0, elapsed = 0, viewRadius = 0.3;
let scene, camera, renderer, controls, grid, objectPoints;
const handMeshes = new Map();
const rgbImages = new Map();
const center = new THREE.Vector3();

function toWorld(values) {
  const positions = new Float32Array(values.length);
  for (let i = 0; i < values.length; i += 3) {
    positions[i] = values[i];
    positions[i + 1] = -values[i + 2];
    positions[i + 2] = values[i + 1];
  }
  return positions;
}

function initScene() {
  scene = new THREE.Scene();
  scene.background = new THREE.Color('#111925');
  camera = new THREE.PerspectiveCamera(40, 1, 0.001, 100);
  renderer = new THREE.WebGLRenderer({antialias: true, alpha: false});
  renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.2;
  $('scene').appendChild(renderer.domElement);
  controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.dampingFactor = 0.08;
  controls.minDistance = 0.025;
  controls.maxDistance = 5;
  scene.add(new THREE.HemisphereLight(0xd8eaff, 0x3c4052, 2.4));
  const key = new THREE.DirectionalLight(0xffeedb, 3.5);
  key.position.set(1, 2, 2); scene.add(key);
  const rim = new THREE.DirectionalLight(0x82bfff, 2);
  rim.position.set(-1, 0.8, -1); scene.add(rim);
  for (const [side, indices] of Object.entries(data.faces)) {
    const geometry = new THREE.BufferGeometry();
    geometry.setIndex(indices.flat());
    geometry.setAttribute('position', new THREE.BufferAttribute(new Float32Array(778 * 3), 3));
    const material = new THREE.MeshStandardMaterial({color: 0xe7c7a3, roughness: 0.45, metalness: 0.08, side: THREE.DoubleSide});
    const mesh = new THREE.Mesh(geometry, material);
    mesh.frustumCulled = false;
    scene.add(mesh); handMeshes.set(side, mesh);
  }
  const geometry = new THREE.BufferGeometry();
  const sprite = document.createElement('canvas');
  sprite.width = sprite.height = 32;
  const spriteContext = sprite.getContext('2d');
  spriteContext.fillStyle = '#ffffff';
  spriteContext.beginPath(); spriteContext.arc(16, 16, 15, 0, Math.PI * 2); spriteContext.fill();
  const material = new THREE.PointsMaterial({color: 0x4cb6ed, size: 0.003, sizeAttenuation: true, map: new THREE.CanvasTexture(sprite), alphaTest: 0.5});
  objectPoints = new THREE.Points(geometry, material);
  objectPoints.frustumCulled = false;
  scene.add(objectPoints);
  const axes = [[], [], []];
  for (const frame of data.frames) {
    const collections = [{values: frame.object, validity: frame.objectValid}, ...(frame.hands || []).filter(h => h.valid).map(h => ({values: h.vertices}))];
    for (const {values, validity} of collections) {
      if (!values?.length) continue;
      for (let i = 0; i < values.length; i += 3) {
        if (validity && !validity[i / 3]) continue;
        if (Number.isFinite(values[i]) && Number.isFinite(values[i + 1]) && Number.isFinite(values[i + 2])) {
          axes[0].push(values[i]); axes[1].push(-values[i + 2]); axes[2].push(values[i + 1]);
        }
      }
    }
  }
  for (const axis of axes) axis.sort((a, b) => a - b);
  const quantile = (axis, ratio) => axis[Math.floor((axis.length - 1) * ratio)];
  // Framing ignores isolated outliers; all valid geometry is still rendered.
  const bounds = new THREE.Box3(new THREE.Vector3(...axes.map(a => quantile(a, 0.01))), new THREE.Vector3(...axes.map(a => quantile(a, 0.99))));
  bounds.getCenter(center);
  viewRadius = Math.max(bounds.getSize(new THREE.Vector3()).length() / 2, 0.08);
  grid = new THREE.GridHelper(Math.max(viewRadius * 3, 0.5), 20, 0x456078, 0x243a4c);
  grid.position.set(center.x, Math.min(bounds.min.y - 0.003, 0), center.z);
  scene.add(grid);
  resetView();
  new ResizeObserver(() => {
    const width = $('scene').clientWidth, height = $('scene').clientHeight;
    renderer.setSize(width, height); camera.aspect = width / height; camera.updateProjectionMatrix();
  }).observe($('scene'));
}

function resetView(top = false) {
  controls.target.copy(center);
  const distance = viewRadius * (top ? 3.1 : 2.05);
  camera.position.copy(center).add(top ? new THREE.Vector3(0, distance, 0.0001) : new THREE.Vector3(distance * 0.75, distance * 0.7, distance));
  controls.update();
}

function showFrame(index) {
  frameIndex = Math.max(0, Math.min(data.frames.length - 1, index));
  const frame = data.frames[frameIndex];
  for (const mesh of handMeshes.values()) mesh.visible = false;
  for (const hand of frame.hands || []) {
    const mesh = handMeshes.get(hand.side);
    if (!mesh || !hand.valid || !hand.vertices) continue;
    const positions = toWorld(hand.vertices);
    mesh.geometry.setAttribute('position', new THREE.BufferAttribute(positions, 3));
    mesh.geometry.computeVertexNormals();
    mesh.visible = $('show-hand').checked;
  }
  const filtered = [], colors = [];
  for (let i = 0; i < frame.object.length; i += 3) {
    if (frame.objectValid && !frame.objectValid[i / 3]) continue;
    if (![frame.object[i], frame.object[i + 1], frame.object[i + 2]].every(Number.isFinite)) continue;
    filtered.push(frame.object[i], -frame.object[i + 2], frame.object[i + 1]);
    if (frame.objectColors) colors.push(...frame.objectColors.slice(i, i + 3));
  }
  objectPoints.geometry.setAttribute('position', new THREE.Float32BufferAttribute(filtered, 3));
  if (colors.length) {
    if (colors.some(v => v > 1)) for (let i = 0; i < colors.length; i++) colors[i] /= 255;
    objectPoints.geometry.setAttribute('color', new THREE.Float32BufferAttribute(colors, 3));
  }
  objectPoints.material.vertexColors = $('original-color').checked && colors.length > 0;
  objectPoints.material.color.set(objectPoints.material.vertexColors ? 0xffffff : 0x4cb6ed);
  objectPoints.material.needsUpdate = true;
  objectPoints.visible = $('show-object').checked;
  $('timeline').value = frameIndex;
  $('frame').textContent = `FRAME ${String(frame.id ?? frameIndex).padStart(3, '0')} / ${data.frames.length - 1}`;
  $('time').textContent = `${(frameIndex / data.fps).toFixed(2)} s`;
  $('rgb-frame').textContent = `${data.rgb.cameraNames[+$('camera').value]} · frame ${frame.id ?? frameIndex}`;
  const rgbImage = rgbImages.get(data.rgb.paths[+$('camera').value][frameIndex]);
  const canvas = $('rgb');
  if (canvas.width !== rgbImage.naturalWidth || canvas.height !== rgbImage.naturalHeight) {
    canvas.width = rgbImage.naturalWidth; canvas.height = rgbImage.naturalHeight;
  }
  canvas.getContext('2d').drawImage(rgbImage, 0, 0);
  const validHands = (frame.hands || []).filter(h => h.valid).length;
  $('status').textContent = `${validHands}개 손 · 물체 ${filtered.length / 3}개 표시점 · 단위 m`;
}

function setPlaying(value) {
  playing = value; elapsed = 0;
  $('play').textContent = playing ? '일시정지' : '재생';
  $('play').setAttribute('aria-label', playing ? '일시정지' : '재생');
}

function animate(time) {
  const dt = Math.min((time - lastTime) / 1000, 0.2); lastTime = time;
  if (playing) {
    elapsed += dt * Number($('speed').value);
    const steps = Math.floor(elapsed * data.fps);
    if (steps) {elapsed -= steps / data.fps; showFrame((frameIndex + steps) % data.frames.length);}
  }
  controls.update(); renderer.render(scene, camera); requestAnimationFrame(animate);
}

async function main() {
  const response = await fetch('data.json', {cache: 'no-store'});
  if (!response.ok) throw new Error(`data.json 로드 실패 (${response.status})`);
  data = await response.json();
  if (!data.frames?.length || !data.faces || !data.rgb?.paths) throw new Error('데이터 구성을 확인해 주세요.');
  $('episode').textContent = `${data.episode} · ${data.frames.length} frames · ${data.fps} fps`;
  $('source').textContent = data.sourceNote;
  $('duration').textContent = `${(data.frames.length / data.fps).toFixed(2)}초 · ${data.frames.length}프레임`;
  $('timeline').max = data.frames.length - 1;
  for (const [index, name] of data.rgb.cameraNames.entries()) $('camera').add(new Option(name, index));
  const paths = data.rgb.paths.flat();
  let loaded = 0;
  await Promise.all(paths.map(path => new Promise((resolve, reject) => {
    const image = new Image();
    image.onload = async () => {
      try {await image.decode();} catch (_) { /* The loaded image can still be displayed. */ }
      rgbImages.set(path, image);
      loaded++;
      $('loading').textContent = `RGB 프레임 준비 중 ${loaded} / ${paths.length}`;
      resolve();
    };
    image.onerror = () => reject(new Error(`RGB 프레임 로드 실패: ${path}`));
    image.src = path;
  })));
  initScene(); showFrame(0); $('loading').remove();
  $('play').onclick = () => setPlaying(!playing);
  $('previous').onclick = () => {setPlaying(false); showFrame(frameIndex - 1);};
  $('next').onclick = () => {setPlaying(false); showFrame(frameIndex + 1);};
  $('timeline').oninput = () => {setPlaying(false); showFrame(+$('timeline').value);};
  $('camera').onchange = () => showFrame(frameIndex);
  $('show-hand').onchange = $('show-object').onchange = $('original-color').onchange = () => showFrame(frameIndex);
  $('wireframe').onchange = () => {for (const mesh of handMeshes.values()) mesh.material.wireframe = $('wireframe').checked;};
  $('point-size').oninput = () => {objectPoints.material.size = +$('point-size').value / 1000;};
  $('grid').onchange = () => {grid.visible = $('grid').checked;};
  $('reset').onclick = () => resetView(); $('top').onclick = () => resetView(true);
  document.addEventListener('keydown', e => {
    if (['INPUT', 'SELECT'].includes(e.target.tagName)) return;
    if (e.code === 'Space') {e.preventDefault(); setPlaying(!playing);}
    if (e.code === 'ArrowRight') {setPlaying(false); showFrame(frameIndex + 1);}
    if (e.code === 'ArrowLeft') {setPlaying(false); showFrame(frameIndex - 1);}
  });
  requestAnimationFrame(animate);
}
main().catch(error => {
  $('alert').hidden = false;
  $('alert').textContent = `뷰어를 열 수 없습니다. ${error.message} · 실행 스크립트로 로컬 서버를 시작한 뒤 접속해 주세요.`;
  console.error(error);
});
