# React + KV 카운터를 celld와 Docker로 실행하기

공식 문서 확인일: 2026-11-26. celld 이미지는 `v0.6.3`으로 고정했습니다.

React 화면, Worker HTTP API, celld KV를 연결하는 **학습용 프로젝트**입니다. 기본 구성은 Docker 컨테이너 하나이며 Cloudflare 계정이나 외부 데이터베이스가 필요하지 않습니다. `celld dev`가 로컬 object store와 단일 node를 시작합니다. [celld 개발 모드 문서](https://celld.dev/docs/#develop-an-application-locally)

여러 사용자가 동시에 증가시키는 정확한 공유 카운터가 필요하면 아래의 동시성 한계를 먼저 확인하세요. 이 예제는 KV 바인딩과 저장 흐름을 배우기 위한 것입니다.

## 1. 바로 실행

Docker와 Docker Compose가 설치된 환경에서 압축을 풀고 실행합니다. Node와 celld를 호스트에 별도로 설치할 필요는 없습니다.

```sh
unzip celld-react-kv-counter.zip
cd celld-react-kv-counter
docker compose up --build -d --wait app
docker compose logs -f app
```

브라우저에서 <http://localhost:8080>을 엽니다. 로그 보기에서 Ctrl+C를 눌러도 백그라운드 컨테이너는 계속 실행됩니다.

화면에서 `1 올리기`, `새로 읽기`, `0으로 초기화`를 사용할 수 있습니다. 서버의 KV key `count` 하나를 모든 브라우저가 공유합니다. 다른 브라우저의 변경은 `새로 읽기`나 페이지 새로고침으로 확인합니다.

기본 포트는 호스트의 `127.0.0.1`에만 공개됩니다. 원격 VPS에서 실행했다면 SSH 터널 등으로 접근하거나 프록시를 구성하세요.

공식 celld 이미지는 Linux x86-64와 ARM64를 제공하므로 Apple Silicon에서도 플랫폼을 강제로 지정하지 않고 사용합니다. [공식 Docker 설명](https://github.com/denoland/celld#container)

## 2. 구성과 파일

| 파일 | 역할 |
| --- | --- |
| `src/App.tsx` | React UI, 초기 조회, 증가 및 초기화 요청, 로딩과 오류 처리 |
| `src/main.tsx` | React 진입점 |
| `src/style.css` | 화면 스타일 |
| `worker/index.ts` | KV에 접근하는 Worker API |
| `wrangler.jsonc` | Worker, KV namespace, React 정적 파일 바인딩 |
| `worker-configuration.d.ts` | `wrangler types`가 생성한 Env 및 런타임 타입 |
| `vite.config.ts` | React 빌드와 로컬 Vite 개발 서버의 API 프록시 |
| `Dockerfile` | Node에서 빌드한 후 celld 개발 모드 실행 |
| `compose.yaml` | Worker 자동 반영, API 준비 상태 검사, 로컬 데이터 디렉터리를 연결한 개발 컨테이너 |
| `compose.r2.yaml` | R2 bucket을 쓰는 별도의 배포 및 node 실행 예제 |
| `.env.r2.example` | R2 설정값 예시. 실제 키는 포함하지 않음 |
| `package-lock.json` | npm 의존성 버전과 무결성 고정 |

브라우저에서는 React가 실행됩니다. celld가 `dist/`의 HTML/CSS/JS를 서빙하고 `/api/counter` 요청은 Worker에서 처리합니다. Worker가 `env.COUNTER_KV`로 KV에 접근합니다. KV 자격증명을 브라우저에 전달하지 않습니다.

이 Dockerfile은 celld 자체를 컨테이너화합니다. 실행 중인 앱에는 Docker 소켓 연결이나 별도의 Node HTTP 서버가 필요하지 않습니다. 이미지에 남긴 Node와 `esbuild`는 `celld dev` 및 `celld deploy`의 Worker 빌드에 사용됩니다.

## 3. Wrangler 설정의 의미

`wrangler.jsonc`는 Cloudflare 호환 설정 형식입니다. 기본 예제는 Cloudflare에 배포하지 않으며 Wrangler CLI 로그인도 필요하지 않습니다.

```json
{
  "name": "celld-react-kv-counter",
  "main": "worker/index.ts",
  "compatibility_date": "2026-01-01",
  "assets": {
    "directory": "./dist",
    "binding": "ASSETS",
    "not_found_handling": "single-page-application",
    "run_worker_first": ["/api", "/api/*"]
  },
  "kv_namespaces": [
    {
      "binding": "COUNTER_KV",
      "id": "react-counter-data"
    }
  ]
}
```

`directory`는 Vite 빌드 결과입니다. `run_worker_first`는 API를 먼저 Worker로 보내어 SPA HTML 응답과 API 응답이 섞이지 않게 합니다. `ASSETS`는 Worker에서 `env.ASSETS.fetch(request)`로 정적 파일을 요청하는 바인딩입니다. [정적 파일 문서](https://celld.dev/docs/services/static-assets/)

`binding`은 코드에서 접근하는 이름이고 `id`는 namespace의 식별자입니다. celld에서는 `id`에 임의 문자열을 사용할 수 있습니다. 이름을 바꾸면 다른 namespace를 읽게 되므로 기존 값이 사라진 것처럼 보일 수 있습니다. [KV 문서](https://celld.dev/docs/services/kv/)

Worker의 `Env`는 `wrangler.jsonc`에서 생성합니다. 바인딩이나 호환성 설정을 변경하면 아래 명령으로 타입을 갱신하세요. 생성 파일은 직접 수정하지 않습니다. 타입 생성에는 Cloudflare 로그인이 필요하지 않습니다.

```sh
npm run cf-typegen
```

## 4. API 동작

| 요청 | 처리 | 성공 응답 |
| --- | --- | --- |
| `GET /api/counter` | KV 값 조회. 없으면 0 | `{"count":0}` |
| `POST /api/counter` | 현재 값에 1을 더해 저장 | `{"count":1}` |
| `DELETE /api/counter` | key 삭제, 0으로 표시 | `{"count":0}` |

POST body는 필요하지 않습니다. API 응답은 `Cache-Control: no-store`를 사용합니다. 알 수 없는 API는 404, 지원하지 않는 메서드는 405, 숫자 최대값은 409를 반환합니다.

```sh
curl -sS http://localhost:8080/api/counter
curl -sS -X POST http://localhost:8080/api/counter
curl -sS http://localhost:8080/api/counter
```

초기화는 값을 실제로 지우는 명령입니다.

```sh
curl -sS -X DELETE http://localhost:8080/api/counter
```

React는 상대 URL `/api/counter`를 호출하므로 기본 실행에서는 CORS 설정이 필요하지 않습니다. 요청 중에는 버튼을 비활성화합니다. 저장 요청의 응답을 받지 못한 경우 결과가 불확실할 수 있으므로 자동으로 재시도하지 않고 현재 값을 다시 읽도록 안내합니다.

## 5. 데이터 보관과 수정 후 실행

celld 개발 모드는 `.celld/dev`에 로컬 상태를 보관합니다. 개발용 Compose는 이를 포함하는 `/app/.celld`를 프로젝트의 `./data` 디렉터리에 연결합니다. `data/`는 Docker build context에서 제외합니다. [개발 모드와 상태 저장](https://celld.dev/docs/#develop-an-application-locally)

```sh
# 중지 — data/ 유지
docker compose down

# 같은 프로젝트를 다시 실행
docker compose up -d

# 의존성·Dockerfile·컨테이너의 React 정적 자산 갱신
docker compose up --build -d --wait app
```

값을 올린 뒤 `down`과 `up -d`를 실행하여 값이 유지되는지 직접 확인할 수 있습니다. 현재 개발 구성은 bind mount이므로 `docker compose down -v`로도 `./data`는 삭제되지 않습니다. `data/`를 삭제하거나 celld의 `--clean`을 사용하면 개발 데이터가 삭제됩니다.

기본 Compose 프로젝트 이름을 고정했기 때문에 같은 예제 디렉터리를 복제해도 같은 이름을 쓰면 기존 컨테이너와 겹칠 수 있습니다. 별개의 실습 인스턴스에는 `docker compose -p 다른이름 ...`과 서로 다른 호스트 포트를 사용하세요.

## 6. React 화면을 빠르게 수정하기

호스트에 Node 24와 Docker가 있다면 다음을 실행합니다.

```sh
npm ci
npm run dev
```

`npm run dev`는 개발용 celld 컨테이너를 빌드·실행하고 `/api/counter` 준비 상태가 확인된 뒤 Vite를 실행합니다. <http://localhost:5173>으로 접속합니다. 5173이 사용 중이면 다른 포트로 이동하지 않고 오류를 표시합니다.

`/api`와 `/api/` 하위 경로는 `vite.config.ts`에서 `http://127.0.0.1:8080`으로 프록시합니다. React 수정은 Vite HMR로 반영됩니다. 개발용 Compose는 `worker/`와 `wrangler.jsonc`를 읽기 전용 bind mount하고 `--no-watch` 없이 celld를 실행하므로, Worker와 설정 수정은 celld가 감지해 재빌드·반영합니다.

| 명령 | 역할 |
| --- | --- |
| `npm run dev` | API 준비 확인 후 Vite 프록시 개발 서버 실행 |
| `npm run dev:proxy` | 이미 실행 중인 API를 사용하는 Vite만 실행 |
| `npm run dev:server` | 개발용 API 서버 빌드·백그라운드 실행 및 준비 확인 |
| `npm run dev:logs` | Worker 재빌드 및 API 로그 확인 |
| `npm run dev:stop` | 개발용 컨테이너 중지·제거, `data/` 유지 |

Vite에서 Ctrl+C를 눌러도 개발용 Docker 서버는 계속 실행됩니다. 종료하려면 `npm run dev:stop`을 실행합니다. `dist/`는 이미지에 포함되므로 8080에서 직접 제공하는 React 화면을 갱신하려면 `npm run dev:server`로 다시 빌드합니다. Worker가 `worker/` 밖의 파일을 import하도록 확장하면 해당 소스 경로도 개발용 mount에 추가해야 합니다.

## 7. R2를 쓰는 node 배포 예제

`compose.r2.yaml`은 기본 Compose와 합치는 override가 아니라 **별도로 실행하는 구성**입니다. 기존 개발 모드와 포트가 겹치므로 먼저 `docker compose down`으로 기본 구성을 중지합니다.

전용 비공개 R2 bucket과 그 bucket에 읽기·쓰기가 가능한 S3 API 자격증명을 준비합니다. 코드와 KV 상태가 들어가는 bucket이므로 일반 정적 웹사이트처럼 공개하지 않습니다. [celld bucket 설정](https://celld.dev/docs/#configure-object-storage), [bucket 권한](https://celld.dev/docs/security/#protect-the-fleet-bucket)

```sh
cp .env.r2.example .env.r2
```

`.env.r2`의 값을 실제 값으로 바꿉니다.

```dotenv
CELLD_BUCKET=s3://your-celld-counter-bucket
S3_ENDPOINT=https://YOUR_ACCOUNT_ID.r2.cloudflarestorage.com
AWS_REGION=auto
AWS_ACCESS_KEY_ID=REPLACE_ME
AWS_SECRET_ACCESS_KEY=REPLACE_ME
```

`.env.r2`는 git과 Docker build context에서 제외됩니다. 이 값들은 Compose가 배포/런타임 프로세스에 전달하며 React 번들에 넣지 않습니다. `celld deploy` 자체는 `.env`를 자동으로 읽지 않습니다. [배포 문서](https://celld.dev/docs/#deploy-an-application)

배포 이미지를 빌드하고 Worker/정적 파일을 R2에 배포한 뒤 node를 실행합니다.

```sh
docker compose --env-file .env.r2 -f compose.r2.yaml build deploy
docker compose --env-file .env.r2 -f compose.r2.yaml run --rm deploy
docker compose --env-file .env.r2 -f compose.r2.yaml up -d celld
docker compose --env-file .env.r2 -f compose.r2.yaml logs -f celld
```

호스트에서 npm 스크립트로 같은 절차를 실행할 수도 있습니다.

```sh
npm run dev:stop
npm run deploy
npm run server:start
npm run server:logs
# 서버 종료: npm run server:stop
```

`deploy`는 이미지 빌드와 R2 업로드를 수행합니다. `server:start`는 업로드된 배포를 실행할 celld 서버를 시작합니다. 두 명령은 분리되어 있으며 모두 준비된 `.env.r2`를 사용합니다. 기본 개발 서버와 R2 서버는 8080 포트를 공유하므로 전환 전에 기존 서버를 중지합니다. 배포 명령은 실제 R2에 쓰므로 로컬 개발에는 `dev` 계열 명령을 사용합니다.

각 명령이 성공한 것을 확인하고 다음 명령을 실행합니다. `deploy` 컨테이너는 완료 후 종료됩니다. 런타임 컨테이너는 bucket에서 배포를 읽습니다. 새 소스를 배포하려면 `build deploy`와 `run --rm deploy`를 다시 실행합니다. 실행 중인 node는 기본 30초 간격으로 새 배포를 확인합니다. [배포 갱신](https://celld.dev/docs/#deploy-an-application)

이 구성에서 React/Worker/KV 연산은 자신의 서버의 celld가 처리하며 R2는 fleet bucket 역할을 합니다. **개발 모드의 기존 KV 값은 R2로 자동 이전되지 않습니다.** R2 구성에서는 해당 bucket의 데이터를 사용합니다.

8080은 사용자 앱용 포트입니다. 8081은 fleet/operator용 내부 포트이며 호스트에 공개하지 않았습니다. 도메인 서비스에는 8080 앞에 TLS와 접근 제어를 처리하는 프록시를 배치합니다. 이 예제 API 자체에는 로그인이 없습니다. [listener 구분](https://celld.dev/docs/security/#separate-the-listeners)

이 파일은 수동 배포·실행 절차를 보여주는 기반 예제입니다. `restart: "no"`이므로 프로세스가 종료되면 자동으로 다시 시작하지 않습니다. 상시 운영 시 공식 문서의 node lease 수명을 고려한 재시작 정책/supervisor를 추가해야 합니다. 단일 node는 매번 쓰기를 bucket에 저장한 뒤 성공 응답하므로 로컬 개발 모드보다 응답이 느릴 수 있습니다. [celld guarantees](https://celld.dev/docs/guarantees/)

## 8. KV 카운터의 동시성 한계

증가는 다음 두 API 호출로 구성됩니다.

```ts
const current = Number(await env.COUNTER_KV.get('count') ?? '0')
const count = current + 1
await env.COUNTER_KV.put('count', String(count))
```

이 전체 과정은 원자적 연산이 아닙니다. A와 B가 모두 0을 읽고 각각 1을 저장하면 두 번 눌러도 최종 값은 1입니다. 초기화와 증가가 동시에 실행되는 경우도 경쟁할 수 있습니다. 버튼을 비활성화하는 것은 같은 화면의 중복 클릭을 줄일 뿐 다른 브라우저의 요청을 직렬화하지 않습니다.

celld KV는 namespace 소유 cell에서 읽으므로 Cloudflare KV의 eventual consistency와는 다릅니다. 그렇더라도 별도의 `get`과 `put`을 합친 atomic increment가 제공되는 것은 아닙니다. 공식 문서도 갱신이 잦은 카운터에 Durable Object를 권합니다. [KV 일관성과 용도](https://celld.dev/docs/services/kv/)

정확한 공유 카운터로 발전시키려면 하나의 이름으로 식별한 Durable Object에 카운터를 두고 그 객체의 storage transaction을 사용하세요. React/API 주소는 유지하고 Worker의 저장 처리를 바꿀 수 있습니다. DO의 storage와 KV namespace 바인딩은 서로 다른 저장소입니다. [Durable Objects 문서](https://celld.dev/docs/services/durable-objects/)

## 9. 검증 범위

최초 예제 작성 환경에서 아래 검증은 통과했습니다.

- `npm run build`: TypeScript 타입 검사와 Vite 프로덕션 빌드
- `esbuild`의 Worker ESM 번들링
- Worker 로직을 Node에서 실행하고 메모리 KV 바인딩으로 조회·증가·초기화, 상태 코드, 캐시 헤더, 숫자 범위와 정적 파일 fallback 확인
- 위 mock 환경에서 겹친 read/write 증가가 유실될 수 있음을 확인
- 두 Compose YAML 구문 파싱과 공식 celld 문서에 대한 설정 검토

최초 작성 환경에서는 Docker 실행 검증을 하지 못했습니다. 이후 개발 자동 반영 개선 시 아래 항목을 실제 확인했습니다.

- `npm run build`: 타입 검사와 React 빌드 통과
- `docker compose config --quiet`: 개발 설정 검사 통과
- `docker compose --env-file .env.r2.example -f compose.r2.yaml config --quiet`: R2 예제 설정 검사 통과
- `npm run dev:server`: Docker 이미지 빌드와 컨테이너 기동, API healthcheck 통과
- Worker 응답에 임시 검증 헤더를 추가한 뒤 이미지 재빌드 없이 API 응답에서 확인하고, 원복 후 헤더 제거도 확인
- `dev:proxy -- --port 5174`: React 페이지, `/api/counter` 프록시 및 미등록 API의 JSON 404 응답 확인. 기본 5173 포트가 사용 중일 때는 충돌 오류 확인
- 검증 중 카운터 GET 값 `28` 유지. 검증용 Vite 프로세스는 종료

R2 업로드·R2 서버 실행과 별도의 장애 복구·영속성 테스트는 수행하지 않았습니다.

## 공식 참고 문서

- [celld 문서](https://celld.dev/docs/)
- [celld v0.6.3 공식 이미지](https://github.com/denoland/celld/pkgs/container/celld)
- [KV](https://celld.dev/docs/services/kv/)
- [Static assets](https://celld.dev/docs/services/static-assets/)
- [Durable Objects](https://celld.dev/docs/services/durable-objects/)
- [Security](https://celld.dev/docs/security/)
- [Vite 시작하기](https://vite.dev/guide/)
