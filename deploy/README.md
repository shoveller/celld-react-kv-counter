# OCI에서 Caddy로 직접 HTTPS 호스팅하기

`compose.oci.yaml`은 프로젝트 내부의 Caddy 설정으로 React/Worker 앱을 직접
호스팅하는 별도 Compose 구성입니다. 기본 `compose.yaml`이나
`compose.r2.yaml`과 합치지 않습니다. 앱은 기존과 같이 `celld dev`로 실행하므로
공개 학습용이며, 정확한 동시 증가나 중요 데이터 보관 용도가 아닙니다.

## 실행

프로젝트 루트에서 실행합니다. Docker와 Docker Compose가 필요합니다.

1. `.env.oci.example`을 `.env.oci`로 복사하고 도메인과 데이터 경로를 확인합니다.
   `COUNTER_DATA_DIR`의 상대 경로는 프로젝트 루트 기준입니다.
2. OCI 인스턴스에 연결된 보안 그룹에서 TCP 80과 443을 허용합니다.
   Cloudflare DNS는 인스턴스 공인 IPv4를 가리키는 **DNS only A 레코드**로
   설정합니다. 공인 IP가 바뀌면 DNS도 갱신해야 합니다.
3. 다음 명령으로 앱과 Caddy를 실행합니다.

```sh
cp .env.oci.example .env.oci
docker compose --env-file .env.oci -f compose.oci.yaml config --quiet
docker compose --env-file .env.oci -f compose.oci.yaml up --build -d
docker compose --env-file .env.oci -f compose.oci.yaml logs --tail=100 -f app caddy
```

`.env.oci`를 이미 편집했다면 `cp`를 다시 실행하지 않습니다.

Caddy가 HTTP-01로 Let's Encrypt 인증서를 발급·갱신하며, HTTP는 HTTPS로
리다이렉트합니다. HTTP/1.1과 HTTP/2를 사용하며 UDP 443은 공개하지 않습니다.
관리 포트 2019는 호스트에 공개하지 않습니다. 앱의 8080은 호스트 loopback에만
공개되며, Caddy는 Compose 네트워크에서 `app:8080`으로 연결합니다.

## 보관할 설정과 데이터

| 경로 | 용도 | Git/Docker 빌드 포함 |
| --- | --- | --- |
| `compose.oci.yaml` | 앱 및 Caddy 실행 구성 | 포함 |
| `deploy/caddy/Caddyfile` | 도메인, HTTPS, reverse proxy 설정 | 포함 |
| `.env.oci.example` | 비밀값 없는 환경 설정 예시 | Git 포함 |
| `.env.oci` | 해당 서버의 도메인·데이터 경로 | 제외 |
| `data/counter/` | 기본 카운터 저장 경로 | 제외 |
| `data/caddy-data/` | ACME 계정, 인증서 및 개인키 | 제외 |
| `data/caddy-config/` | Caddy 실행 상태 | 제외 |

설정만 Git에 보관해도 카운터와 인증서가 백업되는 것은 아닙니다.
별도 백업 시 위 데이터와 `.env.oci`를 안전하게 보관하고, 개인키는 공개하거나
커밋하지 않습니다. `COUNTER_DATA_DIR`를 바꾸면 다른 데이터를 읽게 되므로
기존 데이터가 사라진 것처럼 보일 수 있습니다.

## 관리와 재시작

```sh
docker compose --env-file .env.oci -f compose.oci.yaml ps
docker compose --env-file .env.oci -f compose.oci.yaml restart caddy
```

Caddyfile을 수정한 경우에는 다음 명령으로 검증 후 무중단으로 반영할 수 있습니다.

```sh
docker compose --env-file .env.oci -f compose.oci.yaml exec -T caddy caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
docker compose --env-file .env.oci -f compose.oci.yaml exec -T caddy caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile
```

Compose의 마운트나 환경 변수를 바꾼 경우 `restart`만으로는 반영되지 않습니다.
`up -d --no-deps caddy`로 Caddy를 재생성합니다. 데이터 디렉터리를 삭제하거나
`celld --clean`을 실행하지 않습니다.

## 기존 OCI 외부 설정에서 이전할 때

현재 OCI의 카운터 데이터는 프로젝트 밖의
`../celld-react-kv-counter-data`에 있습니다. 기존 값을 유지하려면 해당 서버의
`.env.oci`에 다음을 설정합니다.

```dotenv
APP_DOMAIN=celld-test.illuwa.click
COUNTER_DATA_DIR=../celld-react-kv-counter-data
```

기존 Caddy를 중지한 뒤 외부의 `celld-caddy-data`와 `celld-caddy-config`
디렉터리를 각각 프로젝트의 `data/caddy-data`와 `data/caddy-config`로 옮깁니다.
소유권과 권한을 보존하며, 대상 경로가 이미 있으면 덮어쓰지 않습니다.
앱의 프로젝트 이름과 서비스 이름은 유지합니다.

```sh
docker compose --env-file .env.oci -f compose.oci.yaml config --quiet
docker compose --env-file .env.oci -f compose.oci.yaml up -d --no-deps caddy
curl --fail https://celld-test.illuwa.click/api/counter
```

인증서 지문과 데이터 파일을 이전 전후 비교한 뒤 이전 실행 설정을 보관합니다.
앱의 데이터를 새 디렉터리로 복사하거나 초기화할 필요는 없습니다.
