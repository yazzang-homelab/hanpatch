// SAB 왕복 상대역: 비동기 대기(waitAsync)로 요청을 받고 동기 대기 중인 쪽을 깨운다.
let i32;
self.onmessage = (e) => {
  i32 = new Int32Array(e.data.sab);
  self.postMessage('ready');
  loop();
};

async function loop() {
  for (;;) {
    const w = Atomics.waitAsync(i32, 0, 0);
    if (w.async) await w.value;
    Atomics.store(i32, 2, Atomics.load(i32, 1) + 1);
    Atomics.store(i32, 0, 0);
    Atomics.notify(i32, 0);
  }
}
