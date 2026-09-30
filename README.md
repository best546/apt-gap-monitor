# 아파트 갈아타기 스프레드 모니터

국토교통부 아파트 매매 실거래 API를 정기 수집해 현재 집과 목표 단지의 가격 차이를 GitHub Pages에서 보여줍니다.

현재 운영 주기는 **매일 1회**입니다. 초기 계획은 주 1회였지만 운영자가 의도적으로 하루 1회로 변경했습니다. 정확한 실행 시각과 시간대는 DSM 작업 스케줄러에서 확인하세요.

## Synology DS920+ 설치 (권장)

1. 저장소의 ZIP을 내려받아 NAS의 `/volume1/docker/apt-gap-monitor`에 압축 해제합니다.
2. `.env.example`을 `.env`로 복사하고 아래 두 값을 입력합니다.
   - `MOLIT_API_KEY`: 공공데이터포털 Decoding 인증키
   - `GITHUB_DATA_TOKEN`: 이 저장소에 Contents 읽기/쓰기만 허용한 Fine-grained PAT
3. NAS에서 Docker Compose를 사용할 수 있는지 확인합니다. `compose.yaml`은 GHCR의 빌드된 이미지를 사용하는 일회성 수집 작업입니다.
4. DSM `제어판 → 작업 스케줄러 → 생성 → 예약된 작업 → 사용자 정의 스크립트`에서 매일 1회 실행하도록 설정합니다. 기존 설치는 현재 일정을 유지하고 실제 실행 시각과 시간대는 DSM에서 확인하세요. 저장소에는 NAS 예약 설정이 포함되어 있지 않습니다.
5. 실행 명령에 아래를 입력합니다.

```sh
sh /volume1/docker/apt-gap-monitor/scripts/nas-run.sh
```

예약 실행 시 GHCR에서 최신 Docker 이미지를 먼저 내려받습니다. `collector/` 코드 변경은 이미지 빌드·게시가 완료된 뒤 다음 실행에 반영됩니다. **`scripts/nas-run.sh`와 `compose.yaml` 변경은 이미지에 포함되지 않으므로 NAS 파일도 별도로 갱신해야 합니다.** `.env`는 보존하세요. 관심단지 설정은 GitHub에서 매번 최신본을 읽습니다.

모든 예약·수동 수집은 위 스크립트로 실행하세요. 스크립트는 이미지 pull부터 수집·게시가 끝날 때까지 `.collector.lock` 디렉터리로 중복 실행을 막습니다. `docker compose run`을 직접 실행하면 이 잠금을 우회합니다. `restart: "no"`인 일회성 작업이므로 별도 상시 실행 프로젝트는 필요하지 않습니다.

정상 종료와 일반 명령 실패 시 잠금을 해제합니다. 강제 중단·재부팅·전원 장애 뒤에는 잠금이 남을 수 있습니다. 이 경우 DSM 작업과 수집 컨테이너가 모두 종료됐는지 확인한 뒤 `rmdir /volume1/docker/apt-gap-monitor/.collector.lock`으로 해제하세요. 실행 중인 컨테이너가 있을 수 있어 오래된 잠금을 자동 삭제하지 않습니다.

게시기는 `latest.json`과 마지막 이력의 시각·단지별 가격·갭 일치를 확인하고, 두 파일을 **하나의 커밋**으로 게시합니다. GitHub의 이력을 기준으로 이번 수집분만 추가하고 최근 104회를 유지합니다. NAS 로컬의 오래된 이력이나 이전 게시 실패분은 원격 이력을 덮어쓰거나 복원하지 않습니다. 같은 수집분 재게시는 커밋을 추가하지 않으며, 원격보다 오래된 수집분이나 같은 시각의 다른 내용은 거부합니다. 동시 변경이 생기면 최신 커밋의 설정·데이터를 다시 검증해 최대 3번 시도하고, 계속 충돌하면 실패로 종료합니다. 오류로 원격 JSON 쌍이 이미 불일치하면 자동 덮어쓰기 대신 수동 확인이 필요합니다.

### GitHub Fine-grained PAT

GitHub `Settings → Developer settings → Personal access tokens → Fine-grained tokens`에서 생성합니다.

- Repository access: `Only select repositories → apt-gap-monitor`
- Repository permissions: `Contents → Read and write`
- 나머지 권한: No access

토큰과 국토부 키는 `.env`에만 저장되며 `.gitignore`로 GitHub 업로드에서 제외됩니다.

NAS 수집기는 실행할 때마다 GitHub의 최신 `config/complexes.json`과 `config/asking-prices.json`을 먼저 읽습니다. 따라서 이 버전으로 이미지를 한 번 빌드한 뒤에는 GitHub에서 관심단지만 수정하면 NAS에도 자동 반영됩니다.

## GitHub Actions 수동 진단

1. 이 폴더를 새 GitHub 저장소에 올립니다.
2. 공공데이터포털에서 `국토교통부_아파트 매매 실거래가 자료` 활용신청 후 일반 인증키(Decoding)를 받습니다.
3. 저장소 `Settings → Secrets and variables → Actions`에 `MOLIT_API_KEY`를 추가합니다.
4. `Actions → Update apartment prices → Run workflow`를 한 번 실행합니다.
5. `Settings → Pages → Deploy from a branch`에서 `main /docs`를 선택합니다.

이 워크플로에는 예약 실행이 없습니다. 수동 실행 결과와 오류 파일은 7일간 보관되는 `apartment-diagnostics` 아티팩트로 확인합니다. 실패한 실행의 JSON은 체크아웃 당시 데이터 또는 일부 수집 결과일 수 있으므로 실행 성공 여부와 오류를 함께 확인하세요. Actions는 저장소에 커밋하거나 Pages 데이터를 갱신하지 않습니다. 운영 데이터 게시와 정기 실행은 NAS가 담당합니다.

## 수정 후 검증 및 반영

```sh
python -m unittest discover -s tests -v
sh -n scripts/nas-run.sh
```

테스트는 임시 JSON·가짜 GitHub API·가짜 Docker 명령으로 실행하며 실제 수집이나 운영 게시를 하지 않습니다. 검토 후 변경을 `main`에 반영하고 `Publish NAS collector image` 빌드 성공을 확인한 다음, NAS의 스크립트 등 호스트 파일을 갱신하세요. 실제 DSM 일정은 별도로 확인하며 이 변경만으로 바뀌지 않습니다.

## 관심단지 추가

`config/complexes.json`의 `complexes` 배열에 아래 형식으로 추가합니다.

```json
{"id":"unique-id","region":"지역","name":"표시명","aliases":["API의 단지명","다른 표기"],"lawd_cd":"시군구 5자리","area":84.9}
```

첫 실행 후 거래수가 0이면 공공데이터 응답의 실제 단지명과 `aliases`가 다른 경우가 많습니다. 별칭을 추가하면 됩니다. 면적 허용오차는 파일 상단 `area_tolerance`로 조정합니다.

## 호가 입력

공식 호가 API는 사용하지 않습니다. `config/asking-prices.json`의 `prices`에 단지 ID와 만원 단위 호가를 입력하세요.

```json
{"updated_at":"2026-09-14","prices":{"pangyo5":185000,"home":90000}}
```

## 산식

- 대표가: 최근 6개월 동일면적 실거래 중앙값
- 우선 제외: 1층, 직거래, 해제 거래
- 가격차: 목표단지 대표가 - 현재 집 대표가
- 이력: 최근 104회 데이터를 `docs/data/history.json`에 보존

> 초기 단지명 별칭은 실데이터 첫 실행 후 일부 보정이 필요할 수 있습니다.
