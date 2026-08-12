// 워커 쪽 전제 확인: OPFS 동기 접근 핸들, SAB 왕복, Pyodide + pycryptodome 부팅.
self.onmessage = async () => {
  const r = {};
  try {
    const root = await navigator.storage.getDirectory();
    const fh = await root.getFileHandle('probe.bin', {create: true});
    const h = await fh.createSyncAccessHandle();
    const buf = new Uint8Array(1 << 20).fill(7);
    h.truncate(0);
    h.write(buf, {at: 0});
    h.write(buf, {at: 8 << 20});          // sparse write, 8MB 오프셋
    r.syncHandleSize = h.getSize();
    const back = new Uint8Array(16);
    h.read(back, {at: 8 << 20});
    r.syncHandleReadOk = back.every((x) => x === 7);
    h.flush();
    h.close();
    r.opfsSyncAccess = true;
  } catch (e) {
    r.opfsSyncAccess = String(e);
  }

  // SAB + Atomics.wait 왕복 (블로킹 클라이언트 + 비동기 프록시)
  try {
    const sab = new SharedArrayBuffer(1024);
    const i32 = new Int32Array(sab);
    const proxy = new Worker('capabilities-proxy.js');
    proxy.postMessage({sab});
    await new Promise((res) => { proxy.onmessage = res; });
    const t0 = performance.now();
    let ops = 0;
    for (let i = 0; i < 2000; i++) {
      i32[1] = i;
      Atomics.store(i32, 0, 1);
      Atomics.notify(i32, 0);
      const w = Atomics.wait(i32, 0, 1, 5000);
      if (w === 'timed-out' || Atomics.load(i32, 2) !== i + 1) break;
      ops++;
    }
    r.sabRoundTrips = ops;
    r.sabUsPerOp = +((performance.now() - t0) * 1000 / Math.max(ops, 1))
        .toFixed(1);
  } catch (e) {
    r.sab = String(e);
  }

  // Pyodide 부팅과 pycryptodome/pillow import
  try {
    const t0 = performance.now();
    importScripts('../vendor/pyodide/pyodide.js');
    const py = await loadPyodide({indexURL: '../vendor/pyodide/'});
    r.pyodideBootMs = Math.round(performance.now() - t0);
    await py.loadPackage(['pycryptodome', 'pillow']);
    r.python = py.runPython(`
import sys, hashlib
from Crypto.Cipher import AES
from PIL import Image
c = AES.new(b'0'*16, AES.MODE_CTR, nonce=b'', initial_value=1)
sys.version.split()[0] + ' aes=' + c.encrypt(b'x'*16).hex()[:8]
`);
    // WORKERFS: 드롭한 파일을 복사 없이 읽을 수 있는지
    const blob = new Blob([new Uint8Array(1 << 20).fill(3)]);
    const file = new File([blob], 'rom.bin');
    py.FS.mkdirTree('/mnt/in');
    py.FS.mount(py.FS.filesystems.WORKERFS, {files: [file]}, '/mnt/in');
    r.workerfs = py.runPython(`
import os
p = '/mnt/in/rom.bin'
f = open(p, 'rb'); f.seek(1 << 19); b = f.read(8); f.close()
f'{os.path.getsize(p)} {b.hex()}'
`);
  } catch (e) {
    r.pyodide = String(e);
  }
  self.postMessage(r);
};
