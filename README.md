# comfy-web

ComfyUI 2단계(base → hires) 애니메이션 생성 워크플로우를 제어하는 가벼운 웹 인터페이스.

원본 ComfyUI 워크플로우(`api_prompt_template.json`)를 그대로 박제해 쓰는 대신, **설정값으로부터 워크플로우 그래프를 매번 동적으로 조립**한다. 모든 파라미터를 웹 폼에서 직접 제어할 수 있고, 다음을 지원한다:

- **와일드카드 프롬프트 구성** — Base prompt + 순차 치환 블록으로, 매 생성마다 랜덤하게 프롬프트를 굴린다.
- **조건부 LoRA** — `PPWCLoraDetector` 트리를 대신해, 서버가 *최종 프롬프트*의 토큰을 보고 LoRA를 켠다.
- **다중 구성(Config) 관리** — 설정 묶음을 저장·전환·복제하고 AFK·재현에 재사용한다.
- **AFK 백그라운드 모드** — 정지할 때까지 생성을 반복하며 결과를 webp로 저장하고, 동작 중 이미지를 브라우저에 실시간 표시한다.
- **재현성** — 단일 master seed가 와일드카드+샘플러 시드를 모두 결정하고, webp에 config·seed를 임베드해 한 장을 그대로 복구한다.
- **모바일 대응** — 좁은 화면에서 단일 컬럼으로 리플로우.

```
┌─────────┐   JSON    ┌───────────┐  validate  ┌──────────┐  graph  ┌─────────┐
│ 브라우저 │ ────────▶ │ server.py │ ─────────▶ │ workflow │ ──────▶ │ ComfyUI │
│ (form)  │  WebSocket │ (FastAPI) │  pydantic  │  builder │  /prompt└────┬────┘
└─────────┘ ◀──────── └───────────┘ ◀───────────────────────── 이미지 ─────┘
            진행률·이미지              comfy.py (websocket 중계)
```

## 파이프라인

원본 템플릿이 하던 2단계 샘플링을 그대로 재현한다:

```
EmptyLatentImage (config의 width×height)
  └─ KSampler (1단계: steps40 / cfg4 / er_sde / denoise1)
       └─ VAEDecode ──▶ ★ 중간 이미지 (base)
            └─ ImageUpscaleWithModel (RealESRGAN x4)
                 └─ ImageScaleBy 0.5  (→ 순수 2배)
                      └─ VAEEncode  (픽셀 → latent 재인코딩)
                           └─ KSamplerAdvanced (2단계 hires: start_at_step40 / steps45)
                                └─ VAEDecode ──▶ ★ 최종 이미지 (hires)
```

모델 조립 체인: `UNET → [활성 LoRA들] → ModelSamplingAuraFlow(shift) → DCWModelPatch → (두 샘플러 공유)`.
CLIP / VAE는 별도 로더에서 공급된다.

## 와일드카드 프롬프트 구성

Positive 프롬프트는 단일 텍스트가 아니라 **Base prompt + 순차 치환 블록** 구조다. 생성 시점(서버)에 매번 새로 해석되므로, AFK 루프를 돌리면 매 장마다 다른 프롬프트가 나온다.

각 블록은 두 필드로 이뤄지며, 블록은 ▲▼ 버튼으로 순서를 바꿀 수 있다(적용 순서 = 위→아래):

- **input** — 콤마로 구분한 트리거 토큰들. 현재 프롬프트에 이 토큰이 있으면 *모두 consume(제거)*되고, **첫 등장 위치**에 선택된 한 줄이 삽입된다. (없으면 그 블록은 no-op)
- **wildcards** — 후보 목록(멀티라인). 매 생성마다 한 줄을 가중 랜덤 선택한다.

후보 줄 문법(앞뒤 공백 제거 후):

| 형태 | 의미 |
|------|------|
| `text` | 일반 치환(콤마 포함 가능, 여러 토큰으로 삽입됨) |
| `|n| text` | 가중치 `n`(0 이상 실수, 기본 1). `|0|`이면 선택 안 됨 |
| `# ...` | 주석, 무시 |
| `NOPROMPT` | input 토큰을 consume만 하고 아무것도 삽입하지 않음 |

치환되지 않고 남은 `__토큰__`(양끝이 double underscore인 콤마 토큰, 예: `__artist__`)은 정적 분석(**검증**)에서 오류로 표시된다 — 와일드카드 의미론과 린트 규칙은 `WILDCARD_SEMANTICS.md` 참고.

예: base에 `@char`를 두고, 블록 input `@char` / wildcards를 아래처럼 두면 캐릭터 태그가 매번 굴려진다.

```
|2| @m00m00
@m4me
|0.5| @kn0r
# @k4ry1n disabled
NOPROMPT
```

## 조건부 LoRA (최종 프롬프트 기반)

`PPWCLoraDetector` 트리가 하던 일을 서버 코드로 대체한다. 와일드카드가 모두 해석된 **최종 프롬프트**를 기준으로:

- **always** — 토큰과 무관하게 항상 설정 강도로 적용.
- **conditional** — 설정한 **트리거 토큰**이 최종 프롬프트의 콤마 토큰과 *정확히 일치*할 때만 적용(없거나 빈 트리거면 비활성).

즉 위 예에서 `@m00m00`이 뽑히면 `@m00m00` 트리거를 가진 LoRA가 그 생성에만 켜진다. 생성 직후 UI에 실제 해석된 프롬프트와 적용된 LoRA 목록이 표시된다.

## 저장 / AFK

생성 결과를 PNG 원본 대신 **webp**로 압축해 디스크에 저장한다.

- **경로 템플릿** — `저장 폴더`(기본 `output`) 하위에 템플릿으로 파일명을 만든다. placeholder: `{date}`(YYYY-MM-DD) · `{time}`(HHMMSS) · `{datetime}` · `{seed}` · `{index}`(AFK 순번) · `{cname}`(현재 구성 이름) · `{ext}`. 예: `{cname}/{date}-{time}.{ext}`.
- **webp 품질 / 무손실** — `webp_quality`(1–100), `webp_lossless` 토글.
- **대화형 저장** — `대화형 생성도 저장`을 켜면 단발 생성 결과도 저장된다.
- **AFK 모드** — `AFK 시작`을 누르면 서버가 백그라운드에서 매번 와일드카드/시드를 다시 굴려 생성→저장을 반복한다. `AFK 생성 개수`가 0이면 `정지`를 누를 때까지 무한, N이면 N장 후 자동 종료. 브라우저를 닫아도 서버 프로세스가 살아 있으면 계속 동작하며, 동작 중에는 진행 상황·이미지가 대화형과 똑같이 실시간 표시된다.

## 구성(Config) 관리

설정 묶음을 여러 개 저장해두고 전환할 수 있다. 각 구성은 이름·생성일·수정일과 전체 `GenerationConfig`를 가지며, `configs/<id>.json` 파일 하나로 저장된다(기존 `config.json`은 첫 실행 시 `default` 구성으로 자동 이관).

좌측 최상단 **구성 관리** 패널에서:

- **목록/선택** — 구성을 이름·생성일·수정일로 정렬해 보여주고, 행을 누르면 그 구성을 폼에 불러온다.
- **추가/복제/이름변경/삭제** — 현재 폼으로 새 구성을 만들거나, 선택 구성을 복제·개명·삭제한다.
- **구성 저장**(헤더 버튼 또는 `Ctrl/Cmd+S`) — 현재 폼 값을 선택된 구성에 덮어쓴다(수정일 갱신).

선택된 구성 이름은 저장 경로의 `{cname}` placeholder로 쓰여, 구성별로 폴더를 나눠 저장하기 좋다.

## 재현성 (Reproducibility)

생성 한 장은 **단일 master seed + config**로 완전히 재현된다.

- **단일 RNG** — `random.Random(master_seed)` 하나가 고정된 순서로 ① 모든 와일드카드 선택 → ② `-1`(랜덤) 샘플러 시드(stage1·stage2)를 굴린다. 따라서 같은 시드+config면 프롬프트도 시드도 동일하게 나온다. master seed는 브라우저(JSON 숫자 = float64)를 무손실로 오가도록 `2^53-1` 이하로 둔다.
- **임베딩** — 저장 시 최종 webp에 `gzip(JSON{master_seed, config, resolved})`를 커스텀 RIFF 청크(`cMTA`)로 덧붙인다. WebP는 RIFF 컨테이너라 미지 청크는 디코더가 무시 → 어디서나 정상 표시되면서 재현 정보를 품는다.
- **복구** — 재현 패널에 그 webp를 올리면(`POST /api/reproduce`) config가 폼에 로드되고 master seed가 채워진다. `생성 ▶`을 누르면 그 시드로 재현된다. 빈 시드는 매번 새 랜덤.
- **오프라인 재현** — `uv run python repro.py <image.webp>` 가 webp에서 메타를 읽어 ComfyUI로 다시 생성한다.

> 실험으로 검증: 한 장 생성 → webp 임베드 → webp에서 `(config, seed)` 복구 → 재생성 했을 때, 최종 PNG가 **바이트 단위로 동일**했다(ComfyUI 샘플링이 고정 시드에서 결정적이라 가능).

## 빠른 시작

```bash
# 의존성 설치 (uv 사용)
uv sync

# ComfyUI 주소 설정 (.env)
echo 'COMFY_BASE_URL="http://localhost:58188"' > .env

# 서버 실행
uv run python main.py          # → http://127.0.0.1:8000
```

ComfyUI 인스턴스가 떠 있어야 하며, 워크플로우가 쓰는 모델/LoRA/업스케일러가 설치돼 있어야 한다. 호스트·포트는 환경변수로 바꾼다:

```bash
HOST=0.0.0.0 PORT=9000 uv run python main.py
```

## 웹 UI에서 제어 가능한 것

| 영역 | 항목 |
|------|------|
| 구성 관리 | 구성 목록/정렬(이름·생성일·수정일)·선택·추가·복제·이름변경·삭제 |
| 프롬프트 | Positive(Base + 와일드카드 블록 트리 추가/삭제/▲▼순서변경/드래그·정적 검증) / Negative(단일 텍스트) |
| 모델 | UNET · CLIP · VAE (설치된 것 드롭다운) |
| 이미지 크기 | width · height · batch + 가로/세로 전환 |
| LoRA | 설치된 LoRA 자유 추가/삭제, 행마다 **강도** · **모드**(`always`/`conditional`) · **트리거 토큰** |
| 1단계 | seed(-1=랜덤) · steps · cfg · denoise · sampler · scheduler |
| 업스케일 | 모델 · scale_by (결과 해상도 실시간 표시) |
| 2단계 | noise_seed · steps · start_at_step · end_at_step · cfg · add_noise · sampler · scheduler (유효 디노이즈 % 표시) |
| 저장 / AFK | 대화형 저장 토글 · 저장 폴더 · 경로 템플릿 · webp 품질/무손실 · AFK 개수 · 시작/정지 |
| 재현 | master seed 입력(빈칸=랜덤) · webp 업로드로 config·시드 복구 |
| 고급 | AuraFlow shift · DCWModelPatch 파라미터 전체 |

생성 중 진행률 바와 중간/최종 이미지가 도착하는 대로 표시되고, 그 위에 실제 해석된 프롬프트·LoRA·master seed가 표시된다. ComfyUI가 라이브 프리뷰 프레임을 보내면 프리뷰 카드도 함께 뜬다.

**구성 저장** 버튼(또는 `Ctrl/Cmd+S`)은 현재 폼 값 전체(와일드카드 블록·LoRA·저장 설정 포함)를 선택된 구성(`configs/<id>.json`)에 덮어써 다음 접속 시 복원한다. 구성 추가·전환은 좌측 **구성 관리** 패널에서 한다.

## 코드 구조

```
main.py        # uvicorn 런처
server.py      # FastAPI: UI 서빙 + /api/* + /ws/generate + /api/afk/*
comfy.py       # ComfyUI 클라이언트 (옵션 조회 / 큐잉 / 웹소켓 이벤트 스트림)
prompt.py      # 와일드카드 해석 엔진 (Base + 블록 → 최종 프롬프트 + 매칭 LoRA)
workflow.py    # (해석된 프롬프트 + LoRA + RNG) → ComfyUI 그래프 빌더
pipeline.py    # master seed 하나 → 와일드카드+시드 일괄 해석 (재현 단위)
embed.py       # webp RIFF 청크에 gz(config+seed) 임베드/추출
repro.py       # webp에서 복구해 재생성하는 CLI
storage.py     # webp 인코딩 + 경로 템플릿(+메타 임베드) → 디스크 저장
configs.py     # 다중 named-config 저장소 (CRUD + 선택 상태)
models.py      # Pydantic 스키마 (설정의 단일 진실 공급원)
configs/       # 저장된 구성들 (<id>.json + _state.json) — 런타임 생성
config.json    # 레거시 단일 설정 (첫 실행 시 configs/default 로 이관)
static/
  index.html   # 마크업
  style.css    # 스타일 (+ 모바일 미디어쿼리)
  main.js      # 폼 로직 + 웹소켓(생성·AFK) + 구성 관리 + 재현 + localStorage
api_prompt_template.json  # 원본 ComfyUI 워크플로우 (참조용, 런타임 미사용)
```

### `models.py` — 타입 스키마

설정의 형태를 정의하는 **단일 진실 공급원**. UI·API·빌더가 모두 이 모델을 공유한다.

- `GenerationConfig` 아래로 `PromptSpec`(`WildcardBlock` 리스트) / `ModelsConfig` / `SizeConfig` / `LoraConfig` / `Stage1Config` / `Stage2Config` / `UpscaleConfig` / `AdvancedConfig` / `SaveConfig`
- 강한 검증: `extra="forbid"`(미지의 키 거부), 범위 제약(`steps≥1`, `width 64~8192`, `denoise 0~1`, `webp_quality 1~100` …), `Literal` enum(`mode`, `add_noise` 등)
- `LoraConfig.matches(tokens)` / `GenerationConfig.matched_loras(final_positive)` — 최종 프롬프트 토큰 기준의 상시/조건부 LoRA 판정을 모델에 캡슐화

### `prompt.py` — 와일드카드 해석 엔진

`resolve(cfg, rng) -> (final_positive, matched_loras)`. Base 프롬프트를 토큰화한 뒤 블록을 차례로 적용한다: 후보 줄을 파싱(`|n|` 가중치 / `#` 주석 / `NOPROMPT`)하고 가중 랜덤으로 한 줄을 골라, input 토큰을 consume하고 첫 등장 위치에 삽입한다. 그 결과 최종 프롬프트로부터 `cfg.matched_loras()`가 켜질 LoRA를 정한다. `rng`를 주입받으므로 호출마다(=AFK 매 장마다) 다시 굴려진다.

### `workflow.py` — 그래프 빌더

`build_workflow(cfg, positive, loras, rng) -> (graph, output_labels, info)`.
이미 해석된 `positive` 문자열과 그 프롬프트가 켠 `loras`를 받아 ComfyUI `/prompt` 그래프를 만든다. LoRA를 순서대로 체인에 끼우고 강도를 직접 박는다. seed `-1`은 주입된 `rng`(와일드카드를 굴린 바로 그 RNG)에서 뽑혀 한 master seed로 전체가 재현되며, 그 값은 `info`(`BuildInfo`: `seed1`/`seed2`/`positive`/`loras`)로 돌려줘 저장·UI 표시에 쓰인다. 노드 링크는 `Link = list[str | int]`(`["node_id", output_index]`)로 표현.

`output_labels`는 두 `SaveImageWebsocket` 노드를 `intermediate`/`final`로 매핑해, 클라이언트가 들어오는 이미지 프레임을 구분하게 한다.

### `pipeline.py` · `embed.py` · `repro.py` — 재현

`pipeline.prepare(cfg, master_seed)`가 `random.Random(master_seed)` 하나로 `prompt.resolve`(와일드카드) → `build_workflow`(시드)를 차례로 돌려 `(graph, labels, info)`를 만든다. `embed.embed_metadata/extract`는 webp RIFF 컨테이너에 `cMTA` 청크로 gz(JSON) 메타를 덧붙이고 되읽으며 `(GenerationConfig, master_seed)`를 복구한다. `repro.reproduce(webp_bytes)`가 둘을 묶어 webp만으로 재생성한다.

### `storage.py` — 저장

`encode_webp(png, quality, lossless)`로 Pillow가 PNG 바이트를 webp로 재인코딩하고, `render_path(save_cfg, seed, index, now)`로 경로 템플릿을 실제 경로로 만든다(`{cname}`는 `save_cfg.cname`에서, 미지의 placeholder는 `ValueError`로 즉시 실패). `save_image()`가 둘을 묶어 디스크에 쓴다.

### `configs.py` — named-config 저장소

여러 설정 묶음을 `configs/<id>.json`(메타 + `GenerationConfig`)으로 저장하고 `_state.json`에 선택 id를 둔다. `list_metas` / `get` / `create` / `update`(이름·config 선택적) / `duplicate` / `delete` / `get_selected` / `set_selected`, 그리고 첫 실행 시 레거시 `config.json`을 `default`로 옮기는 `ensure_seeded`를 제공한다. `StoredConfig`(= `ConfigMeta` + `config`)로 검증한다.

### `comfy.py` — ComfyUI 클라이언트

- `get_options() -> ComfyOptions` — `/object_info`에서 설치된 LoRA·모델·샘플러·스케줄러 목록을 긁어 드롭다운 채움
- `run(workflow, labels, on_event)` — `/prompt`로 큐잉 후 ComfyUI 웹소켓을 구동하며 이벤트를 콜백으로 흘려보냄(대화형·AFK 모두 사용)
- 이벤트는 `type`으로 구분되는 **TypedDict 태그 유니온**(`Event`): `queued` / `node` / `progress` / `image` / `preview` / `error` / `done`. 이미지 프레임은 앞 8바이트(헤더)를 떼고 raw PNG 바이트를 실어 보낸다.

### `server.py` — FastAPI 백엔드

| 엔드포인트 | 역할 |
|-----------|------|
| `GET /` | UI(`index.html`) 서빙, `static/`는 `/`에 마운트 |
| `GET /api/options` | 드롭다운 옵션(모델·LoRA·샘플러) + ComfyUI 주소 |
| `GET /api/configs` | 구성 메타 목록 + 선택 id + 선택 구성 전체 |
| `POST /api/configs` | 새 구성 생성(`{name, config}`) 후 선택 |
| `GET·PUT·DELETE /api/configs/{id}` | 구성 조회 / 수정(이름·config 선택적) / 삭제 |
| `POST /api/configs/{id}/duplicate` · `/select` | 복제 / 선택 변경 |
| `POST /api/reproduce` | 업로드한 webp에서 임베드된 `config`+`master_seed` 복구 |
| `WS /ws/generate` | 설정(+선택적 `master_seed`) 수신 → 해석 → 빌드 → 생성, `resolved`/진행률/이미지/`saved`를 중계 |
| `POST /api/afk/start` | AFK 백그라운드 루프 시작 (본문 `GenerationConfig`) |
| `POST /api/afk/stop` | AFK 루프 정지 신호 |
| `GET /api/afk/status` | 동작 여부·생성 수·마지막 경로/시드/에러 |
| `GET /api/afk/last.webp` | 가장 최근 저장된 이미지(확정 webp 썸네일) |
| `WS /ws/afk` | AFK 루프의 라이브 이벤트(resolved/progress/이미지/saved/afk 상태) 중계 |

웹소켓 핸들러는 프롬프트를 서버에서 해석해 `resolved` 프레임을 먼저 보내고, 동기 클라이언트(`comfy.run`)를 `asyncio.to_thread`로 돌려 스레드 이벤트를 `asyncio.Queue[Event]`로 받아 전달한다. 이미지/프리뷰는 작은 JSON 헤더 프레임 뒤에 바이너리 프레임으로 보낸다. 저장이 켜져 있으면 최종 이미지를 webp로 저장하고 `saved` 프레임을 보낸다. 잘못된 설정은 `ValidationError`를 잡아 `{type:"error", data.errors}` 프레임으로 돌려준다.

`AfkManager`는 단일 백그라운드 `asyncio.Task`로, 매 반복마다 새 master seed로 `pipeline.prepare` → `comfy.run`(별도 스레드 스트리밍) → `storage.save_image`(config·seed 메타 임베드)를 수행한다. 정지 신호나 목표 개수까지 돌며, 연속 오류가 누적되면 멈춘다. 시작은 이벤트 루프 안에서 태스크를 만들어야 하므로 엔드포인트가 `async`다.

**라이브 뷰** — `AfkManager`는 구독자(`/ws/afk`로 접속한 브라우저)들의 큐 집합을 들고, 루프가 내는 모든 이벤트(`resolved`/`node`/`progress`/`image`/`saved`와 상태 `afk`)를 브로드캐스트한다. ComfyUI 콜백은 워커 스레드에서 오므로 `loop.call_soon_threadsafe`로 이벤트 루프에 넘겨 fan-out한다. 덕분에 AFK 동작 중에도 브라우저가 대화형 생성과 **똑같이** 진행 중인 중간/최종 이미지·해석된 프롬프트·진행률을 실시간으로 본다.

### `static/` — 프론트엔드

빌드 스텝 없는 바닐라 JS. `main.js`가 `/api/options`로 폼을 채우고(와일드카드 블록·LoRA 행을 동적 생성), **생성** 시 `/ws/generate`에 설정을 보낸 뒤 텍스트(메타)·바이너리(이미지) 프레임을 짝지어 화면에 그린다.

- **구성 관리** — 좌측 최상단 패널이 `/api/configs`로 목록을 받아 정렬·선택·추가·복제·이름변경·삭제를 처리하고, 선택 시 그 구성을 폼에 로드한다. `Ctrl/Cmd+S`로 현재 폼을 선택 구성에 저장.
- **재현** — 재현 패널에서 master seed를 직접 입력하거나 webp를 올려(`/api/reproduce`) config·시드를 복구해 재생성한다.
- **AFK 라이브 뷰** — **AFK 시작** 시 `/api/afk/start`로 설정을 던지고 `/ws/afk`를 열어 대화형과 같은 카드(중간/최종 이미지·진행률·해석 프롬프트)에 실시간 렌더한다. `/api/afk/status` 폴링은 버튼 상태 백업으로 병행한다.
- **편집 편의** — 와일드카드 블록 ▲▼ 순서변경·줄바꿈(wrap) 토글, 접이식 패널(`<details class="panel">`).
- **뷰 상태 영속** — 패널 접힘 상태, 와일드카드 줄바꿈 기본값, 멀티라인 박스 높이를 `localStorage`에 저장해 새로고침해도 유지한다(`ResizeObserver`로 높이 추적).
- **모바일** — `style.css`의 미디어쿼리가 좁은 화면에서 단일 컬럼·큰 터치 타깃으로 리플로우한다.

## 개발

타입 체크(Pylance와 동일한 pyright 엔진):

```bash
uv run pyright          # 0 errors 기대
```

`pyrightconfig.json`이 `.venv`와 대상 파일을 지정한다. 타입 흐름은 **UI JSON → `GenerationConfig`(검증) → `build_workflow` → ComfyUI 그래프 → `Event` 스트림 → 브라우저**까지 전 구간 타입화돼 있다.


> 참고: 이 저장소가 놓인 일부 환경에서는 pyright 번들 node가 `libatomic.so.1`을 찾지 못한다. 그럴 땐 `LD_LIBRARY_PATH=/path/to/conda/lib uv run pyright`처럼 실행한다. IDE의 Pylance는 자체 엔진이라 영향받지 않는다.
