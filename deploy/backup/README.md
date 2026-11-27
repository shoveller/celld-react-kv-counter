# 로컬 celld 운영 + R2 비동기 백업

이 절차는 **단일 노드·celld v0.6.3·KV 카운터**를 대상으로 합니다.
사용자 요청은 호스트 디스크의 저장 완료까지만 기다립니다.
별도 프로세스가 SQLite의 일관된 스냅샷을 만들고 R2에 보관합니다.
호스트 디스크를 잃으면 마지막 성공 백업 이후의 변경은 모두 잃을 수 있습니다.
백업 주기 15분은 데이터 유실의 상한을 보장하지 않습니다.

## 1. 무엇을 백업하는가

```text
브라우저 → celld → 실행용 셀 SQLite(runtime)
                     ↓ LTX 복구 로그
               로컬 bucket(objects.sqlite3)
                     ↓ SQLite Online Backup API
               독립된 스냅샷 파일
                     ↓ 별도 백업 프로세스의 S3 API
               R2 backups/celld-local/...
```

`objects.sqlite3`는 단순 색인이 아니라 셀의 LTX 로그와 bucket 객체를
담는 로컬 object store입니다. `runtime`이 없는 복원 경로에서 celld가
로그를 읽어 실행용 SQLite를 재구성합니다. 실행 중인 DB를 `cp`하거나
DB와 WAL을 서로 다른 시점에 복사하지 않습니다.

이 기능은 celld 자체를 패치하지 않습니다. 애플리케이션 저장 모드는 기존
`compose.bind.yaml`의 `celld dev`를 유지하며, 프로젝트에 별도 백업 도구를 추가합니다.
`CELLD_BUCKET`을 app에 주입해 R2 fleet로 바꾸는 방식이 아닙니다.

> upstream은 LocalStore를 개발용이라고 명시합니다. 이 단일 KV의 검증을
> 일반 DO·알람·D1·queue·다중 노드의 운영 보장으로 확대하면 안 됩니다.
> 특히 healthy follower가 있는 fleet은 bucket 반영 전 승인할 수 있으므로
> 이 백업 경계를 그대로 적용하지 않습니다.

## 2. 준비

이미 로컬 bind 모드로 운영 중인 app에 백업 기능을 붙이는 절차입니다.
새 서버의 app·Caddy·TLS 준비는
[기존 OCI bind 배포 레시피의 2.8절](https://wiki.illuwa.click/#root/aO0kr8V1CHz9)을
먼저 따릅니다. R2 fleet 레시피로 app을 시작하지 않습니다.

대상은 Linux OCI의 다음 checkout입니다. 다른 경로라면 아래 경로와
systemd service의 `WorkingDirectory`, `ExecStart`, 환경변수를 함께 고칩니다.

```sh
cd /home/ubuntu/works/celld-react-kv-counter
git status --short --branch
git pull --ff-only origin main
python3 --version
docker inspect --format '{{json .Config.Cmd}} {{range .Mounts}}{{.Source}}->{{.Destination}};{{end}}' celld-react-kv-counter-app-1
test -f data/counter-bind/dev/objects.sqlite3
```

운영 app의 mount는 `data/counter-bind` → `/app/.celld`여야 합니다.
기본 로컬 bind 레시피의 경로는 `data/dev/objects.sqlite3`이므로 혼동하지 않습니다.
실행 중인 app은 백업을 위해 멈추지 않습니다.

SQLite WAL reader는 원본을 read-only로 열어도 shared-memory 잠금 파일을
만들 디렉터리 권한이 필요할 수 있습니다. `attempt to write a readonly database`가
발생하면 `immutable=1`로 우회하지 않습니다. WAL이 누락된 백업이 될 수 있습니다.
이 OCI의 app은 root로 실행하고 dev 디렉터리만 root 소유였으므로, 다음처럼
**dev 디렉터리 하나의 소유자만** 백업 사용자로 변경했습니다.
다른 환경에서는 app UID와 기존 권한부터 확인하고 적용합니다.

```sh
stat -c '%U %G %a %n' data/counter-bind/dev data/counter-bind/dev/objects.sqlite3
sudo chown "$(id -un):$(id -gn)" data/counter-bind/dev
```

DB·runtime 전체를 재귀 chown/chmod하거나 world-writable로 바꾸지 않습니다.
이 권한 변경은 백업 사용자에게 디렉터리 쓰기 권한을 주므로 그 사용자를
신뢰해야 합니다. 이 OCI에서 되돌릴 때는 백업을 중지한 후 해당 디렉터리만
`sudo chown root:root data/counter-bind/dev`로 복구합니다.

Python 의존성은 앱 이미지 밖의 전용 venv에 설치합니다.
`python3 -m venv`가 없으면 OS의 `python3-venv` 패키지를 먼저 설치합니다.

```sh
python3 -m venv data/backup-venv
data/backup-venv/bin/python -m pip install -r requirements-backup.txt
python3 -m unittest discover -s tests -v
```

비밀은 기존 Vault 폴더 `env/oci/celld-test.illuwa.click`에서 주입합니다.
`rbw-env`는 자동 sync를 하지 않습니다. 서비스 실행 사용자 `ubuntu`의
Vault 잠금 상태와 토큰 권한을 먼저 확인합니다.

```sh
rbw unlock
rbw sync
command -v rbw-env
```

필요 변수는 `S3_ENDPOINT`(HTTPS), `AWS_ACCESS_KEY_ID`,
`AWS_SECRET_ACCESS_KEY`, `AWS_REGION=auto`, `CELLD_BUCKET=s3://BUCKET`입니다.
전용 백업 bucket을 쓰려면 `CELLD_BACKUP_BUCKET=s3://BACKUP_BUCKET`으로
분리할 수 있습니다. 기본값은 기존 `CELLD_BUCKET`이며 객체는
`backups/celld-local/` 아래에만 씁니다.
기존 fleet의 배포·소유권·LTX 객체를 덮어쓰지 않으며 원격 삭제도 하지 않습니다.
R2 키는 백업 프로세스에만 전달하며 app과 Caddy에는 넣지 않습니다.
자격증명·Vault 출력·`.env` 파일을 Git이나 가이드에 복사하지 않습니다.

## 3. 한 번 백업하고 성공을 확인하기

```sh
export CELLD_BACKUP_SOURCE=data/counter-bind/dev/objects.sqlite3
export CELLD_BACKUP_WORK_DIR=data/backups
export CELLD_BACKUP_PYTHON="$PWD/data/backup-venv/bin/python"
pnpm backup:run
cat data/backups/last-success.json
```

`pnpm` 없이도 같은 명령을 실행할 수 있습니다.

```sh
rbw-env env/oci/celld-test.illuwa.click -- bash scripts/backup-bind.sh
```

성공 출력과 `last-success.json`의 시각·manifest key·digest를 보관합니다.
스냅샷 파일과 SHA-256·크기·UTC 시각·소스 revision을 담은 manifest를
업로드한 뒤에만 원격 `latest.json`과 로컬 성공 기록을 갱신합니다.
시간과 임의 ID가 포함된 객체 key를 쓰므로 이전 백업은 덮어쓰지 않습니다.
R2 업로드 실패는 명령 실패로 기록되고 app 요청은 계속 처리됩니다.

`latest.json`은 최근 완료 백업의 위치를 가리키는 포인터이지
과거 백업의 삭제나 보존 기간 정책이 아닙니다. 자동 정리는 구현하지 않았습니다.
반복 실행 비용과 보관 용량은 별도로 확인해야 합니다.
현재 도구의 스냅샷 단일 업로드 한도는 5 GiB입니다. 이보다 큰 DB에는
multipart 전송을 검증·추가하기 전까지 사용하지 않습니다.
전송 후 로컬 스냅샷 임시 파일은 제거되며 성공 기록만 남습니다.
원격 포인터 갱신과 로컬 성공 기록 사이에는 분산 트랜잭션이 없습니다.
R2가 PUT을 받았지만 응답이 유실되거나 로컬 기록 쓰기가 실패하면,
명령은 실패했어도 원격 백업이 존재할 수 있습니다. 다음 복원으로 확인합니다.

## 4. 15분 타이머 설치

매시 00·15·30·45분의 calendar 조건과 활성화 30초 후 조건 중 먼저 도래한
조건으로 실행합니다. `Persistent=true`는 중단 중 놓친 calendar 실행을
따라잡을 수 있으므로 첫 실행이 반드시 30초 이후인 것은 아닙니다.
최대 30초의 임의 지연과 `AccuracySec=1s`의 정확도 여유가 추가됩니다.

서비스 예시는 OCI의 `data/counter-bind` 경로와 기존 Vault 폴더를 사용합니다.
원하는 경로와 사용자 홈을 확인한 뒤 설치합니다.

```sh
mkdir -p ~/.config/systemd/user
install -m 644 deploy/backup/celld-backup.service ~/.config/systemd/user/
install -m 644 deploy/backup/celld-backup.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user start celld-backup.service
systemctl --user enable --now celld-backup.timer
systemctl --user list-timers celld-backup.timer
systemctl --user show celld-backup.service -p Result -p ExecMainStatus
journalctl --user -u celld-backup.service -n 30 --no-pager
```

로그아웃 후에도 user timer가 돌려면 linger가 필요합니다.

```sh
loginctl show-user "$USER" -p Linger
# Linger=no인 경우 관리자 권한으로 한 번 실행:
sudo loginctl enable-linger "$USER"
```

Vault가 잠기거나 인증이 만료되면 unattended 백업은 실패할 수 있습니다.
`Result=success`, `ExecMainStatus=0`, 마지막 성공 시각을 함께 확인합니다.
이 구현에는 외부 알림이 없으므로 “타이머가 켜짐”을 “복구 가능”으로 보지 않습니다.
백업 작업의 SQLite 읽기·디스크 부하는 남습니다. `Nice=10`은 CPU 우선순위만
낮추며 I/O 무간섭이나 성능 불변을 보장하지 않습니다.

## 5. R2에서 격리 복원하기

먼저 운영 app을 건드리지 않는 복원 연습을 합니다.
목적지는 비어 있어야 하며 도구는 기존 DB나 runtime을 덮어쓰지 않습니다.
복원할 manifest key를 생략하면 해당 prefix의 `latest.json`을 읽습니다.
과거 백업을 고르려면 백업 성공 출력의 manifest key를 명시합니다.

```sh
drill="$PWD/data/restore-drill/$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$drill"
rbw-env env/oci/celld-test.illuwa.click -- \
  data/backup-venv/bin/python scripts/celld_backup.py restore \
  --destination "$drill/dev" --work-dir data/restore-work
```

다운로드 파일의 크기·SHA-256·SQLite 무결성을 통과한 뒤에만 목적지에
`objects.sqlite3`를 설치합니다. 손상 파일이나 비어 있지 않은 목적지는 실패합니다.
복원은 **같은 celld 버전·Worker 코드·namespace ID·설정**으로 시작합니다.
manifest의 revision은 checkout 식별자이며 컨테이너 이미지 보관을 대신하지 않습니다.

현재 운영과 같은 이미지를 사용하되 별도 이름·포트·mount로 실행합니다.
이 예시는 이미지 안의 설정 경로가 `/app/wrangler.jsonc`인 현재 프로젝트용입니다.

```sh
image="$(docker inspect --format '{{.Image}}' celld-react-kv-counter-app-1)"
docker run -d --name celld-backup-restore-drill \
  -p 127.0.0.1:18090:8080 \
  -v "$drill:/app/.celld" \
  "$image" dev wrangler.jsonc --host 0.0.0.0 --port 8080 --no-watch
docker logs celld-backup-restore-drill
# 로그의 ready 확인 후 실행:
curl --fail http://127.0.0.1:18090/api/counter
curl --fail -X POST http://127.0.0.1:18090/api/counter
docker restart celld-backup-restore-drill
# 재시작 ready 확인 후 마지막 값 유지 확인:
curl --fail http://127.0.0.1:18090/api/counter
docker stop --timeout 60 celld-backup-restore-drill
docker rm celld-backup-restore-drill
```

연습용 POST는 격리 복원본만 변경합니다. 운영의 공개 URL에 보내지 않습니다.
연습 경로는 증거 보관 후 사람이 확인하여 지웁니다.
복원본의 증가 값은 마지막 원격 백업보다 새 데이터이므로 자동으로 운영에 합치지 않습니다.

## 6. 실제 장애 복구

1. `systemctl --user stop celld-backup.timer celld-backup.service`로 백업을 멈춥니다.
2. 기존 app을 정상 종료합니다. Caddy·TLS·원래 데이터는 보존합니다.
3. 검증한 manifest를 명시해 **연습용과 다른 새 빈 디렉터리**에 다시 복원합니다.
   예를 들어 `--destination data/recovered-TIMESTAMP/dev`로 준비합니다.
   이 pristine 복원본에는 검증용 POST를 보내지 않습니다.
   연습에서 변경한 DB·생성된 runtime을 운영에 재사용하지 않습니다.
4. `data/compose.bind.oci.yaml`의 app mount만 pristine 복원 루트
   `./data/recovered-TIMESTAMP:/app/.celld`로 바꿉니다.
   기존 runtime은 복사하지 않습니다. `.celld/dev/objects.sqlite3`의 계층을 유지합니다.
5. 아래 명령으로 app만 재생성하고 health·공개 HTTPS·값·재시작 유지를 확인합니다.
6. service의 백업 source도 새 mount에 맞게 바꾼 뒤 daemon-reload·수동 백업을
   성공시키고 timer를 다시 켭니다. 기존 소스 경로를 백업하는 실수를 피합니다.

```sh
docker compose -f compose.bind.yaml -f data/compose.bind.oci.yaml \
  up -d --no-deps --force-recreate --wait --wait-timeout 120 app
curl --fail https://celld-test.illuwa.click/api/counter
```

이때 `pnpm server:start`는 사용하지 않습니다. 해당 명령은 R2 fleet 모드이며
로컬 복원 상태와 다른 저장소를 사용합니다. 여러 호스트나 old/new 노드를 같은
로컬 bucket 복사본으로 동시에 운영하지 않습니다.

## 7. 중단·롤백과 장애 진단

```sh
systemctl --user disable --now celld-backup.timer
systemctl --user stop celld-backup.service
```

백업 기능만 중단하는 데 app 재기동이나 데이터 삭제는 필요 없습니다.
복구 전환을 되돌릴 때는 app을 정상 종료하고 보존한 mount 설정으로 복구한 뒤
app만 다시 생성합니다. 자동 삭제·원격 백업 삭제·R2 fleet 재전환은 하지 않습니다.

- 원본 없음: 실제 app mount와 `CELLD_BACKUP_SOURCE`를 확인합니다. 빈 DB를 만들지 않습니다.
- 인증·네트워크 실패: `rbw unlocked`, `rbw sync`, bucket 권한과 HTTPS endpoint를 확인합니다.
  비밀값을 출력해서 검사하지 않습니다.
- 중복 실행: 기존 백업이 끝났는지 확인합니다. 잠금 파일을 삭제해 우회하지 않습니다.
- digest·SQLite 오류: 해당 복원본을 시작하지 말고 이전 manifest를 선택합니다.
- 목적지 nonempty: 기존 데이터를 삭제하지 말고 새로운 빈 경로를 지정합니다.
- 복원 후 초기값: mount 계층, namespace ID, 동일 코드·버전인지 확인합니다.

## 8. 검증 범위와 한계

실제 배포·R2 왕복·5회 측정 결과는 지정 Trilium 가이드에 기록합니다.
측정 표본 5개는 네트워크를 포함한 순차 사용 결과이며 부하시험·물리 페인트 시간·
R2 대비 통제된 A/B 실험이 아닙니다. 동시 read-modify-write KV 카운터는 원자적이지
않으므로 동시 증가의 정확성을 이 백업 기능으로 해결하지 않습니다.

일반 DO·알람·D1·queue, 셀 간 트랜잭션, 다른 celld 버전, 다중 노드의
복구 보장은 별도 검증 대상입니다. 이 절차에 서버 재부팅·디스크 파괴 시험이나
외부 장애 알림·자동 retention은 포함하지 않습니다.

## 근거

- [dev bucket → LocalStore](https://github.com/denoland/celld/blob/8702f6d186c77698dbfb30cf29e3b1922a88f008/crates/celld/bucket.rs#L642-L654)
- [LTX 저장 후 내구성 credit](https://github.com/denoland/celld/blob/8702f6d186c77698dbfb30cf29e3b1922a88f008/crates/celld/ltx_repl.rs#L4162-L4205)
- [재시작 복원 경계](https://github.com/denoland/celld/blob/8702f6d186c77698dbfb30cf29e3b1922a88f008/crates/logic/restore.rs#L12-L17)
- [SQLite Online Backup API](https://www.sqlite.org/backup.html)
- [R2 S3 API 업로드](https://developers.cloudflare.com/r2/objects/upload-objects/#upload-via-s3-api)
