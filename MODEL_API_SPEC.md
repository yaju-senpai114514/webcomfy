# webcomfy Model Management API 사양서 (v1)

ComfyUI 서버에 설치되는 **라우트 전용 커스텀 노드**의 API 사양이다. 노드 클래스는
없고(`NODE_CLASS_MAPPINGS = {}`), `PromptServer.instance.routes`에 HTTP 엔드포인트만
등록한다. 소비자는 webcomfy 서버(별도 호스트 가능)이며, 이 API를 통해 ComfyUI의
모델 디렉토리 전체를 조회/업로드/다운로드/삭제하고 파일별 메타데이터(업로드 시각,
sha256)를 관리한다.

## 0. 용어와 기본 규칙

- **category**: ComfyUI `folder_paths.folder_names_and_paths`의 키
  (`loras`, `checkpoints`, `vae`, `clip`, `unet`(또는 `diffusion_models`),
  `upscale_models`, `embeddings`, …). 하드코딩하지 말고 `folder_paths`에서
  동적으로 가져온다. 존재하지 않는 category 요청은 `404 unknown_category`.
- **name**: category 베이스 디렉토리 기준 **상대 경로**. 서브폴더 허용
  (`styles/foo.safetensors`). 구분자는 항상 `/`(URL 경로에 그대로 노출,
  aiohttp 라우트는 `{name:.+}` 패턴 사용).
- 한 category가 여러 베이스 디렉토리를 가질 수 있다(`extra_model_paths.yaml`).
  조회는 전체 병합, 동일 name 충돌 시 `folder_paths.get_full_path()`와 같은
  우선순위로 하나만 노출한다. **업로드는 항상 첫 번째(기본) 베이스 디렉토리**에
  쓴다.
- 모든 응답은 JSON(다운로드 제외). 시각은 ISO 8601 UTC(`2026-07-05T12:34:56Z`).
  해시는 sha256 소문자 hex 64자.
- URL 프리픽스는 `/webcomfy/models`로 고정한다. ComfyUI 내장 `/models`,
  `/models/{folder}`와 충돌하지 않도록 반드시 이 프리픽스를 유지할 것.

## 1. 보안 요구사항 (필수)

이 API는 사실상 호스트 파일 쓰기/삭제 권한이다. 아래는 전부 필수.

1. **경로 확정(confinement)**: `name`을 각 베이스 디렉토리와 join 후
   `os.path.realpath()`로 해석한 결과가 해당 베이스 디렉토리 내부인지 검사.
   실패 시 `400 invalid_name`. `..` 포함, 절대경로, 빈 세그먼트, NUL,
   백슬래시는 join 이전에 즉시 거부.
2. **확장자 화이트리스트**: `.safetensors`, `.sft`, `.ckpt`, `.pt`, `.pth`,
   `.bin`, `.gguf`, `.onnx`, `.yaml`(설정 동반 파일). 그 외 업로드는
   `400 invalid_extension`. 숨김 파일(`.`으로 시작하는 세그먼트) 거부.
3. **인증(선택적 활성화, Ed25519 요청 서명)**: 확장 루트(`__init__.py` 옆)에
   `<name>.pub`(Ed25519 공개키, PEM 또는 OpenSSH 형식) 파일이 하나라도 있으면
   모든 엔드포인트에서 요청 서명을 검증한다. 클라이언트는 아래 정규 문자열을
   개인키로 서명해 base64로 보낸다:

   ```
   webcomfy-v1\n{METHOD}\n{PATH}\n{QUERY}\n{TIMESTAMP}\n{NONCE}
   ```

   - `PATH` = 퍼센트 디코딩된 요청 경로(`/webcomfy/models/...`), `QUERY` =
     디코딩된 `k=v` 쌍을 정렬 후 `&`로 결합(쿼리 없으면 빈 문자열),
     `TIMESTAMP` = unix 초(정수), `NONCE` = 요청마다 새로운 랜덤 문자열
     (16바이트 hex 권장).
   - 요청 헤더 4개: `X-Webcomfy-Key`(= `.pub` 파일명에서 확장자를 뺀 키 이름) ·
     `X-Webcomfy-Timestamp` · `X-Webcomfy-Nonce` · `X-Webcomfy-Signature`.
   - 헤더 누락, 미신뢰 키, 서명 불일치, 타임스탬프 창(±300초) 밖, 논스 재사용은
     전부 `401 unauthorized`. 본문은 서명하지 않는다(수 GB 스트리밍) — 업로드
     무결성은 서명에 포함되는 `sha256` 쿼리 파라미터로 보장.
   - `.pub`가 하나도 없으면 인증 없음(신뢰 네트워크 전제) — 이 동작을 README에
     명시할 것. 구 `WEBCOMFY_MODELS_TOKEN` Bearer 토큰 방식은 **폐기**되었다.

## 2. 메타데이터 인덱스

- 위치: `folder_paths.get_user_directory()/webcomfy/model_index.json`
  (모델 디렉토리 안에 두지 않는다 — 스캔 오염 방지).
- 스키마:

```json
{
  "version": 1,
  "files": {
    "loras/styles/foo.safetensors": {
      "size": 151060480,
      "mtime": 1751706896.123,
      "sha256": "ab12…64자",
      "uploaded_at": "2026-07-05T12:34:56Z"
    }
  }
}
```

- 키는 `"{category}/{name}"`.
- **유효성 검증**: 조회 시 인덱스의 `size`/`mtime`이 실제 파일과 다르면 그
  엔트리는 스테일로 간주 — 응답에서 `sha256: null`, `uploaded_at: null`로
  내리고 인덱스 엔트리는 해시 재계산 시까지 유지한다(대역 외 덮어쓰기 감지).
- 인덱스에 없는 파일(원래 있었거나 scp 등으로 들어온 것)도 목록에는 항상
  포함하되 `sha256: null`, `uploaded_at: null`.
- 쓰기는 원자적으로(tempfile + `os.replace`). 프로세스 내 mutex로 직렬화.

## 3. 파일 메타 객체 (공통 응답 단위)

```json
{
  "name": "styles/foo.safetensors",
  "category": "loras",
  "size": 151060480,
  "mtime": "2026-07-05T12:34:50Z",
  "sha256": "ab12…" ,          // 미계산/스테일이면 null
  "uploaded_at": "2026-07-05T12:34:56Z",  // 이 API로 업로드된 경우만, 아니면 null
  "hash_state": "done"          // "done" | "pending" | "none"
}
```

`hash_state`: `done`=유효한 해시 보유, `pending`=계산 작업 진행/대기 중,
`none`=해시 없음.

## 4. 엔드포인트

### 4.1 `GET /webcomfy/models`

카테고리 요약.

```json
{
  "api_version": 1,
  "categories": [
    {"category": "loras", "file_count": 42, "total_size": 12345678901},
    {"category": "checkpoints", "file_count": 3, "total_size": 19999999999}
  ]
}
```

### 4.2 `GET /webcomfy/models/{category}`

카테고리 내 전체 파일 목록(재귀). 응답: `{"files": [<파일 메타 객체>, …]}`.
`folder_paths.get_filename_list(category)`가 지원하는 확장자 필터와 동일한
파일 집합을 반환해야 한다(ComfyUI 드롭다운과 1:1 대응).

### 4.3 `GET /webcomfy/models/{category}/{name:.+}`

파일 다운로드. `Content-Type: application/octet-stream`,
`Content-Length` 필수, HTTP Range 지원(aiohttp `FileResponse` 사용).
없으면 `404 not_found`.

단, 같은 경로 패턴의 메타 조회와 구분하기 위해 **쿼리 `?meta=1`이면 다운로드
대신 파일 메타 객체 하나를 JSON으로 반환**한다.

### 4.4 `POST /webcomfy/models/{category}/{name:.+}`

업로드. 요청 본문은 **raw 바이너리**(`application/octet-stream`,
multipart 아님 — 스트리밍 단순화).

쿼리 파라미터:

| 파라미터 | 기본 | 의미 |
|---|---|---|
| `replace` | `false` | 동일 name 존재 시 덮어쓰기 허용 |
| `sha256` | 없음 | 기대 해시. 수신 완료 후 불일치 시 파일 폐기, `422 hash_mismatch` |

동작 요구사항:

1. 대상 파일이 이미 있고 `replace=false`면 즉시 `409 already_exists`
   (본문 수신 전에 헤더만 보고 거부).
2. 본문은 **청크 스트리밍**으로 수신하며(전체 메모리 적재 금지) 수신과 동시에
   sha256을 계산한다.
3. 임시 파일은 대상과 **같은 파일시스템**의 전용 디렉토리
   (`<베이스>/.webcomfy_tmp/`)에 쓴다. 이 디렉토리는 숨김이므로 목록 스캔에
   잡히지 않는다. 완료 후 `os.replace()`로 최종 경로로 이동(원자적).
   실패/중단 시 임시 파일 삭제.
4. 성공 시 인덱스에 `sha256`, `size`, `mtime`, `uploaded_at`(현재 UTC) 기록.
5. 응답 `201`(신규) 또는 `200`(replace), 본문은 파일 메타 객체.
6. `replace=true`로 덮어쓴 경우 응답에
   `"warning": "stale_cache"` 필드를 추가한다 — ComfyUI의 LoraLoader 계열은
   경로 문자열 기준 인메모리 캐시라, 동명 덮어쓰기 시 재시작 전까지 이전
   가중치가 쓰일 수 있음을 호출자에게 알리는 용도.

### 4.5 `DELETE /webcomfy/models/{category}/{name:.+}`

파일 삭제 + 인덱스 엔트리 제거. 성공 `204`. 없으면 `404`.
삭제로 디렉토리가 비어도 디렉토리 자체는 지우지 않는다.

### 4.6 `POST /webcomfy/models/{category}/{name:.+}/hash`

해시 (재)계산 트리거. 대역 외 유입 파일이나 스테일 엔트리용.

- 이미 유효한 해시가 있으면 `200` + 파일 메타 객체(재계산하지 않음.
  강제 재계산은 `?force=1`).
- 계산이 필요하면 백그라운드 작업 큐에 넣고 `202` +
  `{"hash_state": "pending"}` 반환. 완료 여부는 `?meta=1` 조회로 폴링.
- 동일 파일에 대한 중복 요청은 기존 작업에 합류(중복 계산 금지).
- 백그라운드 워커는 **전역 1개**(동시 해시 1건) — 대용량 파일 IO 폭주 방지.
- 계산 중 파일이 삭제/변경되면 작업을 조용히 폐기.

## 5. 오류 형식 (공통)

```json
{"error": {"code": "already_exists", "message": "loras/foo.safetensors exists; pass replace=1"}}
```

| HTTP | code | 상황 |
|---|---|---|
| 400 | `invalid_name` / `invalid_extension` | 경로·확장자 검증 실패 |
| 401 | `unauthorized` | 토큰 불일치 |
| 404 | `unknown_category` / `not_found` | |
| 409 | `already_exists` | replace 없이 동명 업로드 |
| 422 | `hash_mismatch` | 기대 해시 불일치 |
| 500 | `internal` | 그 외(디스크 부족 포함, message에 원인) |

## 6. 구현 노트

- 등록 방법: 커스텀 노드 패키지 `__init__.py`에서
  `from server import PromptServer` 후 `@PromptServer.instance.routes.get(...)`
  방식으로 등록. `NODE_CLASS_MAPPINGS = {}` export 필수(로더가 요구).
- aiohttp 기본 client_max_size 제한에 걸리지 않도록 업로드 핸들러는
  `request.content`(StreamReader)에서 직접 읽는다.
- 업로드/삭제 후 별도 캐시 무효화는 불필요 — ComfyUI `folder_paths`의 파일
  목록 캐시는 디렉토리 mtime 기반이라 다음 조회 때 자동 반영된다.
- 로깅: 업로드/삭제는 파일 경로·크기·클라이언트 주소를 INFO로 남긴다.

## 7. 검수 기준 (에이전트 완료 조건)

1. `curl`로 4.1~4.6 전부 동작 확인(업로드→목록→메타→다운로드→삭제 왕복).
2. `..`, 절대경로, `.hidden`, 비허용 확장자 업로드가 전부 400으로 거부됨.
3. 업로드 직후 ComfyUI `/object_info`의 `LoraLoaderModelOnly.lora_name`
   목록에 새 파일이 나타남(재시작 없이).
4. `sha256` 쿼리로 틀린 해시를 주면 파일이 남지 않고 422.
5. 업로드 중단(클라이언트 끊김) 시 `.webcomfy_tmp/`에 잔여물이 남지 않음.
6. 인덱스 파일이 없거나 깨져 있어도 모든 조회 엔드포인트가 동작(해시만 null).
