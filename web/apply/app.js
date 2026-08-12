// 화면 쪽. 무거운 일은 전부 워커가 한다. 이쪽은 파일을 받고 진행을 보여 주고,
// 끝난 파일을 사용자가 고른 위치로 흘려 준다.
//
// 업로드 코드는 없다. fetch 는 같은 출처의 채널 목록(index.json)과 번들을 받는
// 데에만 쓰고, 사용자의 ROM 은 워커로만 전달된다.

const state = {rom: null, keys: [], bundle: null, channel: null, running: false};
const $ = (id) => document.getElementById(id);
const logEl = $('log');

function log(line) {
  logEl.textContent += line + '\n';
  logEl.scrollTop = logEl.scrollHeight;
}

function human(n) {
  if (n >= 1e9) return (n / 1e9).toFixed(2) + ' GB';
  if (n >= 1e6) return (n / 1e6).toFixed(1) + ' MB';
  if (n >= 1e3) return (n / 1e3).toFixed(1) + ' KB';
  return n + ' B';
}

// ---- 전제 확인 -------------------------------------------------------------
async function checkEnvironment() {
  const problems = [];
  const notes = [];
  if (!self.crossOriginIsolated || typeof SharedArrayBuffer !== 'function') {
    problems.push('이 페이지가 격리 모드로 열리지 않았습니다 '
                  + '(COOP/COEP 헤더 필요). 서버 설정 문제입니다.');
  }
  if (!navigator.storage || !navigator.storage.getDirectory) {
    problems.push('브라우저에 OPFS 가 없습니다. 크롬·엣지 최신 버전을 쓰세요.');
  }
  let quota = 0;
  if (navigator.storage && navigator.storage.estimate) {
    const est = await navigator.storage.estimate();
    quota = est.quota || 0;
    notes.push(`쓸 수 있는 임시 저장 공간 약 ${human(quota)}`);
  }
  const box = $('requirements');
  box.innerHTML = '<h2>준비 상태</h2>';
  const ul = document.createElement('ul');
  for (const p of problems) {
    const li = document.createElement('li');
    li.className = 'bad';
    li.textContent = p;
    ul.append(li);
  }
  for (const n of notes) {
    const li = document.createElement('li');
    li.className = 'ok';
    li.textContent = n;
    ul.append(li);
  }
  const li = document.createElement('li');
  li.className = problems.length ? 'bad' : 'ok';
  li.textContent = problems.length ? '위 문제를 해결해야 진행할 수 없습니다.'
      : '브라우저 안에서 패치할 수 있는 상태입니다. 2GB ROM 하나에 임시 공간이 '
        + '7GB 가량 필요하고, 끝나면 자동으로 지웁니다.';
  ul.append(li);
  box.append(ul);
  return problems.length === 0;
}

// ---- 파일 받기 -------------------------------------------------------------
function wireDrop(zoneId, inputId, handler) {
  const zone = $(zoneId);
  const input = $(inputId);
  const stop = (e) => { e.preventDefault(); e.stopPropagation(); };
  zone.addEventListener('dragover', (e) => {
    stop(e);
    zone.classList.add('over');
  });
  zone.addEventListener('dragleave', (e) => {
    stop(e);
    zone.classList.remove('over');
  });
  zone.addEventListener('drop', (e) => {
    stop(e);
    zone.classList.remove('over');
    handler([...e.dataTransfer.files]);
  });
  input.addEventListener('change', () => handler([...input.files]));
}

function refreshPlan() {
  const ready = !!(state.rom && state.bundle) && !state.running;
  $('start').disabled = !ready;
  const bits = [];
  bits.push(state.rom ? `입력 ${state.rom.name} (${human(state.rom.size)})`
                      : '원본 ROM 이 필요합니다');
  bits.push(state.keys.length ? `키 ${state.keys.map((k) => k.name).join(', ')}`
                              : '키 없음 (복호화된 덤프만 가능)');
  bits.push(state.bundle ? `번들 ${state.bundle.name}` : '번들이 필요합니다');
  $('plan').textContent = bits.join(' · ');
}

wireDrop('drop-rom', 'file-rom', (files) => {
  if (!files.length) return;
  state.rom = files[0];
  $('picked-rom').textContent = `${state.rom.name} — ${human(state.rom.size)}`;
  refreshPlan();
});
wireDrop('drop-keys', 'file-keys', (files) => {
  state.keys = files;
  $('picked-keys').textContent = files.map((f) => f.name).join(', ');
  refreshPlan();
});
wireDrop('drop-bundle', 'file-bundle', (files) => {
  if (!files.length) return;
  state.bundle = files[0];
  $('picked-bundle').textContent = state.bundle.name;
  for (const el of document.querySelectorAll('input[name=bundle]')) {
    el.checked = false;
  }
  refreshPlan();
});

// ---- 채널 목록 -------------------------------------------------------------
async function loadChannel() {
  const box = $('bundle-list');
  try {
    const r = await fetch('../hpk/index.json', {cache: 'no-store'});
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const doc = await r.json();
    state.channel = doc;
    box.innerHTML = '';
    if (!doc.patches || !doc.patches.length) {
      box.textContent = '채널에 아직 번들이 없습니다.';
      return;
    }
    for (const p of doc.patches) {
      const id = 'b-' + p.id;
      const label = document.createElement('label');
      label.className = 'choice';
      label.innerHTML = `<input type="radio" name="bundle" id="${id}">
        <span><strong>${p.title}</strong> (${p.target}) ${p.version}
        <span class="muted">· ${human(p.size)} · 문자열 ${p.entries}개</span>
        <br><span class="muted mono">대상 ROM sha256 ${p.source_sha256 || '?'}
        </span></span>`;
      label.querySelector('input').addEventListener('change', async () => {
        $('picked-bundle').textContent = '';
        box.querySelectorAll('.choice').forEach((c) =>
            c.classList.remove('sel'));
        label.classList.add('sel');
        log(`번들 받는 중: ${p.file}`);
        const res = await fetch('../hpk/' + p.file, {cache: 'no-store'});
        const buf = await res.arrayBuffer();
        state.bundle = new File([buf], p.file.split('/').pop());
        state.bundleMeta = p;
        log(`번들 준비됨 (${human(state.bundle.size)})`);
        refreshPlan();
      });
      box.append(label);
    }
  } catch (e) {
    box.textContent = '채널을 읽지 못했습니다: ' + e.message
        + ' — .hpk 를 직접 놓아도 됩니다.';
  }
}

// ---- 실행 -----------------------------------------------------------------
let worker = null;
let booted = null;

function ensureWorker() {
  if (worker) return booted;
  worker = new Worker('worker.js', {type: 'module'});
  booted = new Promise((res, rej) => {
    worker.addEventListener('message', (e) => {
      const m = e.data;
      if (m.type === 'log') log(m.line);
      else if (m.type === 'ready') res();
      else if (m.type === 'failed') rej(new Error(m.error));
    });
    worker.addEventListener('error', (e) => rej(new Error(e.message)));
  });
  worker.postMessage({cmd: 'boot'});
  return booted;
}

$('start').addEventListener('click', async () => {
  state.running = true;
  refreshPlan();
  $('progress').hidden = false;
  $('result').hidden = true;
  $('log-box').open = true;
  const t0 = performance.now();
  const tick = setInterval(() => {
    const s = (performance.now() - t0) / 1000;
    $('phase').textContent = `${Math.floor(s / 60)}분 ${Math.floor(s % 60)}초 `
        + `경과 — 창을 닫지 마세요. 다른 탭을 봐도 계속 돕니다.`;
  }, 1000);
  try {
    $('bar-fill').style.width = '5%';
    await ensureWorker();
    $('bar-fill').style.width = '15%';
    const bundleBytes = new Uint8Array(await state.bundle.arrayBuffer());
    const report = await new Promise((res, rej) => {
      const onMsg = (e) => {
        const m = e.data;
        if (m.type === 'log') {
          log(m.line);
          if (/extracting/.test(m.line)) $('bar-fill').style.width = '30%';
          if (/injecting/.test(m.line)) $('bar-fill').style.width = '55%';
          if (/sha256/.test(m.line)) $('bar-fill').style.width = '95%';
        } else if (m.type === 'beat') {
          // 진행 표시용. 통계는 기록에만 남긴다.
        } else if (m.type === 'done') {
          worker.removeEventListener('message', onMsg);
          res(m.report);
        } else if (m.type === 'failed') {
          worker.removeEventListener('message', onMsg);
          rej(new Error(m.error));
        }
      };
      worker.addEventListener('message', onMsg);
      worker.postMessage({cmd: 'run', rom: state.rom, keys: state.keys,
                          bundleBytes, force: $('force').checked,
                          mode: $('mode-luma').checked ? 'luma' : 'rom'});
    });
    $('bar-fill').style.width = '100%';
    await showResult(report);
  } catch (e) {
    $('result').hidden = false;
    const extra = await diagnose(e);
    $('result').innerHTML = extra + '<p class="bad">실패: '
        + String(e.message).replace(/[<>&]/g, '') + '</p>'
        + '<p class="muted">기록을 펼쳐 마지막 줄을 보세요. 입력 해시 불일치는 '
        + '덤프가 다른 판본이라는 뜻이고, 키 오류는 키 파일이 없거나 다른 '
        + '콘솔의 것이라는 뜻입니다.</p>';
    $('bar-fill').style.width = '0%';
  } finally {
    clearInterval(tick);
    state.running = false;
    refreshPlan();
  }
});

// 실기용 결과는 해시로 대조할 상대가 없다(롬을 만들지 않았으니까). 대신 무엇이
// 들어갔는지와 켜야 할 설정을 말해 준다.
async function showLumaResult(r) {
  const box = $('result');
  box.innerHTML = `
    <h3>완료 — 실기용 LayeredFS 팩</h3>
    <table>
      <tr><th>대상</th><td>${r.title} (${r.target}) · 타이틀 ID
        <span class="mono">${r.title_id}</span></td></tr>
      <tr><th>내용</th><td>RomFS 교체 파일 ${r.files.length}개
        · 실행 코드 패치 code.ips ${r.ips} 바이트</td></tr>
      <tr><th>압축 파일</th><td>${human(r.size)}</td></tr>
      <tr><th>걸린 시간</th><td>${r.seconds}초</td></tr>
    </table>
    <p><button id="save">압축 파일 저장</button>
       <button id="wipe" class="plain">임시 파일 지우기</button></p>
    <ol class="muted">
      <li>압축을 SD 카드 <strong>루트</strong>에 풀어
        <span class="mono">SD:/luma/titles/${r.title_id}/</span> 가 되게 합니다.</li>
      <li>SELECT 을 누른 채로 켜서 Luma 설정에 들어가 <strong>Enable game
        patching</strong> 을 켜고 START 로 저장합니다.</li>
      <li>재부팅하고 게임을 실행합니다.</li>
    </ol>
    <p class="muted">이 팩의 RomFS 파일들은 재빌드한 ROM 안의 같은 경로와 바이트
    단위로 같은 것으로 확인된 것입니다. 다만 실기에서의 최종 동작은 님의 콘솔에서
    확인하셔야 합니다.</p>`;
  wireSaveWipe(r, `${r.title} (${r.target}) LayeredFS.zip`);
}

function wireSaveWipe(r, suggested) {
  $('save').addEventListener('click', async () => {
    const {handle} = await opfsFile(r.opfsPath);
    const file = await handle.getFile();
    if (window.showSaveFilePicker) {
      const dest = await window.showSaveFilePicker({suggestedName: suggested});
      const w = await dest.createWritable();
      await file.stream().pipeTo(w);
      log(`저장했습니다: ${suggested}`);
    } else {
      const a = document.createElement('a');
      a.href = URL.createObjectURL(file);
      a.download = suggested;
      a.click();
      setTimeout(() => URL.revokeObjectURL(a.href), 60000);
    }
  });
  $('wipe').addEventListener('click', () => {
    worker.postMessage({cmd: 'cleanup'});
    log('임시 파일을 지웠습니다.');
  });
}

// 실패가 "모듈을 못 받았다" 류일 때 추측을 남기지 않는다. 그 URL 을 캐시를 무시하고
// 다시 받아 상태와 MIME 을 그대로 보여 준다. 한 번은 서버가 .mjs 를 octet-stream 으로
// 줘서 이 실패가 났고, 그때 브라우저는 요청조차 하지 않아 서버 로그에 흔적이 없었다.
async function diagnose(err) {
  const m = /(https?:\/\/\S+?\.(?:mjs|js|wasm|whl))/.exec(String(err.message));
  if (!m) return '';
  try {
    const r = await fetch(m[1], {cache: 'reload'});
    const ct = r.headers.get('content-type') || '(없음)';
    const okType = /javascript|ecmascript/.test(ct) || !/\.(mjs|js)$/.test(m[1]);
    return `<p class="muted">진단: <span class="mono">${m[1]}</span> →
      HTTP ${r.status}, Content-Type <code>${ct}</code>` +
      (okType ? ' (정상)' : ' — 이 타입으로는 모듈을 실행할 수 없습니다.') +
      '</p><p class="bad">브라우저에 옛 응답이 캐시된 경우입니다. '
      + 'Ctrl+Shift+R(맥은 Cmd+Shift+R)로 강제 새로고침하세요.</p>';
  } catch (e2) {
    return `<p class="muted">진단: ${m[1]} 을 다시 받지도 못했습니다 (${e2}).</p>`;
  }
}

async function opfsFile(path) {
  const parts = path.split('/');
  const name = parts.pop();
  let dir = await navigator.storage.getDirectory();
  for (const p of parts) dir = await dir.getDirectoryHandle(p);
  return {handle: await dir.getFileHandle(name), dir, name};
}

async function showResult(r) {
  const box = $('result');
  box.hidden = false;
  const ok = r.reproduced;
  if (r.layeredfs) return showLumaResult(r);
  box.innerHTML = `
    <h3>${ok ? '완료 — 제작자 빌드와 같은 결과입니다'
             : '완료 — 다만 제작자 빌드와 다릅니다'}</h3>
    <table>
      <tr><th>파일</th><td>${r.title} (${r.target}) · ${human(r.size)}</td></tr>
      <tr><th>sha256</th><td class="mono">${r.sha256}</td></tr>
      <tr><th>제작자 빌드</th><td class="mono">${r.expected || '(없음)'}</td></tr>
      <tr><th>걸린 시간</th><td>${r.seconds}초</td></tr>
    </table>
    <p><button id="save">결과 저장</button>
       <button id="wipe" class="plain">임시 파일 지우기</button></p>
    <p class="muted">저장은 브라우저 임시 저장소에서 당신이 고른 위치로 그대로
    복사합니다. 저장한 뒤 임시 파일을 지우면 공간이 돌아옵니다.</p>`;
  if (!ok) {
    box.insertAdjacentHTML('beforeend',
        '<p class="bad">해시가 다릅니다. 다른 판본의 덤프이거나 키가 다른 '
        + '경우입니다. 쓰기 전에 확인하세요.</p>');
  }

  // 저장은 스트리밍 복사다. 2GB 를 램에 올리지 않는다.
  wireSaveWipe(r, `${r.title} (${r.target})`
      + r.out.slice(r.out.lastIndexOf('.')));
}

for (const id of ['mode-rom', 'mode-luma']) {
  $(id).addEventListener('change', () => {
    $('mode-rom-label').classList.toggle('sel', $('mode-rom').checked);
    $('mode-luma-label').classList.toggle('sel', $('mode-luma').checked);
  });
}

(async () => {
  const ok = await checkEnvironment();
  await loadChannel();
  refreshPlan();
  if (!ok) $('start').disabled = true;
})();
