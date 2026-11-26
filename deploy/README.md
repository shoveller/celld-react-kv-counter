# 춘천 OCI + Caddy + R2로 실행하기

`compose.oci.yaml`은 Cloudflare DNS only, OCI의 Caddy, celld 서버와 R2 fleet
bucket을 연결하는 독립 Compose 구성입니다. 기본 `compose.yaml`이나
`compose.r2.yaml`과 합치지 않습니다.

```text
브라우저 → DNS only로 찾은 OCI 공인 IP → Caddy HTTPS → app:8080
                                                       │
                                                celld + SQLite
                                                       │
                                                R2 fleet bucket

deploy 서비스 → 코드·정적 자산을 같은 R2 bucket에 배포하고 종료
```

이 구성의 `app`은 `celld dev`가 아니라 fleet node로 실행합니다. R2는
celld의 배포와 영속 상태를 보관하는 저장 기반이며, Worker에 R2 바인딩을
추가하는 방식이 아닙니다. 카운터의 `COUNTER_KV` 코드는 그대로 사용합니다.

## 1. R2와 환경 설정

프로젝트 루트에서 `.env.oci.example`을 `.env.oci`로 복사합니다.
이미 설정한 파일이 있으면 덮어쓰지 말고 새 R2 항목을 추가합니다.

```sh
cp .env.oci.example .env.oci
```

```dotenv
APP_DOMAIN=celld-test.illuwa.click
CELLD_STATE_DIR=./data/celld-r2
CELLD_BUCKET=s3://your-celld-oci-bucket
S3_ENDPOINT=https://YOUR_ACCOUNT_ID.r2.cloudflarestorage.com
AWS_REGION=auto
AWS_ACCESS_KEY_ID=REPLACE_ME
AWS_SECRET_ACCESS_KEY=REPLACE_ME
```

- 전용 비공개 R2 bucket을 만들고 해당 bucket의 Object Read & Write 권한이 있는
  S3 API Access Key ID/Secret Access Key를 입력합니다. 일반 Cloudflare API
  token 문자열을 Secret Access Key 자리에 넣지 않습니다.
- `S3_ENDPOINT`는 해당 bucket에 표시되는 S3 API endpoint입니다. 예시는 일반
  jurisdiction endpoint이며, jurisdiction을 지정한 bucket은 그에 맞는 endpoint를 사용합니다.
- 춘천 OCI에서 주로 접근하므로 새 bucket 생성 시 Asia Pacific (`apac`)
  location hint를 고려합니다. 힌트는 최선 노력이며 서울·춘천 배치를 보장하지 않습니다.
- `AWS_REGION=auto`는 S3 서명 설정입니다. bucket을 미국이나 아시아로 옮기는
  설정이 아니며, Compose에 위치 힌트를 추가해도 기존 bucket 위치가 바뀌지 않습니다.
- `CELLD_STATE_DIR`는 SQLite·복제 작업 상태를 위한 로컬 경로입니다.
  상대 경로는 프로젝트 루트 기준입니다. 기존 개발 데이터와 다른 경로를 씁니다.

R2 자격증명은 `deploy`와 `app`에만 전달하고 Caddy에는 전달하지 않습니다.
`.env.oci`는 Git과 Docker build context에서 제외됩니다.

## 2. 최초 배포와 서버 시작

OCI 보안 그룹과 호스트 방화벽에서 TCP 80/443을 허용합니다.
Cloudflare DNS는 OCI 공인 IPv4를 가리키는 **DNS only A 레코드**를 사용합니다.
공인 IP가 바뀌면 DNS도 갱신합니다.

Docker와 Docker Compose만 있으면 다음 순서로 실행할 수 있습니다.

```sh
docker compose --env-file .env.oci -f compose.oci.yaml config --quiet
docker compose --env-file .env.oci -f compose.oci.yaml build deploy
docker compose --env-file .env.oci -f compose.oci.yaml run --rm --no-deps deploy
docker compose --env-file .env.oci -f compose.oci.yaml up -d --wait --wait-timeout 180 app caddy
docker compose --env-file .env.oci -f compose.oci.yaml logs --tail=100 -f app caddy
```

각 명령의 성공을 확인한 후 다음 명령을 실행합니다. 먼저 R2에 배포해야
`app`이 실행할 코드를 읽을 수 있습니다. `app`과 `deploy`는 같은 로컬 이미지
`celld-react-kv-counter-oci:local`을 사용합니다. `deploy`는 `tools` 프로필에 있어
일반 `up`에서는 실행되지 않습니다.

호스트에 npm이 있다면 같은 작업을 아래 명령으로 실행합니다.

```sh
npm run oci:deploy
npm run oci:start
npm run oci:logs
# 종료: npm run oci:stop
```

기존 `deploy`, `server:start` 스크립트는 Caddy 없는 `compose.r2.yaml`용입니다.
OCI에서는 `oci:*` 명령을 사용합니다. 로컬 개발 구성과 OCI 구성은 프로젝트
이름과 `app` 서비스 이름을 공유하므로 같은 호스트에서 동시에 실행하지 않습니다.

### HTTPS와 listener

Caddy가 HTTP-01로 Let's Encrypt 인증서를 발급·갱신하며 HTTP를 HTTPS로
리다이렉트합니다. HTTP/1.1과 HTTP/2를 사용하며 UDP 443은 공개하지 않습니다.
관리 포트 2019는 호스트에 공개하지 않습니다.

- `app:8080`: Caddy가 연결하는 사용자 HTTP listener. 호스트에서는
  `127.0.0.1:8080`으로만 접근합니다.
- `app:8081`: celld 내부 listener. 호스트에 publish하지 않습니다.
- Caddy는 app의 celld healthcheck가 통과한 후 시작합니다.

```sh
curl --fail http://127.0.0.1:8080/.well-known/celld/health
curl --fail https://celld-test.illuwa.click/api/counter
```

## 3. 기존 로컬 데이터에서 전환할 때

**기존 OCI에서 실행 검증한 구성은 `celld dev`와 로컬 저장소를 사용했습니다.
이번 R2 구성으로 전환해도 기존 KV 값이 자동으로 이전되지는 않습니다.**
새 bucket이면 카운터는 0부터 시작하고, 기존 fleet bucket이면 그 안의 값을 읽습니다.

1. 기존 앱이 실행 중일 때 현재 카운터 값을 기록합니다.
2. 기존 Compose 파일과 `.env.oci` 설정을 보관합니다. 개발 데이터가
   `../celld-react-kv-counter-data` 또는 `data/counter`에 있다면 그대로 보존합니다.
3. 기존 값을 계속 사용해야 한다면 트래픽을 멈추고 별도의 데이터 이전을
   완료한 뒤 전환합니다. 이 구성 변경은 데이터 복사나 가져오기를 수행하지 않습니다.
4. `.env.oci`에 R2 항목과 새 `CELLD_STATE_DIR`를 추가합니다.
   과거 `COUNTER_DATA_DIR`는 새 구성에서 사용하지 않습니다.
5. 기존 app을 중지한 후 R2 배포 및 새 app 기동을 진행합니다.

```sh
docker compose --env-file .env.oci -f compose.oci.yaml stop app
docker compose --env-file .env.oci -f compose.oci.yaml build deploy
docker compose --env-file .env.oci -f compose.oci.yaml run --rm --no-deps deploy
docker compose --env-file .env.oci -f compose.oci.yaml up -d --wait --wait-timeout 180 app caddy
```

첫 전환에는 중단 시간이 있습니다. 실패 시 보관한 이전 Compose와 환경 설정으로
기존 개발 저장소를 다시 사용할 수 있지만, 전환 후 R2에 기록한 변경은 이전 저장소로
자동 복사되지 않습니다.

Caddy의 기존 `data/caddy-data`와 `data/caddy-config` 경로는 유지합니다.
외부 `celld-caddy-data`·`celld-caddy-config`에서 처음 옮기는 경우에는 Caddy를
중지하고 소유권과 권한을 보존해 옮깁니다. 기존 대상 디렉터리를 덮어쓰지 않습니다.

## 4. 수정 배포와 관리

React 또는 Worker 코드를 수정한 뒤 `npm run oci:deploy`를 실행합니다.
실행 중인 node는 기본 30초 간격으로 R2 배포 변경을 확인합니다.
Compose·환경 변수·실행 이미지도 변경했다면 `npm run oci:start`로 재생성합니다.
단순 `restart`는 새 환경 변수나 mount를 반영하지 않습니다.

```sh
docker compose --env-file .env.oci -f compose.oci.yaml ps
docker compose --env-file .env.oci -f compose.oci.yaml exec -T caddy caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
docker compose --env-file .env.oci -f compose.oci.yaml exec -T caddy caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile
```

| 위치 | 보관 내용 |
| --- | --- |
| R2 fleet bucket | 배포 코드, 정적 자산, KV 영속 상태 및 fleet 메타데이터 |
| `CELLD_STATE_DIR` | celld node의 로컬 SQLite·복제 작업 상태 |
| `data/caddy-data/` | ACME 계정, 인증서 및 개인키 |
| `data/caddy-config/` | Caddy 실행 상태 |
| `.env.oci` | 도메인, R2 접근키, 저장 경로 |

R2에 데이터를 보관하는 것과 실수로 지운 데이터를 복원할 별도 백업은 다릅니다.
기존 개발 데이터와 인증서 디렉터리를 삭제하지 않고 전환 결과를 확인합니다.

## 5. 춘천 OCI ↔ R2의 지연

이 구성은 `CELLD_DURABILITY=bucket`을 명시해 쓰기를 R2에 저장한 후 성공
응답합니다. 단일 node에서는 기본 `fleet` 모드도 peer가 없어 bucket 저장을
기다리므로, 이 설정은 단일 node의 내구성 경로를 명확히 표현합니다.

활성 cell의 일반적인 작은 KV 조회는 로컬 SQLite에서 처리할 수 있습니다.
생성·수정·삭제는 R2 저장 지연의 영향을 받으며, cell 복원이나 큰 KV 값 조회도
R2 접근이 필요할 수 있습니다. DNS only 여부는 이 서버→R2 경로와 별개입니다.
미국 경유가 필수인 것은 아니며 bucket 배치와 실제 OCI 연결 지연에 달려 있습니다.
R2 사용만으로 KV의 read-modify-write 증가가 원자적이 되지는 않습니다.

## 근거와 검증 범위

- [celld v0.6.3 배포·node·저장소 설정](https://github.com/denoland/celld/blob/v0.6.3/docs/README.md)
- [celld 영속성 보장](https://celld.dev/docs/guarantees/)
- [R2 데이터 위치와 location hints](https://developers.cloudflare.com/r2/reference/data-location/)
- [R2 S3 API 자격증명](https://developers.cloudflare.com/r2/api/tokens/)

기존 로컬 저장소 기반 OCI+Caddy 구성은 사용자가 OCI에서 실행 검증했습니다.
R2 연결 변경에서 다음 로컬 검증을 통과했습니다.

- `npm run build`: TypeScript 검사와 React 빌드
- 예제 환경값을 사용한 Compose 설정 검사
- `tools` 프로필을 포함한 설정에서 deploy/app의 R2 환경 일치, Caddy에 접근키
  미전달, 내부 포트 비공개, healthcheck 시작 의존성, npm 명령 연결 확인
- Secret Access Key가 비어 있을 때 Compose가 실행 전에 오류를 내는지 확인

실제 R2 자격증명을 사용한 업로드·OCI 재기동·데이터 복원은 별도 실행 확인이 필요합니다.
