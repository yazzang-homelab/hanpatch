# hanpatch

게임 ROM 한글화(현지화) 파이프라인. 타이틀의 텍스트를 추출하고, 손에 있는 아무 모델로든
번역한 다음, **기계가 결과의 일관성을 증명하기 전까지 패치 빌드를 거부한다.**

레퍼런스 구현: *Crimson Shroud* (3DS) 한국어 패치 전체 — 문자열 3262개, 문자열마다
독립 심판 2명, 바이트 단위로 동일하게 재생성되는 빌드.

English: [README.en.md](README.en.md)

```bash
pip install -e .
hanpatch init --title "My Game" --adapter my_game --profile profiles/my_game.json
hanpatch extract && hanpatch fonts
hanpatch translate --family dialogue --workers 4
hanpatch qa --judges 2 --workers 4
hanpatch build && hanpatch verify
```

## 왜 프롬프트 개선이 아니라 게이트인가

게임 스크립트의 기계 번역은 눈으로 몇 줄 훑어봐서는 절대 안 보이는 방식으로 깨진다.
등장인물 이름이 500줄에 걸쳐 세 가지로 표기되고, 1페이지에서는 들어가던 줄이 3페이지에서
넘치고, `<player>` 태그가 조용히 사라지고, 심판이 자기가 만든 출력을 자기가 통과시킨다.
프롬프트로는 하나도 고쳐지지 않는다. 유일하게 통하는 방법은 각 실패 유형이 기계적으로
배제될 때까지 빌드 자체를 불가능하게 만드는 것이다.

모든 검사는 실제로 배포되는 산출물에서 주장을 다시 유도한다. 건너뛸 수 있는 게이트는
게이트가 아니므로 `--force` 같은 것은 없다.

## 게이트 순서

| # | 게이트 | 걸러내는 것 |
|---|--------|-------------|
| 1 | `glossary` | 고유명사가 두 가지로 표기된 경우, UI 라벨 표기를 산문에까지 강제한 경우 |
| 2 | `capacity` | 해당 레이아웃 그룹이 실제로 렌더한 가장 넓은 페이지보다 긴 텍스트 |
| 3 | `materialize` | 규칙으로 생성한 행이 자기 검증기를 통과하지 못한 경우 |
| 4 | `audit` | 미번역 행, 태그 손상, 말투(register) 이탈, 의미 중복 |
| 5 | `manifest` | 아무것도 거르지 않음 — 배포 대상 문자열 전체를 하나의 다이제스트로 봉인 |
| 6 | `qagate` | **바로 그 원문·번역 쌍에 대해** 독립 심판 N명의 통과 기록이 없는 항목 |

이후 패커는 한 바이트라도 쓰기 전에 QA 검증을 프로세스 내에서 다시 수행한다. 승인 토큰은
편의 장치일 뿐이고 권위는 갓 수행한 재검증에 있으므로, 매니페스트와 토큰을 함께 손봐도
빌드는 실패한다.

## 구조

```
hanpatch/config.py       프로젝트 + 타이틀 프로파일 해석
hanpatch/adapter.py      extract/inject/verify 계약
hanpatch/pipeline.py     fail-closed 게이트 러너
hanpatch/*.py            용어집, 번역, 레이아웃, 감사, 매니페스트, QA
hanpatch/platforms/      컨테이너 계층 (threeds: CIA/NCCH/RomFS/BCFNT/BLZ)
hanpatch/formats/        메시지·아카이브 리더
hanpatch/adapters/       타이틀당 모듈 하나
profiles/                타이틀당 JSON 하나: 마크업, 용어, 예산, 폰트
```

코어는 ROM을 읽지 않는다. 어댑터는 표현(문장)을 결정하지 않는다 — 어댑터가 표현 관련
모듈을 import하면 테스트 스위트가 실패시킨다. 타이틀 추가는 프로파일 하나 + 어댑터 하나이고
그 외에는 아무것도 바뀌지 않는다. 플랫폼 추가는 디렉터리 하나다.

## 실제로 재사용 가능한 범위

**타이틀·플랫폼 무관.** 용어집/스코프 모델, 레이아웃 용량 유도, 태그 골격 보존, 프로바이더
로테이션이 붙은 샤드 단위 재개 가능 번역, 감사 게이트, 봉인 매니페스트, 해시로 묶인 예외
승인(waiver)을 갖춘 다중 심판 QA 패널, 스크립트북 생성기. 이 중 어느 것도 ROM이 무엇인지
모른다.

**3DS 타이틀.** 컨테이너 계층 전체: CIA, CCI/`.3ds` 카트리지 덤프, 순수 NCCH; 문서화된 모든
NCCH 암호화 방식(0, 1, 10, 11), fixed/zero 키, seed 암호화, 타이틀 키로 암호화된 콘텐츠;
IVFC RomFS 읽기 및 재빌드; NCSD 파티션 재빌드; BLZ; RGBA4444 셰이딩 의미를 올바르게 처리하는
BCFNT.

키 소재는 절대 동봉되지 않는다. `boot9.bin`, `keys.txt`, `seeddb.bin`을 직접 넣으면
`hanpatch keys`가 어떤 슬롯이 존재하는지 정확히 알려준다. 암호화 방식 0을 쓰는 타이틀은
아무것도 필요 없다. 유도된 모든 키는 섹션을 실제로 복호화해 매직을 확인하는 방식으로
검증되므로, 슬롯이 틀리면 쓰레기 데이터가 나오는 대신 큰 소리로 실패한다.

**타이틀별로 직접 해야 하는 것.** 메시지/아카이브 포맷과 프로파일. *Crimson Shroud*의 경우
어댑터 250줄과 JSON 파일 하나다.

## 설치와 사전 준비

Python 3.9 이상, `pycryptodome`, `pillow`.

```bash
git clone https://github.com/yazzang-homelab/hanpatch
cd hanpatch && pip install -e .
```

번역과 심판에는 모델 엔드포인트가 필요하다. OpenAI 호환 base URL이면 무료 티어를 포함해
무엇이든 동작한다. 엔드포인트 목록은 `hanpatch/providers.py`의 `ENDPOINTS`에 정의하고,
API 키는 환경변수 또는 dotenv 파일에서 읽는다. 기본 탐색 위치는 `~/.hanpatch/env`, `~/.env`
이며 `HANPATCH_ENV`에 경로를 `:`로 이어 붙여 바꿀 수 있다. 키를 소스에 적어 넣지 말 것.

3DS 컨테이너가 암호화되어 있으면 키 소재를 아래 순서로 찾는다.

1. `$HANPATCH_KEYS` (경로 또는 `:`로 이은 경로 목록)
2. `<프로젝트>/keys/`, `~/.hanpatch/keys/`
3. 개별 환경변수 (`HANPATCH_KEY_slot0x25KeyX` 등)

인식하는 파일은 `boot9.bin`(또는 `boot9_prot.bin`), `keys.txt`/`aes_keys.txt`,
`seeddb.bin`, `encTitleKeys.bin`이다. 준비 상태는 `hanpatch keys`로 확인한다.

## 작업 순서

프로젝트는 `hanpatch.json`을 가진 디렉터리 하나다. 프로젝트 루트는 `$HANPATCH_PROJECT` →
`hanpatch.json`을 가진 가장 가까운 상위 디렉터리 → 현재 디렉터리 순으로 해석된다.

```bash
hanpatch init --title "My Game" --adapter my_game \
              --profile profiles/my_game.json --rom game.cia --target ko
hanpatch info                       # 프로젝트/어댑터 상태 확인
hanpatch keys                       # 로드된 키 슬롯 확인 (3DS 암호화 타이틀)

hanpatch extract                    # ROM -> work/text_src.json + extracted/
hanpatch fonts                      # 대상 언어 폰트 빌드 (폭 측정 기준이 된다)

hanpatch translate --family dialogue --workers 4
hanpatch qa --judges 2 --workers 4   # 독립 심판 패널 충원
hanpatch gates                       # 게이트 전부 실행 후 매니페스트 봉인

hanpatch build --out dist/patched.cia
hanpatch verify                      # 빌드된 ROM을 다시 읽어 검증
hanpatch book --out build/scriptbook  # 대역 스크립트북 디렉터리 (검수용, 기본값 build/scriptbook)
```

`hanpatch all`은 `fonts + gates + build + verify`를 한 번에 돌린다. `translate`는 재개
가능하므로 중단해도 다시 실행하면 남은 샤드만 처리한다. 검증에 실패한 행을 다시 돌릴 때는
`--refail`, QA에서 떨어진 행을 다시 돌릴 때는 `--qafail`을 쓴다.

주요 산출물 경로:

```
work/text_src.json          추출된 원문 {family: [{key, en, jp?, note?}, ...]}
work/<lang>/manifest.json   봉인된 매니페스트 {"digest": ..., "entries": {...}}
extracted/                  재빌드 재료 (원본 컨테이너, 원본 폰트)
dist/                       빌드된 ROM, 릴리스 번들
```

## 새 타이틀 추가하기

프로파일 JSON 하나와 어댑터 모듈 하나면 된다.

프로파일에는 그 타이틀의 마크업 문법(`tag_pattern`, `hard_break`, `page_break`,
`movable_tags`, `control_tags`), 용어집 스코프(`name_keys`, `terms`, `ui_only_families`,
`ui_only_terms`, `hard_families`), 레이아웃 예산(`budget`, `capacity`, 그리고
줄바꿈이 없는 행을 엔진이 직접 배치하는지를 밝히는 `engine_wraps` — 기본값은
없다. 선언하지 않으면 레이아웃 게이트가 그 행을 아예 측정하지 않는지 알 수
없으므로 파이프라인이 멈춘다),
측정에 쓸 폰트(`font_src`, `font_out`), 문체(`style`, `register`), 번역 제외 규칙
(`skip_families`, `skip_key_patterns`, `skip_value_patterns`)을 적는다.

어댑터는 `hanpatch/adapters/`에 모듈로 넣고 `@register('이름')`으로 등록한 뒤
`hanpatch.json`의 `"adapter"`에서 그 이름을 고른다. 구현할 연산은 세 개다.

- `extract(rom)` → `work/text_src.json`을 쓰고, 이후 단계가 필요한 것(측정용 폰트, 재빌드용
  원본 컨테이너)을 `extracted/`에 남긴다.
- `inject(entries, rom, out)` → 패치된 ROM을 쓴다. `entries`는 항상 봉인된 매니페스트에서
  오고, 작업 중인 번역 메모리에서 오지 않는다.
- `verify(rom, entries)` → 빌드된 ROM을 다시 읽어 모든 항목이 살아남았음을 증명한다. 문제
  문자열 목록을 반환하며 비어 있으면 정상이다.

선택 훅으로 `build_fonts()`와 `font_paths()`가 있다.

## 훔쳐 갈 만한 아이디어

- **용량은 추측하지 않고 측정한다.** 한 레이아웃 그룹에서 원본이 실제로 렌더한 가장 넓은
  페이지가 증명된 한계다. 숫자를 접어 `family/key-shape`로 묶으므로 `system/treasure`는
  자기 자신의 한 줄로 한계가 정해진다.
- **줄바꿈은 새로 발명하지 말고 스크립트가 하던 대로 재현한다.** 어떤 태그가 줄을 바꾸는지는
  타이틀별 사실이다(`hard_break`, 비어 있을 수도 있다 — Crimson Shroud의 `<br>`은 메시지를
  진행시킬 뿐이다). 스크립트가 새 줄을 원하는 지점에서는 제어 태그 직후에 줄바꿈을 넣는데,
  그 위치는 레이아웃이므로 번역문에 그대로 이식하고 문장 중간의 줄바꿈만 재배치한다. 하나를
  접어 없애면 두 문장이 한 줄로 합쳐져 화면 밖으로 밀려난다.
- **용어집은 스코프를 가진다.** `Dead`, `Key`, `Cure`는 UI 라벨로는 강제 대상이지만 산문에서는
  평범한 단어이므로 강제 금지다.
- **심판은 둘이고, 생산자는 자기 출력을 심판할 수 없다.** 심판 하나면 상관된 거짓 음성이
  발생한다 — 그 심판이 통과시킨 문자열 5개를 표본 조사했더니 실제 결함이 4개 있었다.
- **판정은 구조화해서 읽고 키워드로 냄새 맡지 않는다.** 형식이 깨진 판정은 버려서 그 행이
  pending으로 남아 프로바이더를 교체해 다시 돌게 하고, 억지로 통과로 합성하지 않는다.
- **예외 승인은 해시에 묶인다** — `sha1(원문 + '\0' + 배포 텍스트)`. 어느 쪽이든 수정하면
  승인이 만료되어 빌드를 막는다.
- **글리프의 권위는 패킹된 폰트**이며, ROM에서 다시 읽어낸 것이다. 유니코드 범위가 아니다.
- **바이너리 포맷은 쓰기 전에 측정한다.** 3DS 폰트 시트는 RGBA4444이고 `A`가 커버리지,
  `RGB`는 텍스트 색과 곱해지는 셰이딩 마스크다. 순진하게 `255 - coverage`로 하면 글리프가
  까맣게 뭉치고 테두리만 밝아진다 — PNG로 보면 괜찮지만 실기에서는 읽을 수 없다.

## 테스트

```bash
python3 tests/test_gates.py                                    # 로직만
HANPATCH_PROJECT=/path/to/project python3 tests/test_gates.py  # + 코퍼스 케이스
python3 tests/test_containers.py                               # 암호/컨테이너/델타
```

각 케이스는 과거에 실제로 통과해버린 적이 있는 구체적인 공격이다. 전각 라틴 문자 오염, 제어
태그 순서 뒤바뀜, 제어 구간 밖으로 텍스트 이동, 용량 초과, 용어 이탈, 샤드 경쟁, 매니페스트
위조, 심판 ID 위장, 다른 키의 예외 승인 재사용, 매니페스트와 토큰 동시 수정.

컨테이너 스위트는 암호 입력을 스스로 합성한다 — 자기가 고른 타이틀 키를 자기가 만든 공용 키로
감싼다 — 따라서 CBC/IV 배치, 공용 키 탐색, 부트ROM 앵커 스캔, seed 유도가 실제 키 소재 없이도
전부 커버된다. 이 스위트는 실제 버그를 잡았다: 키 스크램블러의 덧셈이 127번 비트를 넘어
자리올림될 수 있는데, 마스킹하지 않은 합을 회전시키면 그 자리올림이 다시 접혀 들어와 서로
다른 KeyY 값이 하나의 키로 붕괴했다.

## 스킬

`skills/hanpatch/`는 에이전트 스킬(Claude Code / GJC 포맷)로, 원칙, 어댑터 계약, QA 패널
설계, 3DS 포맷 노트를 담고 있다. `~/.claude/skills/` 또는 `~/.gjc/skills/`로 복사해서 쓴다.

## 범위와 한계 (솔직하게)

- 어떤 게이트도 게임을 직접 플레이하는 것을 대신하지 못한다. 레이아웃은 폰트 메트릭 기준으로
  검사하며 실제 렌더러로 검사하지 않는다.
- 하나의 제어 구간 안에서 두 문자열의 텍스트가 서로 바뀐 경우는 구조적으로 탐지할 수 없다.
- JSON 산출물은 무결성 검사 대상이며 서명 대상은 아니다. 위협 모델은 우발적 손상과 모델
  오류이고, 악의적인 로컬 편집자는 아니다.
- 심판/생산자 분리는 출처 로깅이 도입되기 전에 번역된 행에 대해서는 최선 노력 수준이다.
- **재빌드된 산출물의 RSA 서명은 구조적으로 무효다.** 개인키가 없으므로 NCSD/NCCH 서명은
  원본에서 그대로 복사되지만, 그 서명이 덮는 헤더 필드(미디어 크기, 파티션 테이블, 섹션
  오프셋, 슈퍼블록 해시)는 다시 쓰인다. 즉 **소매 상태의 콘솔은 이 산출물을 로드하지
  않는다** — 서명 검사를 우회하는 환경(sighax/boot9strap 계열 CFW)이 필요하다. 이
  파이프라인의 모든 검사가 초록이어도 이 사실은 변하지 않는다. 실기 확인에서 부팅이나 설치가
  실패하면 번역이나 컨테이너보다 먼저 이 조건을 확인해야 한다. 예외는 무손질 라운드트립처럼
  재빌드가 원본과 **바이트 동일**한 경우뿐이며, 그때는 원본과 구별되지 않으므로 서명도
  그대로 유효하다.

## 배포

`hanpatch release`는 당신의 번역물 — 봉인된 매니페스트, 빌드된 폰트, 프로파일 — 을 묶고,
받는 사람이 자기 ROM에 적용한다.

```bash
hanpatch release --out "MyPatch.hpk"        # 레퍼런스 타이틀 기준 0.34 MB
hanpatch apply "MyPatch.hpk" --rom their.cia
```

번들은 기대 입력 해시와 출력 해시를 기록하므로, 적용하면 원작자의 빌드를 바이트 단위로
재현하거나 아니면 재현되지 않았다고 말해준다. 레퍼런스 타이틀 실측: 340 KB 번들이 249 MB
ROM을 4초 만에 원작자와 동일한 sha256으로 재구성한다.

순수 바이너리 델타도 있다(`hanpatch delta`, xdelta3 또는 의존성 없는 애플라이어가 딸린 내장
블록 포맷). 다만 CTR 암호화 컨테이너에는 잘못된 도구다. 키스트림이 위치 의존적이라 한 바이트만
밀려도 이후 모든 매치가 파괴된다. 두 백엔드 모두 레퍼런스 타이틀에서 전체 ROM의 약 82% 크기가
나온다. 번들을 쓸 것.

## 법적 사항

**무엇이 합법인지는 당신이 판단한다. 이 프로젝트가 대신 판단해주지 않는다.**

이 저장소는 소스만 배포한다 — 게임 데이터, ROM, 추출/번역된 텍스트, 키 소재, 폰트 모두
포함하지 않는다. 패치할 게임, 덤프에 필요한 키 소재, 임베드할 폰트는 당신이 준비하며 그 셋과
당신이 배포하는 결과물 전부에 대한 책임도 당신에게 있다. 저작권과 기술적 보호조치 우회 관련
법률은 국가마다 다르므로 본인 관할 법을 확인하라.

여기 있는 어떤 기능도 합법성에 대한 추측을 근거로 빠지지 않았다. 전문은
[NOTICE.md](NOTICE.md)를 보고, 보증 부인은 MIT 라이선스를 보라 — 전면 적용된다.

레퍼런스 빌드는 [NeoDunggeunmo](https://github.com/Dalgona/neodgm)(OFL-1.1)를 사용하며 이를
명시한다.

## 라이선스

MIT — [LICENSE](LICENSE) 참고.
