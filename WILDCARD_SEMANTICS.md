# 와일드카드 의미론 — Resolver vs Linter

이 문서는 두 가지를 정리한다.

1. **실제 resolver의 치환 알고리즘** (`prompt.py`) — 생성 시점에 프롬프트가 실제로 어떻게 만들어지는가.
2. **Linter가 가정하는 strict tree 모델** (`analyze.py`) — 정적 검사가 "올바르다"고 보는 이상적인 트리 의미론.

두 모델은 **일부러 다르다**. resolver는 관대(loose)하고, linter는 엄격(strict)하다. 그 간극이 바로 린트가
잡아내는 "교정 대상"이다.

---

## 1. 데이터 모델 (`models.py`)

```
PromptSpec
├─ base: str                 # 시작 프롬프트(콤마 구분 토큰)
└─ blocks: list[WildcardBlock]

WildcardBlock                 # 하나의 치환 단계 (재귀 트리)
├─ input: str                 # 트리거 토큰들(콤마 구분). 빈 문자열 = "컨테이너"
├─ items: list[WildcardItem]  # 후보 목록
├─ sample_count: int = 1      # 가중 비복원 추출 개수(1은 기존 단일 선택)
└─ children: list[WildcardBlock]   # 이 블록 뒤에 적용되는 하위 블록(재귀)

WildcardItem                  # 후보 하나
├─ text: str                  # 삽입할 텍스트(콤마 포함 가능). "NOPROMPT" = 삽입 안 함
├─ weight: float (>= 0)       # 가중 랜덤 선택용. 0 = 선택 안 됨
└─ enabled: bool              # False = 비활성(옛 `# 주석`)
```

토큰은 모두 `_tokenize()`로 콤마 분리·trim한 단위로 다룬다. **플레이스홀더 토큰**은 정규식
`^__.+__$`에 맞는 토큰(예: `__hair__`, `__lips__`)을 말한다. 일반 단어 토큰(`blue eyes`)과 구분된다.

---

## 2. 실제 Resolver 치환 알고리즘 (`prompt.py`)

매 생성마다 단일 `master_seed`에서 파생된 RNG로 새로 해석된다(AFK 루프가 이걸 반복).

### 2.1 최상위 흐름 — `resolve_positive(spec, rng)`

```
tokens = tokenize(spec.base)
for block in spec.blocks:          # 최상위 블록을 "정의된 순서대로" 적용
    tokens = apply_block(tokens, block, rng)
return ", ".join(tokens)
```

### 2.2 블록 적용 — `apply_block(tokens, block, rng)`

```
tokens = substitute(tokens, block, rng)   # ① 이 블록 자신의 치환
for child in block.children:              # ② 자식 블록을 "무조건" 재귀 적용
    tokens = apply_block(tokens, child, rng)
return tokens
```

> **핵심(①→②):** 자식 블록은 부모의 치환 성공 여부와 **무관하게 항상 실행된다.** 자식은 그저 자기
> 트리거 토큰이 "현재 running 프롬프트 전체"에 있으면 발동한다. 트리는 **순서와 정리**를 줄 뿐,
> 활성화를 게이트하지 않는다.

### 2.3 단일 치환 — `substitute(tokens, block, rng)`

```
triggers = set(tokenize(block.input))
if not triggers:            # 컨테이너(빈 input): 치환 없음, 그대로 반환
    return tokens
first = 첫 번째로 triggers 에 속하는 토큰의 인덱스
if first is None:           # 트리거 토큰이 프롬프트에 없음 → no-op
    return tokens
chosen = choose_many(block.items, block.sample_count, rng)
if not chosen:             # 선택 가능한 후보 없음(전부 비활성/weight 0) → no-op
    return tokens
kept = [t for t in tokens if t not in triggers]      # 모든 트리거 토큰 제거(consume)
insert_at = first 위치를 kept 기준으로 환산한 인덱스
replacement = 선택된 후보들을 추출 순서대로 tokenize해 연결(NOPROMPT는 생략)
kept[insert_at:insert_at] = replacement              # 첫 트리거 자리에 삽입
return kept
```

요점:
- 트리거 토큰은 **모두 제거**되고, 선택된 후보 텍스트가 **첫 등장 위치**에 삽입된다.
- `sample_count > 1`이면 가중 비복원 추출하므로 한 후보가 중복 선택되지 않는다. 선택 가능 후보 수보다 크면
  가능한 후보를 모두 선택한다.
- 선택된 `NOPROMPT`는 추출 개수 한 자리를 차지하지만 아무것도 넣지 않는다.
- 후보 텍스트는 `__sub_token__`을 포함할 수 있고, 그건 이후(자식 또는 뒤 블록)에 다시 치환될 수 있다.

### 2.4 후보 선택 — `choose(items, rng)`

`enabled and weight > 0` 인 후보만 후보풀에 넣고 weight 비례로 고른다. 복수 추출 시 선택한 후보를
풀에서 제거하고 반복한다. `sample_count=1`은 기존 `_choose`를 정확히 한 번 호출하므로, 이 필드가
없던 과거 config/임베디드 메타데이터도 같은 master seed에서 RNG 소비와 결과가 그대로 유지된다.

### 2.5 Resolver의 스코프 = **전역·순차(loose)**

`apply_block`이 자식을 running 토큰 리스트 **전체**에 대해 돌리므로, 어떤 블록이든 다음에서 온
토큰으로 발동할 수 있다.

- `base` 프롬프트
- **조상**(ancestor)의 치환 출력
- **앞 형제**(earlier sibling) 및 그 서브트리의 출력 — DFS 순서상 먼저 실행되므로
- (트리 위치와 무관한 사실상 전역 토큰 풀)

즉 **resolver는 "토큰이 현재 프롬프트에 있기만 하면" 치환한다.** 트리 구조는 활성화 조건이 아니다.

---

## 3. Linter가 가정하는 Strict Tree 모델 (`analyze.py`)

Linter는 resolver의 관대함을 **버그가 숨는 공간**으로 본다. 그래서 더 엄격한 이상적 모델을 가정하고,
거기서 어긋나는 부분을 보고한다.

### 3.1 부모 전용(parent-only) 스코프

> **토큰은 부모 → 직속 자식으로만 흐른다.** 형제·조부모·전역으로는 흐르지 않는다.

- 어떤 블록의 **스코프**(트리거로 쓸 수 있는 토큰 집합):
  - 최상위 블록 → `base`의 토큰
  - 자식 블록 → **부모의 후보 출력**(`_produced_by(parent)`, 선택 가능 후보들의 텍스트 토큰 합집합)
  - 컨테이너(빈 input) → 자기 스코프를 자식에게 **그대로 통과**(passthrough), 자신은 emit 없음
- 블록은 트리거가 자기 스코프에 있어야만 **발동**한다.
- 블록이 emit한 플레이스홀더 토큰은 **직속 자식**(`_direct_consumers`)만 치환할 수 있다. 형제도,
  손자도, 뒤 블록도 안 된다.

이 모델에서는 트리 구조가 곧 의미다: `__mouth__` 브랜치가 꺼지면 그 자식 `__lips__` 블록은
"존재하지 않는" 것으로 취급된다.

### 3.2 검사 알고리즘 — `analyze_spec(spec)`

`recurse(blocks, scope, prefix)`로 트리를 내려가며 검사한다.

```
recurse(blocks, scope):
  for b in blocks:
    if b.input 가 비었으면(컨테이너):
        recurse(b.children, scope)          # 스코프 그대로 통과
        continue
    if b.trigger ∩ scope == ∅:              # 부모 전용 스코프에서 발동 불가
        if b.trigger 가 전역에도 없음:  → dead_block (error)
        else:                          → out_of_scope (warning)
        continue                            # 서브트리는 스코프를 못 받으므로 건너뜀
    if 선택 가능 후보 없음:  → no_candidate (warning); continue
    kids = b.children 의 트리거 토큰 합집합(선택 가능한 것만)
    for 후보 it in b.items(선택 가능, NOPROMPT 제외):
        for tok in tokenize(it.text):
            if tok 가 플레이스홀더 and tok ∉ kids:   # 직속 자식이 안 먹음 → 누수
                report_leak(...)
    recurse(b.children, _produced_by(b))    # 자식 스코프 = 이 블록의 emit
# base 레벨: base의 플레이스홀더는 최상위 블록이 소비해야 함
```

`reachable_tokens()`(전역 도달성)는 오직 **dead_block과 out_of_scope를 구분**하는 데만 쓴다.

### 3.3 보고 항목

| kind | 심각도 | 의미 |
|------|--------|------|
| `dead_block` | error | 트리거가 **어디서도** 생성되지 않아 절대 실행되지 않는 블록 (never enabled branch) |
| `out_of_scope` | warning | 트리거가 전역엔 있지만 **부모(최상단=base)** 출력엔 없음 → 부모 전용 스코프에선 발동 안 함. 형제/전역 토큰에 의존하는 **배치 교정 대상** |
| `no_candidate` | warning | 발동은 하지만 선택 가능한 후보가 없음(전부 비활성/weight 0) → 치환 불가 |
| `unsubstituted_token` | error | 어떤 블록도 치환하지 않는 플레이스홀더 → 최종 프롬프트에 그대로 남음(loose resolver에서도 누수) |
| `strict_leak` | error | **전역엔 소비자가 있지만** 직속 자식에는 없음 → 부모 전용 스코프에선 치환 불가. resolver는 형제/전역 흐름으로 우연히 치환하지만 트리 배치가 틀린 것 |

`unsubstituted_token` vs `strict_leak` 구분 기준: 그 토큰을 치환할 블록이 **전역적으로** 존재하는가
(`consumed` 집합). 존재하면 `strict_leak`, 아니면 `unsubstituted_token`.

---

## 4. 두 모델의 차이 (왜 strict_leak / out_of_scope가 뜨는가)

| 항목 | Resolver (실제) | Linter (strict tree) |
|------|------------------|----------------------|
| 자식 발동 게이트 | 없음 — 자식은 부모 치환과 무관하게 항상 실행 | 부모 스코프에 트리거 있어야만 발동 |
| 토큰 가시 범위 | 전역·순차(base + 조상 + 앞 형제 + …) | **부모 → 직속 자식만** |
| 형제끼리 토큰 전달 | 가능(앞 형제 → 뒤 형제, DFS 순서) | **불가** |
| 손자가 조부모 토큰 소비 | 가능(전역에 있으니) | 불가(부모가 다시 emit해야) |

linter가 무언가를 보고한다는 건 "**지금 생성은 동작하지만, 그건 트리 구조가 아니라 전역/순차 흐름에
기대고 있다**"는 뜻이다.

---

## 5. 예시

### 5.1 strict_leak — `__lips__` 오배치 (missionary_school)

```
__doing__ 후보: "..., __face__, ..."
  └ __face__ (자식)
       후보1: "wide-eyed, scared, __lips__"      ← __lips__ 직접 생성, __mouth__ 없음
       후보3: "ahegao, __blush__, __mouth__"
       ├ __blush__ (자식)
       └ __mouth__ (자식)
            후보: "open mouth, __lips__, ..."
            └ __lips__ (손자)   후보: "pink lips" | NOPROMPT
```

- **Resolver:** `__face__`가 후보1을 골라 `__lips__`를 직접 넣어도, `__lips__` 블록은
  (`__mouth__`의 자식이지만) **무조건 실행**되므로 `__lips__`를 치환한다 → 누수 없음. 동작한다.
- **Linter(strict):** `__lips__`는 `__face__`가 emit한다. `__face__`의 **직속 자식**은
  `__blush__`, `__mouth__`뿐 — `__lips__`를 소비하는 직속 자식이 없다. `__lips__` 블록은 `__mouth__`
  아래(손자)라, `__mouth__`가 꺼지면 존재하지 않는다 → **`strict_leak`**.

  **교정:** `__lips__` 블록을 `__mouth__`의 자식이 아니라 **`__face__`의 직속 자식**(= `__mouth__`의
  형제)으로 옮긴다. 그러면 `__face__`가 emit한 `__lips__`를 직속 자식이 받는다 → CLEAN.

### 5.2 out_of_scope + strict_leak — 형제 cascade

```
__p__ 후보: "..., __x__"
  ├ __x__ (자식)  후보: "foo, __z__"     ← __z__ 생성
  └ __z__ (자식)                          ← 트리거 __z__ 는 "형제 __x__"가 생성
```

- **Resolver:** `__x__`(앞 형제)가 먼저 실행되어 `__z__`를 넣고, `__z__`(뒤 형제)가 그걸 소비 →
  동작한다.
- **Linter(strict):** 형제 전달 불허.
  - `__x__`가 emit한 `__z__`를 `__x__`의 직속 자식이 안 먹음 → **`strict_leak`**.
  - `__z__` 블록의 트리거 `__z__`가 부모 `__p__` 출력엔 없음 → **`out_of_scope`**.

  **교정:** `__z__` 블록을 `__p__`의 형제가 아니라 **`__x__`의 자식**으로 옮긴다.

### 5.3 CLEAN — 올바른 트리 (missionary_test)

모든 블록의 트리거가 부모(최상단=base) 출력에 있고, 모든 블록이 emit한 플레이스홀더를 직속 자식이
받는 구조. 부모 전용 스코프에서 누수·고립 없음.

---

## 6. 교정 가이드 (린트 → 행동)

| 보고 | 보통의 원인 | 교정 |
|------|-------------|------|
| `dead_block` | 트리거 토큰 오타, 또는 그 토큰을 만드는 블록을 지움 | 트리거 철자 확인 / 생성하는 블록 추가 |
| `out_of_scope` | 형제·전역에서 트리거를 받는 레거시 cascade | 그 토큰을 **생성하는 블록의 자식**으로 이동 |
| `no_candidate` | 후보를 전부 비활성/weight 0 으로 둠 | 후보 하나 이상 활성화 |
| `unsubstituted_token` | 치환 블록 누락/오타(`__lenn__` 등) | 해당 토큰용 블록 추가 또는 철자 수정 |
| `strict_leak` | 소비 블록을 너무 깊이(손자/형제 아래) 배치 | 토큰을 **생성하는 블록의 직속 자식**으로 끌어올리기(드래그로 재배치 가능) |

> 정책 요약: **resolver는 전역·순차로 관대하게 동작**하지만, 설정은 **부모 전용 트리**로 작성·검증하는
> 것을 권장한다. 그래야 트리 구조가 곧 의도가 되고, 토큰 충돌·우연한 의존이 사라진다.

---

## 관련 파일

- `prompt.py` — resolver (`resolve_positive`, `_apply_block`, `_substitute`, `_choose`)
- `models.py` — `PromptSpec` / `WildcardBlock` / `WildcardItem`, 레거시 업그레이드
- `analyze.py` — strict tree linter (`analyze_spec`, `recurse`, `_direct_consumers`, `_produced_by`)
- `server.py` — `POST /api/analyze`
- `static/main.js` — UI(`검증` 버튼, 이슈 렌더링)
