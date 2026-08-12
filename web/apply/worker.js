// 브라우저 안에서 `hanpatch apply` 를 그대로 돌린다.
//
// 왜 파이오다이드인가: 3DS 컨테이너는 CTR 암호화라 위치 의존적이고, 출력은 RomFS
// 재조립과 IVFC 해시 재계산을 포함한다. 이걸 자바스크립트로 다시 쓰면 검증된
// 파이프라인의 사본이 하나 더 생기고, 두 사본은 반드시 갈라진다. 그래서 같은
// 파이썬 코드를 wasm 으로 돌리고, 결과 해시가 제작자의 빌드와 같은지로 확인한다.
//
// 입력 ROM 은 WORKERFS 로 읽기 전용 마운트한다(복사도 업로드도 없다). 스크래치와
// 출력은 OPFS 에 두는데, 실측 스크래치가 4.3GB 라서 램에 올릴 수 없기 때문이다.

import {Bridge, SAB_SIZE, makeOpfsFs} from './opfs-bridge.js';

const SCRATCH = '/hanpatch-scratch';
// 버전이 박힌 경로다. 이유: 한때 이 파일이 잘못된 MIME(octet-stream)으로 나갔고
// 캐시 수명이 일주일이었다. 모듈 import 는 MIME 을 검사하므로, 그때 캐시에 들어간
// 브라우저는 서버를 고친 뒤에도 요청조차 하지 않고 계속 실패한다(실측: nginx 로그에
// 요청이 없는데 클라이언트는 "Failed to fetch dynamically imported module").
// URL 이 곧 캐시 키이므로, 버전을 경로에 넣으면 그 실패는 재현될 수 없다.
let PYODIDE_DIR = './vendor/pyodide-0.26.4/';
let WHEEL = './wheels/hanpatch-1.0.0-py3-none-any.whl';
let py = null;
let bridge = null;

function post(type, payload) { self.postMessage({type, ...payload}); }

let lastBeat = 0;
function heartbeat(phase) {
  const now = performance.now();
  if (now - lastBeat < 3000) return;
  lastBeat = now;
  const s = bridge ? bridge.stats : null;
  post('beat', {phase, stats: s && {
    ops: s.ops, blockedS: +(s.blockedMs / 1000).toFixed(1),
    readMB: +(s.bytesRead / 1e6).toFixed(1),
    writeMB: +(s.bytesWritten / 1e6).toFixed(1),
  }, elapsedS: +(now / 1000).toFixed(1)});
}
self.hanpatchBeat = heartbeat;
function log(line) { post('log', {line}); }

async function boot() {
  const t0 = performance.now();
  // 프록시 워커와 SAB 다리를 먼저 세운다. 파이오다이드가 뜨기 전에 마운트해야
  // 파이썬이 처음 파일을 열 때 이미 준비돼 있다.
  const sab = new SharedArrayBuffer(SAB_SIZE);
  bridge = new Bridge(sab);
  const proxy = new Worker(new URL('./opfs-proxy.js', import.meta.url),
                           {type: 'classic'});
  await new Promise((res, rej) => {
    proxy.onmessage = (e) => (e.data.ready ? res() : rej(e.data));
    proxy.onerror = rej;
    proxy.postMessage({sab, scratch: SCRATCH.slice(1)});
  });
  log('저장소 준비됨 (OPFS, 디스크에 직접 씀)');

  // 어느 휠과 어느 파이오다이드를 쓸지는 배포가 정한다. 이 파일만 짧게 캐시되고
  // 나머지는 내용 주소이므로, 코드를 고쳐도 옛 것이 남지 않는다.
  try {
    const r = await fetch(new URL('./build.json', import.meta.url), 
                          {cache: 'no-store'});
    if (r.ok) {
      const b = await r.json();
      if (b.pyodide) PYODIDE_DIR = './' + b.pyodide.replace(/^\.?\//, '');
      if (b.wheel) WHEEL = './' + b.wheel.replace(/^\.?\//, '');
    }
  } catch (e) {
    log('build.json 을 읽지 못했습니다. 기본 경로로 갑니다: ' + e);
  }

  // 모듈 워커에는 importScripts 가 없다. 파이오다이드의 ESM 진입점을 쓴다.
  const {loadPyodide} = await import(
      new URL(PYODIDE_DIR + 'pyodide.mjs', import.meta.url).href);
  py = await loadPyodide({
    indexURL: new URL(PYODIDE_DIR, import.meta.url).href,
    stdout: (s) => log(s),
    stderr: (s) => log(s),
  });
  log(`파이썬 ${py.runPython('import sys; sys.version.split()[0]')} 준비 `
      + `(${((performance.now() - t0) / 1000).toFixed(1)}초)`);

  await py.loadPackage(['pycryptodome', 'pillow'], {messageCallback: () => {}});
  const fs = makeOpfsFs(py.FS, py.PATH, py.ERRNO_CODES, bridge, SCRATCH);
  py.FS.mkdirTree(SCRATCH);
  py.FS.mount(fs, {}, SCRATCH);
  py.FS.mkdirTree('/mnt/rom');
  py.FS.mkdirTree('/mnt/keys');
  py.FS.mkdirTree('/mnt/bundle');

  await py.loadPackage(new URL(WHEEL, import.meta.url).href);
  log('hanpatch ' + py.runPython(
      'import hanpatch, hanpatch.release as r; str(r.FORMAT)')
      + ' 번들 포맷 지원');
  post('ready', {});
}

function mountFiles(dir, files) {
  if (!files.length) return;
  py.FS.mount(py.FS.filesystems.WORKERFS, {files}, dir);
}

async function run({rom, keys, bundleBytes, force, profile, mode}) {
  const t0 = performance.now();
  mountFiles('/mnt/rom', [rom]);
  mountFiles('/mnt/keys', keys || []);
  py.FS.writeFile(SCRATCH + '/bundle.hpk', bundleBytes);

  const romPath = '/mnt/rom/' + rom.name;
  const ext = rom.name.includes('.') ? rom.name.slice(rom.name.lastIndexOf('.'))
                                     : '.3ds';
  py.globals.set('_rom', romPath);
  py.globals.set('_ext', ext);
  py.globals.set('_force', !!force);
  py.globals.set('_mode', mode === 'luma' ? 'luma' : 'rom');
  py.globals.set('_scratch', SCRATCH);

  if (profile) {
    py.runPython(`
import time
from hanpatch.platforms import threeds
from hanpatch import adapter as _adapter

def _timed(mod, name):
    fn = getattr(mod, name, None)
    if fn is None or getattr(fn, '_timed', False):
        return
    def wrapper(*a, **k):
        t0 = time.monotonic()
        try:
            return fn(*a, **k)
        finally:
            print(f'[prof] {name} {time.monotonic() - t0:.1f}s', flush=True)
    wrapper._timed = True
    setattr(mod, name, wrapper)

for n in ('open_ncch', 'dump', 'dump_romfs', 'unpack_romfs', 'build_romfs',
          'rebuild', 'rebuild_cia', 'content_hashes', 'superblock_hashes'):
    _timed(threeds, n)
`);
  }

  let report;
  try {
    report = py.runPython(`
import json, os, sys
os.environ['HANPATCH_KEYS'] = '/mnt/keys'
os.environ['HOME'] = _scratch
work = _scratch + '/work'
os.makedirs(work, exist_ok=True)
out = _scratch + '/out/patched' + _ext
os.makedirs(_scratch + '/out', exist_ok=True)

from hanpatch import release
info = release.inspect(_scratch + '/bundle.hpk')
print(f"번들: {info['title']} ({info['target']}) / "
      f"{info['entries']}개 문자열 / 매니페스트 {info['digest'][:16]}")
if _mode == 'luma':
    # 실기용: 컨테이너를 다시 만들지 않는다. 바뀐 파일만 SD 카드 배치로 낸다.
    pack_dir = _scratch + '/out/layeredfs'
    r = release.luma(_scratch + '/bundle.hpk', _rom, out=pack_dir,
                     force=_force, workdir=work)
    import zipfile
    out = _scratch + '/out/' + f"{info['title']} ({info['target']}) LayeredFS.zip"
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for base, _d, files in os.walk(pack_dir):
            for name in sorted(files):
                p = os.path.join(base, name)
                z.write(p, os.path.relpath(p, pack_dir))
    print(f"{out} " f"{len(r['files'])}개 파일, "
          f"{os.path.getsize(out) / 1e6:.1f} MB (압축)")
    r['out'] = out
    r['sha256'] = ''
    r['reproduced'] = True
    r['expected'] = None
    r['layeredfs'] = True
    r['source_expected'] = info.get('source_sha256')
    r['title'] = info['title']
    r['target'] = info['target']
else:
    r = release.apply(_scratch + '/bundle.hpk', _rom, out=out, force=_force,
                      workdir=work)
    r['expected'] = info.get('output_sha256')
    r['source_expected'] = info.get('source_sha256')
    r['title'] = info['title']
    r['target'] = info['target']
json.dumps(r)
`);
  } catch (e) {
    post('failed', {error: String(e.message || e)});
    return;
  }
  const r = JSON.parse(report);
  r.seconds = +((performance.now() - t0) / 1000).toFixed(1);
  r.opfsPath = SCRATCH.slice(1) + '/out/' + r.out.split('/').pop();
  r.size = bridge.stat(SCRATCH + '/out/' + r.out.split('/').pop()).size;
  // 스크래치(수 GB)는 결과를 내보낸 뒤에 비운다. 결과 파일은 남긴다.
  bridge.rmtree(SCRATCH.slice(1) + '/work');
  bridge.release();   // 잠금을 놓아 페이지가 결과 파일을 열 수 있게 한다
  post('done', {report: r});
}

self.onmessage = async (e) => {
  try {
    if (e.data.cmd === 'boot') await boot();
    else if (e.data.cmd === 'run') await run(e.data);
    else if (e.data.cmd === 'cleanup') {
      bridge.rmtree(SCRATCH.slice(1));
      post('cleaned', {});
    }
  } catch (err) {
    post('failed', {error: String((err && err.message) || err)});
  }
};
