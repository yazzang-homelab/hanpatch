// OPFS 를 Emscripten 파일시스템으로 이어 주는 다리 — 클라이언트 쪽(파이오다이드 워커).
//
// 왜 필요한가: Pyodide 의 `mountNativeFS` 는 디렉터리 전체를 MEMFS 로 복사해 두고
// `syncfs` 로 되쓴다. 2GB ROM 하나를 패치하려면 스크래치가 4.3GB(실측: 풀린 RomFS
// 2.8GB + 재조립 1.4GB)라서 램에 올릴 수 없다. OPFS 의 동기 접근 핸들
// (`createSyncAccessHandle`)은 진짜 디스크에 스트리밍으로 읽고 쓰지만, 만드는 일이
// 비동기다. 파이썬의 `open()` 은 동기다. 그래서 실제 파일 조작은 프록시 워커가
// 맡고, 이쪽은 SharedArrayBuffer + `Atomics.wait` 로 그 워커를 동기적으로 기다린다.
// 이 구조가 성립하려면 cross-origin isolation(COOP/COEP)이 필요하다.
//
// 프로토콜은 요청 하나에 왕복 한 번이다. 실측 왕복 19µs, 파이프라인이 4MB 단위로
// 읽고 쓰므로 2GB 파일 하나가 왕복 500여 번이다.

export const OP = {
  STAT: 1, MKDIR: 2, READDIR: 3, UNLINK: 4, RMDIR: 5, RENAME: 6,
  OPEN: 7, CLOSE: 8, READ: 9, WRITE: 10, TRUNCATE: 11, FLUSH: 12,
  RMTREE: 13, RELEASE: 14,
};

export const HDR_INTS = 32;
export const F64_OFF = HDR_INTS * 4;
export const F64_N = 8;
export const DATA_OFF = F64_OFF + F64_N * 8;
export const DATA_SIZE = 8 << 20;
// 열린 파일마다 두는 병합 창. 8KB 짜리 파이썬 버퍼를 이 크기로 뭉쳐 보낸다.
export const CACHE = 4 << 20;
export const SAB_SIZE = DATA_OFF + DATA_SIZE;

// Int32 슬롯
const FLAG = 0;      // 0 유휴, 1 요청
const OPCODE = 1;
const IARG0 = 2;     // fd 등
const IRESULT = 3;
const IERRNO = 4;
const ILEN = 5;      // 데이터 영역 유효 길이
// Float64 슬롯: 0 = position, 1 = length/size, 2 = result64

export class Bridge {
  constructor(sab) {
    this.sab = sab;
    this.i32 = new Int32Array(sab, 0, HDR_INTS);
    this.f64 = new Float64Array(sab, F64_OFF, F64_N);
    this.data = new Uint8Array(sab, DATA_OFF, DATA_SIZE);
    this.enc = new TextEncoder();
    this.dec = new TextDecoder();
    this.cache = new Map();   // fd -> 병합 창
    // 어디서 시간을 쓰는지 추측하지 않기 위한 계기. 왕복 수와 막힌 시간을 센다.
    this.stats = {ops: 0, blockedMs: 0, bytesRead: 0, bytesWritten: 0,
                  byOp: {}};
  }

  call(op, {path = null, fd = 0, pos = 0, len = 0, bytes = null,
            path2 = null} = {}) {
    let n = 0;
    if (path !== null) {
      const a = this.enc.encode(path);
      const b = path2 === null ? null : this.enc.encode(path2);
      if (a.length + 1 + (b ? b.length + 1 : 0) > DATA_SIZE) {
        throw new Error('path too long');
      }
      this.data.set(a, 0);
      this.data[a.length] = 0;
      n = a.length + 1;
      if (b) {
        this.data.set(b, n);
        this.data[n + b.length] = 0;
        n += b.length + 1;
      }
    } else if (bytes) {
      this.data.set(bytes, 0);
      n = bytes.length;
    }
    this.i32[OPCODE] = op;
    this.i32[IARG0] = fd;
    this.i32[ILEN] = n;
    this.f64[0] = pos;
    this.f64[1] = len;
    const t0 = performance.now();
    Atomics.store(this.i32, FLAG, 1);
    Atomics.notify(this.i32, FLAG);
    while (Atomics.load(this.i32, FLAG) === 1) {
      Atomics.wait(this.i32, FLAG, 1, 30000);
    }
    this.stats.ops++;
    if (self.hanpatchBeat && (this.stats.ops & 1023) === 0) {
      self.hanpatchBeat('io');
    }
    this.stats.blockedMs += performance.now() - t0;
    this.stats.byOp[op] = (this.stats.byOp[op] || 0) + 1;
    if (op === OP.READ) this.stats.bytesRead += this.i32[IRESULT];
    if (op === OP.WRITE) this.stats.bytesWritten += this.i32[IRESULT];
    const err = this.i32[IERRNO];
    if (err) {
      const e = new Error(`opfs op ${op} failed: errno ${err}`);
      e.errno = err;
      throw e;
    }
    return {
      result: this.i32[IRESULT],
      result64: this.f64[2],
      len: this.i32[ILEN],
    };
  }

  stat(path) {
    this.sync();   // 크기를 묻기 전에 미뤄 둔 쓰기를 내려야 한다
    const r = this.call(OP.STAT, {path});
    return {exists: !!(r.result & 1), isDir: !!(r.result & 2),
            size: r.result64};
  }

  mkdir(path) { this.sync(); this.call(OP.MKDIR, {path}); }
  rmdir(path) { this.sync(); this.call(OP.RMDIR, {path}); }
  unlink(path) { this.sync(); this.call(OP.UNLINK, {path}); }
  rmtree(path) { this.sync(); this.call(OP.RMTREE, {path}); }
  // 결과 파일을 페이지가 읽으려면 동기 접근 핸들의 배타 잠금을 풀어야 한다.
  release() { this.sync(); this.cache.clear(); this.call(OP.RELEASE, {}); }
  rename(from, to) { this.sync(); this.call(OP.RENAME, {path: from,
                                                       path2: to}); }

  readdir(path) {
    this.sync();
    const r = this.call(OP.READDIR, {path});
    // TextDecoder 는 공유 버퍼 뷰를 받지 않는다. 먼저 복사한다.
    const raw = this.dec.decode(new Uint8Array(this.data.subarray(0, r.len)));
    return raw ? raw.split('\n') : [];
  }

  open(path, create) {
    this.sync();
    const r = this.call(OP.OPEN, {path, len: create ? 1 : 0});
    return r.result;
  }

  close(fd) {
    this.flushFd(fd);
    this.cache.delete(fd);
    this.call(OP.CLOSE, {fd});
  }

  size(fd) {
    this.flushFd(fd);
    return this.call(OP.FLUSH, {fd, len: 1}).result64;
  }

  truncate(fd, size) {
    this.flushFd(fd);
    this.call(OP.TRUNCATE, {fd, pos: size});
  }

  flush(fd) {
    this.flushFd(fd);
    this.call(OP.FLUSH, {fd});
  }

  // ---- 쓰기 병합과 읽기 선행 ------------------------------------------------
  // 파이썬은 8KB 단위로 읽고 쓴다. 왕복 하나가 19µs 이므로 그대로 흘리면 1.5GB
  // 재조립이 한 시간을 넘긴다(실측: 600초 안에 끝나지 않음). 열린 파일마다 창
  // 하나를 두고 순차 접근을 뭉쳐 보내면 왕복 수가 CACHE/8KB 배로 줄어든다.
  slot(fd) {
    let s = this.cache.get(fd);
    if (!s) {
      s = {buf: new Uint8Array(CACHE), start: -1, len: 0, dirty: false};
      this.cache.set(fd, s);
    }
    return s;
  }

  flushFd(fd) {
    const s = this.cache.get(fd);
    if (!s || !s.dirty) return;   // 깨끗한 읽기 창은 버리지 않는다
    if (s.len) this.rawWrite(fd, s.buf, 0, s.len, s.start);
    s.dirty = false;
    s.start = -1;
    s.len = 0;
  }

  sync() {
    for (const fd of this.cache.keys()) this.flushFd(fd);
  }

  read(fd, buffer, offset, length, position) {
    if (length >= CACHE) {
      this.flushFd(fd);
      return this.rawRead(fd, buffer, offset, length, position);
    }
    const s = this.slot(fd);
    if (s.dirty) {
      // 쓰던 창에서 읽는 경우가 있다(헤더를 되읽는다). 먼저 내려쓴다.
      this.flushFd(fd);
    }
    if (s.start < 0 || position < s.start || position >= s.start + s.len) {
      s.start = position;
      s.len = this.rawRead(fd, s.buf, 0, CACHE, position);
      if (s.len <= 0) { s.start = -1; s.len = 0; return 0; }
    }
    const from = position - s.start;
    const n = Math.min(length, s.len - from);
    buffer.set(s.buf.subarray(from, from + n), offset);
    return n;
  }

  write(fd, buffer, offset, length, position) {
    if (length >= CACHE) {
      this.flushFd(fd);
      return this.rawWrite(fd, buffer, offset, length, position);
    }
    const s = this.slot(fd);
    const contiguous = s.dirty && position === s.start + s.len &&
        s.len + length <= CACHE;
    if (!contiguous) {
      this.flushFd(fd);
      s.start = position;
      s.len = 0;
      s.dirty = true;
    }
    s.buf.set(buffer.subarray(offset, offset + length), s.len);
    s.len += length;
    return length;
  }

  rawRead(fd, buffer, offset, length, position) {
    let done = 0;
    while (done < length) {
      const n = Math.min(length - done, DATA_SIZE);
      const r = this.call(OP.READ, {fd, pos: position + done, len: n});
      const got = r.result;
      if (got <= 0) break;
      buffer.set(this.data.subarray(0, got), offset + done);
      done += got;
      if (got < n) break;
    }
    return done;
  }

  rawWrite(fd, buffer, offset, length, position) {
    let done = 0;
    while (done < length) {
      const n = Math.min(length - done, DATA_SIZE);
      const chunk = buffer.subarray(offset + done, offset + done + n);
      const r = this.call(OP.WRITE, {fd, pos: position + done, len: n,
                                     bytes: chunk});
      const put = r.result;
      if (put <= 0) throw new Error('short write');
      done += put;
    }
    return done;
  }
}

// ---- Emscripten 파일시스템 ------------------------------------------------
// NODEFS 와 같은 방식(경로 기반, 노드는 필요할 때 만든다)이다. 노드마다 OPFS 상의
// 절대 경로를 들고 있고, 크기와 존재 여부는 매번 프록시에 묻는다.

export function makeOpfsFs(FS, PATH, ERRNO_CODES, bridge, prefix = '') {
  const dirMode = 16384 | 0o777;   // S_IFDIR
  const fileMode = 32768 | 0o666;  // S_IFREG
  const linkMode = 40960 | 0o777;  // S_IFLNK

  // OPFS 에는 심볼릭 링크가 없다. 그런데 어댑터는 손대지 않는 RomFS 를 링크로
  // 세워 둔다(DQ7 는 1.4GB 중 두 디렉터리만 실제로 복사한다). 링크를 지원하지
  // 않으면 매 빌드마다 기가바이트를 옮겨야 한다. 스크래치는 실행마다 비우므로
  // 링크 표는 메모리에만 둔다.
  const links = new Map();   // 절대 경로 -> 대상 경로

  // 마운트 루트의 parent 는 자기 자신이다. 즉 노드 사슬만으로는 마운트 지점의
  // 이름을 알 수 없어 트리가 OPFS 루트에 그대로 쏟아진다(실측: work/, out/ 이
  // 최상위에 생겼다). 그래서 접두어를 명시적으로 받는다.
  function toPath(node) {
    const parts = [];
    for (let n = node; n.parent !== n; n = n.parent) parts.unshift(n.name);
    const rel = parts.join('/');
    return prefix + (rel ? '/' + rel : '') || '/';
  }

  function err(code) {
    const e = new FS.ErrnoError(code);
    throw e;
  }

  const node_ops = {
    getattr(node) {
      const p = toPath(node);
      const target = links.get(p);
      if (target !== undefined) {
        return {
          dev: 1, ino: node.id, mode: linkMode, nlink: 1, uid: 0, gid: 0,
          rdev: 0, size: target.length,
          atime: new Date(0), mtime: new Date(0), ctime: new Date(0),
          blksize: 4096, blocks: 1,
        };
      }
      if (node._size !== undefined && !FS.isDir(node.mode)) {
        return {
          dev: 1, ino: node.id, mode: fileMode, nlink: 1, uid: 0, gid: 0,
          rdev: 0, size: node._size,
          atime: new Date(0), mtime: new Date(0), ctime: new Date(0),
          blksize: 4096, blocks: Math.ceil(node._size / 4096),
        };
      }
      const st = bridge.stat(p);
      if (!st.exists) err(ERRNO_CODES.ENOENT);
      if (!st.isDir) node._size = st.size;
      return {
        dev: 1, ino: node.id, mode: st.isDir ? dirMode : fileMode,
        nlink: 1, uid: 0, gid: 0, rdev: 0,
        size: st.isDir ? 4096 : st.size,
        atime: new Date(0), mtime: new Date(0), ctime: new Date(0),
        blksize: 4096, blocks: Math.ceil(st.size / 4096),
      };
    },
    setattr(node, attr) {
      if (attr.size !== undefined && !FS.isDir(node.mode)) {
        const p = toPath(node);
        const fd = bridge.open(p, true);
        try { bridge.truncate(fd, attr.size); } finally { bridge.close(fd); }
        node._size = attr.size;
      }
      // 모드와 시각은 OPFS 에 저장할 곳이 없다. 조용히 받아들인다.
    },
    lookup(parent, name) {
      const p = PATH.join2(toPath(parent), name);
      if (links.has(p)) return createNode(parent, name, linkMode);
      const st = bridge.stat(p);
      if (!st.exists) err(ERRNO_CODES.ENOENT);
      const node = createNode(parent, name, st.isDir ? dirMode : fileMode);
      if (!st.isDir) node._size = st.size;
      return node;
    },
    mknod(parent, name, mode, dev) {
      const p = PATH.join2(toPath(parent), name);
      if (FS.isDir(mode)) {
        bridge.mkdir(p);
      } else if (FS.isFile(mode)) {
        bridge.close(bridge.open(p, true));
        const node = createNode(parent, name, mode);
        node._size = 0;
        return node;
      } else {
        err(ERRNO_CODES.EPERM);   // 장치 노드는 없다
      }
      return createNode(parent, name, mode);
    },
    rename(oldNode, newDir, newName) {
      const from = toPath(oldNode);
      if (links.has(from)) {
        links.set(PATH.join2(toPath(newDir), newName), links.get(from));
        links.delete(from);
        oldNode.name = newName;
        oldNode.parent = newDir;
        return;
      }
      bridge.rename(toPath(oldNode), PATH.join2(toPath(newDir), newName));
      oldNode.name = newName;
      delete oldNode.parent.contents[oldNode.name];
      oldNode.parent = newDir;
    },
    unlink(parent, name) {
      const p = PATH.join2(toPath(parent), name);
      if (links.delete(p)) return;
      bridge.unlink(p);
    },
    rmdir(parent, name) {
      const p = PATH.join2(toPath(parent), name);
      for (const k of [...links.keys()]) {
        if (k === p || k.startsWith(p + '/')) links.delete(k);
      }
      bridge.rmdir(p);
    },
    readdir(node) {
      const p = toPath(node);
      const prefix = (p === '/' ? '' : p) + '/';
      const names = bridge.readdir(p);
      for (const k of links.keys()) {
        if (k.startsWith(prefix) && !k.slice(prefix.length).includes('/')) {
          names.push(k.slice(prefix.length));
        }
      }
      return ['.', '..'].concat(names);
    },
    symlink(parent, name, target) {
      const p = PATH.join2(toPath(parent), name);
      links.set(p, target);
      return createNode(parent, name, linkMode);
    },
    readlink(node) {
      const target = links.get(toPath(node));
      if (target === undefined) err(ERRNO_CODES.EINVAL);
      return target;
    },
  };

  const stream_ops = {
    open(stream) {
      if (FS.isDir(stream.node.mode)) return;
      stream.opfsFd = bridge.open(toPath(stream.node), true);
    },
    close(stream) {
      if (stream.opfsFd !== undefined) {
        bridge.close(stream.opfsFd);
        stream.opfsFd = undefined;
      }
    },
    read(stream, buffer, offset, length, position) {
      if (length === 0) return 0;
      return bridge.read(stream.opfsFd, buffer, offset, length, position);
    },
    write(stream, buffer, offset, length, position) {
      if (length === 0) return 0;
      const n = bridge.write(stream.opfsFd, buffer, offset, length, position);
      const end = position + n;
      const node = stream.node;
      if (node._size === undefined || end > node._size) node._size = end;
      return n;
    },
    llseek(stream, offset, whence) {
      let position = offset;
      if (whence === 1) position += stream.position;
      else if (whence === 2) {
        position += stream.node._size !== undefined ? stream.node._size
                                                    : bridge.size(stream.opfsFd);
      }
      if (position < 0) err(ERRNO_CODES.EINVAL);
      return position;
    },
    allocate() {},
    mmap() { err(ERRNO_CODES.ENODEV); },
    msync() { return 0; },
  };

  function createNode(parent, name, mode) {
    const node = FS.createNode(parent, name, mode, 0);
    node.node_ops = node_ops;
    node.stream_ops = stream_ops;
    if (FS.isDir(mode)) node.contents = {};
    return node;
  }

  return {
    mount(mount) {
      const root = createNode(null, '/', dirMode);
      return root;
    },
    node_ops, stream_ops,
  };
}
