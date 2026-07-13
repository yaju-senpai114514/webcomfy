# webcomfy

**다중 원격 ComfyUI 서버를 오케스트레이션하는 2단계(base → hires) 이미지 생성 플랫폼.**

설정값으로부터 ComfyUI 워크플로우 그래프를 매번 동적으로 조립하고, 등록된 서버 fleet에 생성을 분산하며, LoRA·체크포인트를 로컬 저장소에서 각 서버로 투명하게 공급한다.

- **다중 서버 오케스트레이션** — 원격 ComfyUI 서버를 여러 대 등록하고, 대화형 생성은 서버를 골라, AFK는 체크한 서버 전체에 분산해서 돌린다. 서버별 헬스체크(큐 깊이·모델 API 유무) 포함.
- **로컬 모델 저장소 + 투명 프로비저닝** — webcomfy 백엔드에 모델 저장소(`local_models/`)를 두고, 생성 시 대상 서버에 없는 모델을 **자동으로 전송한 뒤** 생성한다. 드롭다운에는 로컬 전용 파일도 `(로컬→자동전송)`으로 표시되어 그대로 선택 가능.
- **원격 모델 관리** — `/models` 페이지에서 로컬 저장소와 각 서버의 모델을 조회/업로드/다운로드/삭제/해시하고, diff 뷰 + 복사로 fleet을 동기화한다.
- **와일드카드 프롬프트** — Base prompt + 순차 치환 블록(재귀 트리)으로 매 생성마다 프롬프트를 랜덤하게 굴린다. 정적 검증(린트) 포함.
- **조건부 LoRA** — 와일드카드가 해석된 *최종 프롬프트*의 토큰을 보고 서버가 LoRA를 켠다.
- **다중 구성(Config) 관리** — 설정 묶음을 저장·전환·복제하고 AFK·재현에 재사용한다.
- **AFK 백그라운드 모드** — 정지할 때까지 생성을 반복하며 webp로 저장하고, 서버별 라이브 카드로 실시간 표시한다.
- **재현성** — 단일 master seed가 와일드카드+샘플러 시드를 모두 결정하고, webp에 config·seed를 임베드해 한 장을 그대로 복구한다.
- **모바일 대응** — 좁은 화면에서 단일 컬럼으로 리플로우.

```
┌─────────┐   JSON    ┌────────────┐  validate  ┌──────────┐  graph   ┌─────────┐
│ 브라우저 │ ────────▶ │ web/server │ ─────────▶ │ workflow │ ───┬───▶ │ ComfyUI │←ⓜ
│ (form)  │  WebSocket │ (FastAPI)  │  pydantic  │  builder │    ├───▶ │ ComfyUI │←ⓜ
└─────────┘ ◀──────── └────────────┘ ◀──────────────────────    └───▶ │ ComfyUI │←ⓜ
            진행률·이미지     ↑ store/servers 레지스트리               └─────────┘
                             └ 모델 프록시·프로비저닝(local_models/ ↔ ⓜ)
                                          ⓜ = /webcomfy/models API (커스텀 노드)
```

## 빠른 시작

```bash
# 의존성 설치 (uv 사용)
uv sync

# 첫 서버 주소 설정 (.env — 첫 실행 시 servers.json의 'default' 서버로 이관됨)
echo 'COMFY_BASE_URL="http://localhost:58188"' > .env
echo 'WEBCOMFY_MODELS_TOKEN="..."' >> .env    # 그 서버의 모델 API 토큰 (없으면 생략)

# 서버 실행 (HOST/PORT도 .env에서 읽음)
uv run python main.py          # → UI :8000 · read-only 뷰어 :8001
```

추가 서버는 UI의 **ComfyUI 서버** 패널에서 등록한다. 원격 모델 관리·투명 프로비저닝을 쓰려면 각 ComfyUI 서버에 아래 [원격 ComfyUI 모델 API](#원격-comfyui-모델-api)를 구현한 라우트 전용 커스텀 노드가 설치되어 있어야 한다(생성 자체는 노드 없이도 동작).

`.env` 항목: `COMFY_BASE_URL` / `WEBCOMFY_MODELS_TOKEN`(첫 실행 시딩용) · `HOST` / `PORT` · `LOCAL_MODELS_DIR`(기본 `local_models/`) · `OUTPUT_DIR`(뷰어, 기본 `output/`).

## 생성 파이프라인

config로부터 매번 조립되는 2단계 샘플링 그래프:

```
EmptyLatentImage (config의 width×height)
  └─ KSampler (1단계: steps/cfg/sampler/scheduler/denoise)
       └─ VAEDecode ──▶ ★ 중간 이미지 (base)
            └─ ImageUpscaleWithModel (예: RealESRGAN x4)
                 └─ ImageScaleBy scale_by
                      └─ VAEEncode  (픽셀 → latent 재인코딩)
                           └─ KSamplerAdvanced (2단계 hires: start_at_step~end_at_step)
                                └─ VAEDecode ──▶ ★ 최종 이미지 (hires)
```

모델 조립 체인: `UNET → [활성 LoRA들] → ModelSamplingAuraFlow(shift) → DCWModelPatch → (두 샘플러 공유)`. CLIP / VAE는 별도 로더에서 공급된다. 모든 파라미터(스텝·cfg·샘플러·업스케일러·고급 패치값)는 웹 폼에서 제어한다.

## 와일드카드 프롬프트

Positive 프롬프트는 단일 텍스트가 아니라 **Base prompt + 순차 치환 블록** 구조다. 생성 시점(서버)에 매번 새로 해석되므로, AFK 루프를 돌리면 매 장마다 다른 프롬프트가 나온다.

각 블록은 **input**(콤마 구분 트리거 토큰)과 **후보 목록**으로 이뤄진다. 현재 프롬프트에 input 토큰이 있으면 *모두 consume(제거)*되고, 가중 랜덤으로 뽑힌 후보 한 줄이 **첫 등장 위치**에 삽입된다(없으면 no-op). 블록에는 **하위 블록(재귀 트리)**을 달 수 있어, 뽑힌 후보가 도입한 `__토큰__`을 하위 블록이 이어서 굴린다.

후보는 표 편집(활성 체크 · 가중치 · 텍스트) 또는 텍스트 일괄 편집으로 다룬다. 텍스트 모드 한 줄 문법:

| 형태 | 의미 |
|------|------|
| `text` | 일반 치환(콤마 포함 가능, 여러 토큰으로 삽입됨) |
| `\|n\| text` | 가중치 `n`(0 이상 실수, 기본 1). `\|0\|`이면 선택 안 됨 |
| `# ...` | 비활성(주석) |
| `NOPROMPT` | input 토큰을 consume만 하고 아무것도 삽입하지 않음 |

치환되지 않고 남을 수 있는 `__토큰__`이나 dead branch는 정적 분석(**검증** 버튼, 저장 시 자동)이 잡아낸다 — 의미론과 린트 규칙은 `WILDCARD_SEMANTICS.md` 참고.

## 조건부 LoRA (최종 프롬프트 기반)

와일드카드가 모두 해석된 **최종 프롬프트**를 기준으로 서버가 LoRA 적용 여부를 정한다:

- **always** — 토큰과 무관하게 항상 설정 강도로 적용.
- **conditional** — 설정한 **트리거 토큰**이 최종 프롬프트의 콤마 토큰과 *정확히 일치*할 때만 적용(빈 트리거면 비활성).

예: 캐릭터 블록에서 `@m00m00`이 뽑히면 `@m00m00` 트리거를 가진 LoRA가 그 생성에만 켜진다. 생성 직후 UI에 실제 해석된 프롬프트와 적용된 LoRA 목록이 표시된다.

## 다중 서버 오케스트레이션

원격 ComfyUI 서버들을 `servers.json` 레지스트리(id·이름·base_url·모델 API 토큰·활성 여부)로 관리한다. 첫 실행 시 `.env`의 `COMFY_BASE_URL` / `WEBCOMFY_MODELS_TOKEN`이 `default` 서버로 자동 이관된다.

- **서버 패널** — 서버 추가/수정/삭제/활성 토글. 행마다 헬스 점(● 초록 = `/queue` 응답 OK, 툴팁에 큐 깊이)과 **M** 배지(모델 관리 API 탑재)를 표시.
- **대화형 생성** — 헤더의 서버 드롭다운으로 생성 대상 서버를 고른다. 모델/LoRA/샘플러 드롭다운 옵션도 그 서버의 `/object_info`로 다시 채워진다.
- **AFK 분산** — 저장/AFK 패널에서 체크한 서버들이 **동시에** 생성을 돌린다. 각 서버 워커는 시작 전에 config가 요구하는 UNET·CLIP·VAE·업스케일러·LoRA가 그 서버에 있는지 검사하고(pre-flight), 없으면 로컬 저장소에서 자동 전송한다. 서버에도 로컬에도 없을 때만 그 서버가 명확한 에러로 빠진다. 목표 개수(`afk_count`)는 전역 인덱스 클레임으로 서버들이 나눠 갖고, 실패한 슬롯은 다른 서버가 재사용한다.
- **서버별 라이브 카드** — AFK 동작 중 서버마다 진행률·해석 프롬프트·중간/최종 이미지를 가진 카드가 출력 영역에 떠서 fleet 전체를 한눈에 본다.

## 로컬 모델 저장소와 투명 프로비저닝

webcomfy 백엔드 호스트에 모델 저장소를 둔다 (`LOCAL_MODELS_DIR`, 기본 `local_models/` — 카테고리별 서브폴더 `loras/`, `diffusion_models/`, `text_encoders/`, `vae/`, `checkpoints/`, …). 여기가 fleet의 스테이징 그라운드다:

- **투명 프로비저닝** — 생성(대화형·AFK 모두) 직전에 대상 서버의 `/object_info`와 config를 대조해서, 없는 모델이 로컬 저장소에 있으면 그 서버로 **자동 업로드**(sha256 검증, 64 MiB마다 진행률 이벤트)한 뒤 생성을 진행한다. 서버에도 로컬에도 없을 때만 에러.
- **드롭다운 통합** — UNET/CLIP/VAE/업스케일러/LoRA 선택창에 서버 설치본과 로컬 전용본이 함께 뜬다. 로컬 전용은 `(로컬→자동전송)` 표시가 붙고, 선택해서 생성하면 위 프로비저닝이 알아서 처리한다.
- **관리·동기화** — `/models` 페이지에서 "로컬 저장소 (webcomfy)"가 서버 목록의 첫 항목으로 떠서, 원격 서버와 똑같이 조회/업로드/삭제/해시하고 diff·복사로 양방향 동기화(로컬→서버 push, 서버→로컬 pull)한다. 파일을 디렉토리에 직접 넣어도(scp 등) 그대로 인식된다(해시는 지연 계산).

## 모델 관리 페이지 (/models)

각 ComfyUI의 모델 API를 webcomfy가 프록시해서, 브라우저에서 서버별 모델 파일을 직접 관리한다. 토큰·CORS는 webcomfy 서버 쪽에서 처리되므로 브라우저는 ComfyUI에 직접 붙지 않는다.

- **조회** — 카테고리(`loras`, `checkpoints`, `vae`, … — ComfyUI `folder_paths`에서 동적으로)별 파일 목록: 크기·수정시각·sha256·업로드 시각. 이름 필터.
- **업로드** — 파일 + 저장 이름(서브폴더 허용) + 덮어쓰기 토글. XHR 진행률 표시, 서버가 수신하며 sha256을 계산해 응답. 동명 덮어쓰기 시 ComfyUI의 로더 캐시 때문에 재시작 전까지 이전 가중치가 쓰일 수 있다는 경고(`stale_cache`)를 그대로 표시.
- **다운로드 / 삭제 / 해시** — 다운로드는 Range 지원 스트리밍 프록시. 대역 외로 들어온 파일은 "해시 계산" 버튼으로 백그라운드 sha256 계산(서버당 동시 1건).
- **복사 / 동기화** — 헤더에서 "비교/복사 대상"을 고르면(다른 서버 또는 로컬 저장소) 파일마다 **동일/다름/없음** diff 배지가 붙고, 개별 "복사 →" 또는 "누락분 모두 복사"로 webcomfy를 경유해 스트리밍 전송한다(소스에 sha256이 있으면 전송 후 무결성 검증). 진행 상황은 복사 작업 패널에서 실시간 폴링.

## 원격 ComfyUI 모델 API

webcomfy가 소비하는, **각 ComfyUI 서버에 설치되는 라우트 전용 커스텀 노드**의 HTTP API. 노드 클래스 없이 `PromptServer` 라우트만 등록하며, ComfyUI의 모델 디렉토리를 원격에서 조회/업로드/다운로드/삭제하고 파일별 메타데이터(sha256, 업로드 시각)를 관리한다. 전체 사양은 **`MODEL_API_SPEC.md`(v1)**, 아래는 요약:

- **URL 프리픽스** `/webcomfy/models` (ComfyUI 내장 `/models`와 충돌 방지). **category**는 `folder_paths` 키(`loras`, `checkpoints`, `diffusion_models`, `vae`, …)를 동적으로 따르고, **name**은 category 기준 상대 경로(서브폴더 허용, 구분자 `/`).
- **인증** — ComfyUI 프로세스에 `WEBCOMFY_MODELS_TOKEN`이 설정되어 있으면 모든 요청에 `Authorization: Bearer <token>`을 요구. 미설정 = 무인증(신뢰 네트워크 전제). webcomfy에는 서버 등록 시 같은 토큰을 넣는다.
- **보안** — `name`은 realpath confinement(베이스 디렉토리 탈출 시 400), 확장자 화이트리스트(`.safetensors` `.ckpt` `.pt` `.pth` `.bin` `.gguf` `.onnx` `.yaml` 등), 숨김 세그먼트 거부.

| 메서드 · 경로 | 역할 |
|---|---|
| `GET /webcomfy/models` | 카테고리 요약 (`{category, file_count, total_size}[]`) |
| `GET /webcomfy/models/{category}` | 파일 목록(재귀) — ComfyUI 드롭다운과 1:1 |
| `GET /webcomfy/models/{category}/{name}` | 다운로드 (Range 지원) · `?meta=1`이면 메타 JSON |
| `POST /webcomfy/models/{category}/{name}` | raw 바이너리 스트리밍 업로드 · `?replace=1` 덮어쓰기 · `?sha256=` 기대 해시(불일치 시 422, 파일 미생성) |
| `DELETE /webcomfy/models/{category}/{name}` | 삭제 (204) |
| `POST /webcomfy/models/{category}/{name}/hash` | sha256 (재)계산 트리거 — 202 후 `?meta=1` 폴링 · `?force=1` 강제 |

파일 메타 객체: `{name, category, size, mtime, sha256, uploaded_at, hash_state}` — `sha256`/`uploaded_at`은 이 API로 업로드됐거나 해시 계산이 끝난 파일만 채워지고, 대역 외 변경이 감지되면 스테일 처리되어 `null`로 내려간다(`hash_state`: `done`/`pending`/`none`). 오류는 전부 `{"error": {"code", "message"}}` 형식(`invalid_name`/`invalid_extension` 400, `unauthorized` 401, `unknown_category`/`not_found` 404, `already_exists` 409, `hash_mismatch` 422).

업로드는 대상과 같은 파일시스템의 숨김 임시 디렉토리에 스트리밍 후 `os.replace()`로 원자 반영되며, 중단 시 잔여물이 남지 않는다. 업로드 직후 ComfyUI `/object_info`에 재시작 없이 반영된다(`folder_paths` 캐시는 디렉토리 mtime 기반).

## 저장 / AFK

생성 결과를 PNG 원본 대신 **webp**로 압축해 디스크에 저장한다.

- **경로 템플릿** — `저장 폴더`(기본 `output`) 하위에 템플릿으로 파일명을 만든다. placeholder: `{date}`(YYYY-MM-DD) · `{time}`(HHMMSS) · `{datetime}` · `{seed}` · `{index}`(AFK 전역 순번) · `{cname}`(현재 구성 이름) · `{ext}`. 예: `{cname}/{date}-{time}.{ext}`.
- **webp 품질 / 무손실** — `webp_quality`(1–100), `webp_lossless` 토글.
- **대화형 저장** — `대화형 생성도 저장`을 켜면 단발 생성 결과도 저장된다.
- **AFK 모드** — `AFK 시작`을 누르면 체크된 서버들이 백그라운드에서 매번 와일드카드/시드를 다시 굴려 생성→저장을 반복한다. `AFK 생성 개수`가 0이면 `정지`를 누를 때까지 무한, N이면 fleet 전체가 합쳐서 N장 후 자동 종료. 브라우저를 닫아도 서버 프로세스가 살아 있으면 계속 동작한다.

## 구성(Config) 관리

설정 묶음을 여러 개 저장해두고 전환할 수 있다. 각 구성은 이름·생성일·수정일과 전체 `GenerationConfig`를 가지며, `configs/<id>.json` 파일 하나로 저장된다.

- **목록/선택** — 구성을 이름·생성일·수정일로 정렬해 보여주고, 행을 누르면 그 구성을 폼에 불러온다.
- **추가/복제/이름변경/삭제** — 현재 폼으로 새 구성을 만들거나, 선택 구성을 복제·개명·삭제한다.
- **구성 저장**(헤더 버튼 또는 `Ctrl/Cmd+S`) — 현재 폼 값 전체를 선택된 구성에 덮어쓴다(수정일 갱신, 자동 린트).

선택된 구성 이름은 저장 경로의 `{cname}` placeholder로 쓰여, 구성별로 폴더를 나눠 저장하기 좋다.

## 재현성 (Reproducibility)

생성 한 장은 **단일 master seed + config**로 완전히 재현된다.

- **단일 RNG** — `random.Random(master_seed)` 하나가 고정된 순서로 ① 모든 와일드카드 선택 → ② `-1`(랜덤) 샘플러 시드(stage1·stage2)를 굴린다. 같은 시드+config면 프롬프트도 시드도 동일하다. master seed는 브라우저(JSON 숫자 = float64)를 무손실로 오가도록 `2^53-1` 이하로 둔다.
- **임베딩** — 저장 시 최종 webp에 `gzip(JSON{master_seed, config, resolved})`를 커스텀 RIFF 청크(`cMTA`)로 덧붙인다. WebP는 RIFF 컨테이너라 미지 청크는 디코더가 무시 → 어디서나 정상 표시되면서 재현 정보를 품는다.
- **복구** — 재현 패널에 그 webp를 올리면(`POST /api/reproduce`) config가 폼에 로드되고 master seed가 채워진다. `생성 ▶`을 누르면 그 시드로 재현된다. 빈 시드는 매번 새 랜덤.
- **오프라인 재현** — `uv run python repro.py <image.webp>` 가 webp에서 메타를 읽어 기본 서버로 다시 생성한다.

> 실험으로 검증: 한 장 생성 → webp 임베드 → webp에서 `(config, seed)` 복구 → 재생성 했을 때, 최종 PNG가 **바이트 단위로 동일**했다(ComfyUI 샘플링이 고정 시드에서 결정적이라 가능).

## 코드 구조

```
main.py            # uvicorn 런처 (UI + 뷰어 듀얼 서버) — src/를 import path에 추가
repro.py           # webp에서 복구해 재생성하는 CLI
src/
  paths.py         # ROOT_DIR/STATIC_DIR — 런타임 파일은 전부 레포 루트 기준
  gen/             # 생성 도메인: config → 프롬프트 해석 → ComfyUI 그래프
    models.py      #   Pydantic 스키마 (설정의 단일 진실 공급원)
    prompt.py      #   와일드카드 해석 엔진 (Base + 블록 → 최종 프롬프트 + 매칭 LoRA)
    analyze.py     #   와일드카드 트리 정적 검증 (dead branch / 치환 불가 토큰)
    workflow.py    #   (해석된 프롬프트 + LoRA + RNG) → ComfyUI 그래프 빌더
    pipeline.py    #   master seed 하나 → 와일드카드+시드 일괄 해석 (재현 단위)
  comfy/           # 원격 ComfyUI 통신
    client.py      #   ComfyClient — 옵션/큐잉/웹소켓 이벤트 스트림/큐 상태
    modelapi.py    #   /webcomfy/models API 비동기 클라이언트 (httpx 스트리밍)
  store/           # 영속성
    configs.py     #   다중 named-config 저장소 (CRUD + 선택 상태)
    servers.py     #   원격 서버 레지스트리 (servers.json, .env에서 시딩)
    localstore.py  #   로컬 모델 저장소 (프로비저닝 소스 + /models의 "local" 백엔드)
    storage.py     #   webp 인코딩 + 경로 템플릿(+메타 임베드) → 디스크 저장
    embed.py       #   webp RIFF 청크에 gz(config+seed) 임베드/추출
  web/             # HTTP 계층 (FastAPI 앱)
    server.py      #   UI 서빙 + /api/* + /ws/* + 모델 프록시 + AFK 오케스트레이터
    viewer.py      #   read-only 갤러리/구성 뷰어 (PORT+1)
configs/           # 저장된 구성들 (<id>.json + _state.json) — 런타임 생성
servers.json       # 서버 레지스트리 (토큰 포함 — gitignore) — 런타임 생성
local_models/      # 로컬 모델 저장소 (카테고리별 서브폴더 — gitignore) — 런타임 생성
output/            # 생성 결과 webp (gitignore) — 런타임 생성
static/
  index.html       # 생성 UI 마크업 (단일 컬럼 + 플로팅 미리보기)
  style.css / style_v2.css              # 스타일 (+ 모바일 미디어쿼리)
  main.js          # 폼 로직 + 웹소켓(생성·AFK) + 서버/구성 관리 + 재현 + localStorage
  models.html / models.js / models.css   # 모델 관리 페이지 (/models)
  viewer.html / viewer.js / viewer.css   # read-only 뷰어 (PORT+1)
```

### `gen/` — 생성 도메인

- **`models.py`** — 설정의 형태를 정의하는 단일 진실 공급원. `GenerationConfig` 아래로 `PromptSpec`(`WildcardBlock` 트리) / `ModelsConfig` / `SizeConfig` / `LoraConfig` / `Stage1Config` / `Stage2Config` / `UpscaleConfig` / `AdvancedConfig` / `SaveConfig`. `extra="forbid"` + 범위 제약 + `Literal` enum으로 강하게 검증하고, 상시/조건부 LoRA 판정(`matched_loras`)을 모델에 캡슐화한다.
- **`prompt.py`** — `resolve(cfg, rng) -> (final_positive, matched_loras)`. Base 프롬프트를 토큰화한 뒤 블록 트리를 차례로 적용: 가중 랜덤으로 후보를 골라 input 토큰을 consume하고 첫 등장 위치에 삽입, 하위 블록을 재귀 적용. `rng` 주입식이라 호출마다(=AFK 매 장마다) 다시 굴려진다.
- **`workflow.py`** — `build_workflow(cfg, positive, loras, rng) -> (graph, output_labels, info)`. 해석된 프롬프트와 켜진 LoRA로 ComfyUI `/prompt` 그래프를 조립한다. seed `-1`은 주입된 `rng`에서 뽑혀 master seed 하나로 전체가 재현되고, 실제 시드·프롬프트는 `BuildInfo`로 돌려준다. `output_labels`가 두 출력 노드를 `intermediate`/`final`로 매핑해 클라이언트가 이미지 프레임을 구분한다.
- **`pipeline.py`** — `prepare(cfg, master_seed)`: `random.Random(master_seed)` 하나로 `prompt.resolve` → `build_workflow`를 고정 순서로 돌리는 재현 단위.

### `comfy/` — 원격 ComfyUI 통신

- **`client.py`** — `ComfyClient(base_url)`, 서버 1대용 동기 클라이언트. `get_options()`(`/object_info` → 드롭다운), `get_queue()`(헬스체크), `run(workflow, labels, on_event)`(`/prompt` 큐잉 + 웹소켓 구동). 이벤트는 `type` 태그 유니온(`queued`/`node`/`progress`/`image`/`preview`/`error`/`done`).
- **`modelapi.py`** — 위 [모델 API](#원격-comfyui-모델-api)의 httpx 비동기 소비자: `summary`/`list_files`/`file_meta`/`download`(스트리밍, Range 전달)/`upload`(async 이터레이터 본문)/`delete`/`trigger_hash`. 업스트림 오류는 `ModelAPIError({code, message})`로 번역되어 FastAPI 핸들러가 그대로 중계한다. 타임아웃은 connect/first-byte에만 걸고 본문 스트리밍에는 걸지 않는다(수 GB 파일).

### `store/` — 영속성

- **`configs.py`** — `configs/<id>.json`(메타 + `GenerationConfig`) CRUD + `_state.json` 선택 상태.
- **`servers.py`** — `servers.json`에 `ServerEntry`(id/name/base_url/token/enabled) 목록을 원자적으로 저장. 첫 실행 시 `.env`를 `default` 서버로 시딩. `default_server()` = 첫 활성 서버.
- **`localstore.py`** — 로컬 모델 저장소. 원격 모델 API와 같은 응답 형태·오류 코드·인덱스 시맨틱(sha256 인덱스, 스테일 감지, 단일 해시 워커, 원자적 업로드)을 구현해서, 프록시·복사·UI가 "local"을 서버 하나처럼 다룬다.
- **`storage.py`** — webp 인코딩(Pillow) + 경로 템플릿 렌더 + 메타 임베드 → 디스크 저장.
- **`embed.py`** — webp RIFF 컨테이너에 `cMTA` 청크로 gz(JSON{config, master_seed, resolved})를 임베드/추출.

### `web/server.py` — FastAPI 백엔드

| 엔드포인트 | 역할 |
|-----------|------|
| `GET /` · `/models` | UI 서빙, `static/`는 `/`에 마운트 |
| `GET·POST /api/servers` · `PUT·DELETE /api/servers/{id}` | 서버 레지스트리 CRUD |
| `GET /api/servers/{id}/health` | 헬스체크: `/queue` 응답 + 큐 깊이 + 모델 API 유무 |
| `GET /api/options?server_id=` | 해당 서버의 드롭다운 옵션 + 로컬 저장소 목록(`local`) |
| `GET·POST /api/configs` · `GET·PUT·DELETE /api/configs/{id}` | 구성 CRUD |
| `POST /api/configs/{id}/duplicate` · `/select` | 복제 / 선택 변경 |
| `POST /api/analyze` | 와일드카드 트리 정적 검증 |
| `POST /api/reproduce` | 업로드한 webp에서 임베드된 `config`+`master_seed` 복구 |
| `GET /api/models/{sid}` · `/{sid}/{category}` | 모델 API 프록시: 카테고리 요약 / 파일 목록 (`sid`가 `local`이면 로컬 저장소) |
| `GET·POST·DELETE /api/models/{sid}/{category}/{name}` | 다운로드(`?meta=1`=메타, Range 지원) / 스트리밍 업로드(`?replace`·`?sha256`) / 삭제 |
| `POST /api/models/{sid}/{category}/{name}/hash` | sha256 (재)계산 트리거 (`?force=1`) |
| `POST /api/models/copy` | 복사 잡 시작(`{src_id, dst_id, category, name, replace}` — 양쪽 모두 `local` 가능, 단 동일 금지) |
| `GET /api/model_jobs[/{id}]` | 복사 잡 진행 상황 (bytes_done/total/state) |
| `WS /ws/generate` | 설정(+선택적 `master_seed`·`server_id`) 수신 → 해석 → 프로비저닝 → 생성, `resolved`/`provision`/진행률/이미지/`saved` 중계 |
| `POST /api/afk/start` | AFK 분산 루프 시작 (본문 `{config, server_ids?}` — 생략 시 활성 서버 전체) |
| `POST /api/afk/stop` · `GET /api/afk/status` | 정지 신호 / 총 생성 수 + 서버별 워커 상태 |
| `GET /api/afk/last.webp` | 가장 최근 저장된 이미지 |
| `WS /ws/afk` | AFK 라이브 이벤트 중계 — 모든 이벤트에 `server_id`/`server` 태그 |

생성 웹소켓은 프롬프트를 서버에서 해석해 `resolved` 프레임을 먼저 보내고, 필요 시 `provision` 프레임(로컬→서버 모델 전송 진행률)을 흘린 뒤, 동기 `ComfyClient.run`을 스레드로 돌려 이벤트를 중계한다. 이미지/프리뷰는 JSON 헤더 프레임 + 바이너리 프레임 쌍으로 보낸다.

`AfkManager`는 선택된 서버마다 `AfkWorker`(asyncio Task)를 띄우는 fleet 오케스트레이터다. 각 워커는 ① pre-flight(`provision_missing`)로 부족한 모델을 로컬 저장소에서 자동 전송하고, ② 전역 인덱스를 클레임한 뒤 새 master seed로 `pipeline.prepare` → `ComfyClient.run` → `storage.save_image`(메타 임베드)를 반복한다. 실패한 슬롯은 반납되어 다른 서버가 재사용하고, 워커별 연속 오류 누적 시 그 워커만 멈춘다. 모든 이벤트는 `server_id` 태그와 함께 `/ws/afk` 구독자에게 브로드캐스트되어 서버별 라이브 카드에 렌더된다.

### `static/` — 프론트엔드

빌드 스텝 없는 바닐라 JS.

- **`main.js`** — `/api/servers` → `/api/options?server_id=`로 폼을 채우고(와일드카드 블록·LoRA 행 동적 생성), 생성 시 `/ws/generate`로 설정을 보낸 뒤 텍스트·바이너리 프레임을 짝지어 그린다. 서버 패널(헬스 점·M 배지), AFK 분산 체크박스, 서버별 AFK 카드, 재현 패널, 구성 관리(`Ctrl/Cmd+S` 저장), 패널 접힘·박스 높이 등 뷰 상태의 `localStorage` 영속, 모바일 리플로우.
- **`models.js`** — 모델 관리 페이지: 서버(+로컬 저장소) 선택, 카테고리·파일 테이블, XHR 진행률 업로드, 다운로드/삭제/해시, diff 배지와 복사(+작업 진행 패널).

## 개발

타입 체크(Pylance와 동일한 pyright 엔진):

```bash
uv run pyright          # 0 errors 기대
```

`pyrightconfig.json`이 `.venv`와 `src/` 경로를 지정한다. 타입 흐름은 **UI JSON → `GenerationConfig`(검증) → `build_workflow` → ComfyUI 그래프 → `Event` 스트림 → 브라우저**까지 전 구간 타입화돼 있다.

> 참고: 이 저장소가 놓인 일부 환경에서는 pyright 번들 node가 `libatomic.so.1`을 찾지 못한다. 그럴 땐 `LD_LIBRARY_PATH=/path/to/conda/lib uv run pyright`처럼 실행한다. IDE의 Pylance는 자체 엔진이라 영향받지 않는다.
