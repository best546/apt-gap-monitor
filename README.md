# 아파트 갈아타기 스프레드 모니터

국토교통부 아파트 매매 실거래 API를 매주 수집해 현재 집과 목표 단지의 가격 차이를 GitHub Pages에서 보여줍니다.

## 설치

1. 이 폴더를 새 GitHub 저장소에 올립니다.
2. 공공데이터포털에서 `국토교통부_아파트 매매 실거래가 자료` 활용신청 후 일반 인증키(Decoding)를 받습니다.
3. 저장소 `Settings → Secrets and variables → Actions`에 `MOLIT_API_KEY`를 추가합니다.
4. `Actions → Update apartment prices → Run workflow`를 한 번 실행합니다.
5. `Settings → Pages → Deploy from a branch`에서 `main /docs`를 선택합니다.

이후 매주 월요일 오전 7:15(KST)에 자동 갱신됩니다. Actions 화면에서 언제든 수동 실행할 수도 있습니다.

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
