// OPFS 를 실제로 만지는 쪽. 파이오다이드 워커가 `Atomics.wait` 로 블로킹해 있는 동안
// 이 워커가 비동기 OPFS API 를 돌리고 결과를 SharedArrayBuffer 에 놓는다.
//
// 여기서 하는 일은 전부 브라우저 안이다. 네트워크로 나가는 코드가 한 줄도 없어야
// 하고(업로드 없음이 이 도구의 전제다), 실제로 없다.

const OP = {
  STAT: 1, MKDIR: 2, READDIR: 3, UNLINK: 4, RMDIR: 5, RENAME: 6,
  OPEN: 7, CLOSE: 8, READ: 9, WRITE: 10, TRUNCATE: 11, FLUSH: 12,
  RMTREE: 13, RELEASE: 14,
};
const ENOENT = 44, EEXIST = 20, EIO = 29, ENOTEMPTY = 55;

const HDR_INTS = 32;
const F64_OFF = HDR_INTS * 4;
const DATA_OFF = F64_OFF + 8 * 8;

const FLAG = 0, OPCODE = 1, IARG0 = 2, IRESULT = 3, IERRNO = 4, ILEN = 5;

let i32, f64, data, root;
// SharedArrayBuffer 를 들여다보는 뷰는 TextDecoder 도 OPFS 동기 핸들도 받지 않는다
// ("must not be shared"). 그래서 경계에서 한 번 복사한다. 8MB memcpy 는 디스크
// 왕복에 비하면 공짜다.
let staging;
const dec = new TextDecoder();
const enc = new TextEncoder();
const handles = new Map();   // fd -> {path, handle, refs}
const byPath = new Map();    // path -> fd
// 동기 접근 핸들을 만드는 일은 비동기 IPC 다(실측 왕복 0.9ms). 파이프라인은 같은
// 파일을 여러 번 여닫으므로, 닫으라는 말을 들어도 바로 닫지 않고 잠시 세워 둔다.
const parked = new Map();    // path -> handle (LRU, 삽입 순서가 곧 나이)
const PARK_MAX = 96;

function parkClose(path) {
  const h = parked.get(path);
  if (h) {
    h.flush();
    h.close();
    parked.delete(path);
  }
}

function parkClosePrefix(path) {
  parkClose(path);
  const prefix = path + '/';
  for (const k of [...parked.keys()]) {
    if (k.startsWith(prefix)) parkClose(k);
  }
}
let nextFd = 1;

self.onmessage = async (e) => {
  const sab = e.data.sab;
  i32 = new Int32Array(sab, 0, HDR_INTS);
  f64 = new Float64Array(sab, F64_OFF, 8);
  data = new Uint8Array(sab, DATA_OFF);
  staging = new Uint8Array(data.length);
  root = await navigator.storage.getDirectory();
  if (e.data.scratch) {
    // 스크래치 루트를 매번 비우고 시작한다. 앞선 시도의 잔해가 4GB 씩 남는다.
    await removeTree(e.data.scratch, true);
    await dirHandle(e.data.scratch, true);
  }
  self.postMessage({ready: true});
  loop();
};

function paths() {
  const raw = dec.decode(new Uint8Array(data.subarray(0, i32[ILEN])));
  return raw.split('\0');
}

async function loop() {
  // 왜 도는가: `Atomics.waitAsync` 로 깨어나면 왕복 하나가 2.7ms 였다(실측). 메타
  // 연산이 5만 번이면 그것만 150초다. 공유 메모리의 값은 즉시 보이므로, 바쁜 동안은
  // 짧게 돌면서 확인하고(왕복 수십 µs), 한동안 요청이 없으면 비동기 대기로 내려가
  // CPU 를 놓는다.
  let idle = 0;
  for (;;) {
    if (Atomics.load(i32, FLAG) !== 1) {
      if (++idle < 20000) {
        if ((idle & 255) === 0) await null;   // 마이크로태스크만 양보한다
        continue;
      }
      const w = Atomics.waitAsync(i32, FLAG, 0);
      if (w.async) await w.value;
      if (Atomics.load(i32, FLAG) !== 1) continue;
    }
    idle = 0;
    i32[IERRNO] = 0;
    i32[IRESULT] = 0;
    f64[2] = 0;
    try {
      await handle();
    } catch (err) {
      i32[IERRNO] = err && err.errno ? err.errno : EIO;
      if (!(err && err.errno)) console.error('[opfs-proxy]', err);
    }
    Atomics.store(i32, FLAG, 0);
    Atomics.notify(i32, FLAG);
  }
}

function fail(errno) {
  const e = new Error('opfs errno ' + errno);
  e.errno = errno;
  throw e;
}

const dirCache = new Map();   // 경로 -> FileSystemDirectoryHandle
const fileCache = new Map();  // 경로 -> FileSystemFileHandle

function forget(path) {
  fileCache.delete(path);
  dirCache.delete(path);
  const prefix = path + '/';
  for (const k of [...dirCache.keys()]) {
    if (k.startsWith(prefix)) dirCache.delete(k);
  }
  for (const k of [...fileCache.keys()]) {
    if (k.startsWith(prefix)) fileCache.delete(k);
  }
}

async function dirHandle(path, create) {
  const key = path.replace(/^\/+|\/+$/g, '');
  const hit = dirCache.get(key);
  if (hit) return hit;
  let h = root;
  let sofar = '';
  for (const part of path.split('/').filter(Boolean)) {
    sofar = sofar ? sofar + '/' + part : part;
    const c = dirCache.get(sofar);
    if (c) { h = c; continue; }
    try {
      h = await h.getDirectoryHandle(part, {create: !!create});
      dirCache.set(sofar, h);
    } catch (err) {
      if (err.name === 'NotFoundError') fail(ENOENT);
      if (err.name === 'TypeMismatchError') fail(ENOENT);
      throw err;
    }
  }
  dirCache.set(key, h);
  return h;
}

function split(path) {
  const parts = path.split('/').filter(Boolean);
  const name = parts.pop();
  return [parts.join('/'), name];
}

async function fileHandle(path, create) {
  const key = path.replace(/^\/+/, '');
  const hit = fileCache.get(key);
  if (hit) return hit;
  const [dir, name] = split(path);
  if (!name) fail(ENOENT);
  const d = await dirHandle(dir, create);
  try {
    const fh = await d.getFileHandle(name, {create: !!create});
    fileCache.set(key, fh);
    return fh;
  } catch (err) {
    if (err.name === 'NotFoundError') fail(ENOENT);
    if (err.name === 'TypeMismatchError') fail(ENOENT);
    throw err;
  }
}

async function removeTree(path, quiet) {
  const [dir, name] = split(path);
  if (!name) return;
  let d;
  try {
    d = await dirHandle(dir, false);
  } catch (err) {
    if (quiet) return;
    throw err;
  }
  try {
    await d.removeEntry(name, {recursive: true});
  } catch (err) {
    if (!quiet && err.name === 'NotFoundError') fail(ENOENT);
    if (!quiet && err.name === 'InvalidModificationError') fail(ENOTEMPTY);
  }
}

async function handle() {
  const op = i32[OPCODE];
  switch (op) {
    case OP.STAT: {
      const [path] = paths();
      {
        const p = parked.get(path);
        if (p) {   // 세워 둔 핸들이 최신 크기를 안다
          i32[IRESULT] = 1;
          f64[2] = p.getSize();
          return;
        }
      }
      if (path === '/' || path === '') {
        i32[IRESULT] = 1 | 2;
        return;
      }
      const key = path.replace(/^\/+/, '');
      // 열려 있으면 크기는 동기 핸들이 정답이다. getFile() 은 옛 값을 준다.
      const openFd = byPath.get(path);
      if (openFd !== undefined) {
        i32[IRESULT] = 1;
        f64[2] = handles.get(openFd).handle.getSize();
        return;
      }
      if (dirCache.has(key)) {
        i32[IRESULT] = 1 | 2;
        return;
      }
      const cachedFile = fileCache.get(key);
      if (cachedFile) {
        i32[IRESULT] = 1;
        f64[2] = (await cachedFile.getFile()).size;
        return;
      }
      const [dir, name] = split(path);
      let d;
      try {
        d = await dirHandle(dir, false);
      } catch (err) {
        if (err.errno === ENOENT) { i32[IRESULT] = 0; return; }
        throw err;
      }
      try {
        const fh = await d.getFileHandle(name);
        fileCache.set(key, fh);
        i32[IRESULT] = 1;
        f64[2] = (await fh.getFile()).size;
        return;
      } catch (err) {
        if (err.name !== 'NotFoundError' && err.name !== 'TypeMismatchError') {
          throw err;
        }
      }
      try {
        const dh = await d.getDirectoryHandle(name);
        dirCache.set(key, dh);
        i32[IRESULT] = 1 | 2;
      } catch (err) {
        i32[IRESULT] = 0;
      }
      return;
    }
    case OP.MKDIR: {
      const [path] = paths();
      const [dir, name] = split(path);
      const d = await dirHandle(dir, false);
      try {
        await d.getDirectoryHandle(name);
        fail(EEXIST);
      } catch (err) {
        if (err.errno === EEXIST) throw err;
      }
      await d.getDirectoryHandle(name, {create: true});
      return;
    }
    case OP.READDIR: {
      const [path] = paths();
      const d = await dirHandle(path, false);
      const names = [];
      for await (const name of d.keys()) names.push(name);
      const bytes = enc.encode(names.join('\n'));
      data.set(bytes, 0);
      i32[ILEN] = bytes.length;
      return;
    }
    case OP.UNLINK: {
      const [path] = paths();
      parkClose(path);
      const fd = byPath.get(path);
      if (fd !== undefined) {
        handles.get(fd).handle.close();
        handles.delete(fd);
        byPath.delete(path);
      }
      const [dir, name] = split(path);
      const d = await dirHandle(dir, false);
      forget(path.replace(/^\/+/, ''));
      try {
        await d.removeEntry(name);
      } catch (err) {
        if (err.name === 'NotFoundError') fail(ENOENT);
        throw err;
      }
      return;
    }
    case OP.RMDIR: {
      const [path] = paths();
      parkClosePrefix(path);
      const [dir, name] = split(path);
      const d = await dirHandle(dir, false);
      forget(path.replace(/^\/+/, ''));
      try {
        await d.removeEntry(name);
      } catch (err) {
        if (err.name === 'NotFoundError') fail(ENOENT);
        if (err.name === 'InvalidModificationError') fail(ENOTEMPTY);
        throw err;
      }
      return;
    }
    case OP.RMTREE: {
      const [path] = paths();
      parkClosePrefix(path);
      forget(path.replace(/^\/+/, ''));
      for (const [fd, h] of [...handles]) {
        if (h.path === path || h.path.startsWith(path + '/')) {
          h.handle.close();
          handles.delete(fd);
          byPath.delete(h.path);
        }
      }
      await removeTree(path, true);
      return;
    }
    case OP.RENAME: {
      const [from, to] = paths();
      parkClose(from);
      parkClose(to);
      forget(from.replace(/^\/+/, ''));
      forget(to.replace(/^\/+/, ''));
      const fd = byPath.get(from);
      if (fd !== undefined) {
        handles.get(fd).handle.close();
        handles.delete(fd);
        byPath.delete(from);
      }
      const src = await fileHandle(from, false);
      // OPFS 에 rename 이 없다. 옮겨 쓰고 지운다.
      const [toDir, toName] = split(to);
      const d = await dirHandle(toDir, true);
      const dst = await d.getFileHandle(toName, {create: true});
      const sh = await dst.createSyncAccessHandle();
      const file = await src.getFile();
      sh.truncate(0);
      const CH = 8 << 20;
      for (let at = 0; at < file.size; at += CH) {
        const buf = new Uint8Array(
            await file.slice(at, Math.min(at + CH, file.size)).arrayBuffer());
        sh.write(buf, {at});
      }
      sh.flush();
      sh.close();
      const [fromDir, fromName] = split(from);
      (await dirHandle(fromDir, false)).removeEntry(fromName);
      return;
    }
    case OP.OPEN: {
      const [path] = paths();
      const create = f64[1] === 1;
      const existing = byPath.get(path);
      if (existing !== undefined) {
        handles.get(existing).refs++;
        i32[IRESULT] = existing;
        return;
      }
      const revived = parked.get(path);
      if (revived) {
        parked.delete(path);
        const fd = nextFd++;
        handles.set(fd, {path, handle: revived, refs: 1});
        byPath.set(path, fd);
        i32[IRESULT] = fd;
        return;
      }
      const fh = await fileHandle(path, create);
      const h = await fh.createSyncAccessHandle();
      const fd = nextFd++;
      handles.set(fd, {path, handle: h, refs: 1});
      byPath.set(path, fd);
      i32[IRESULT] = fd;
      return;
    }
    case OP.CLOSE: {
      const fd = i32[IARG0];
      const h = handles.get(fd);
      if (!h) return;
      if (--h.refs <= 0) {
        handles.delete(fd);
        byPath.delete(h.path);
        parked.set(h.path, h.handle);
        while (parked.size > PARK_MAX) {
          parkClose(parked.keys().next().value);
        }
      }
      return;
    }
    case OP.READ: {
      const h = handles.get(i32[IARG0]);
      if (!h) fail(EIO);
      const len = f64[1];
      const got = h.handle.read(staging.subarray(0, len), {at: f64[0]});
      data.set(staging.subarray(0, got), 0);
      i32[IRESULT] = got;
      return;
    }
    case OP.WRITE: {
      const h = handles.get(i32[IARG0]);
      if (!h) fail(EIO);
      const len = f64[1];
      staging.set(data.subarray(0, len), 0);
      i32[IRESULT] = h.handle.write(staging.subarray(0, len), {at: f64[0]});
      return;
    }
    case OP.TRUNCATE: {
      const h = handles.get(i32[IARG0]);
      if (!h) fail(EIO);
      h.handle.truncate(f64[0]);
      return;
    }
    case OP.FLUSH: {
      const h = handles.get(i32[IARG0]);
      if (!h) fail(EIO);
      if (f64[1] === 1) {
        f64[2] = h.handle.getSize();
      } else {
        h.handle.flush();
      }
      return;
    }
    case OP.RELEASE: {
      for (const [fd, h] of [...handles]) {
        h.handle.flush();
        h.handle.close();
        handles.delete(fd);
        byPath.delete(h.path);
      }
      for (const k of [...parked.keys()]) parkClose(k);
      return;
    }
    default:
      fail(EIO);
  }
}
