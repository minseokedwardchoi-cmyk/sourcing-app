# 로딩 속도 개선 작업 — 진행 상황 (2026-07-27)

다른 세션에서 이어갈 수 있도록 지금까지의 조사·조치·미해결 이슈를 정리한 문서.
작업 다 끝나면 이 파일은 지워도 됨.

## 배경

사용자 보고: 사이트가 개느림 — 첫 화면 리스트, 검색, 국가/제조사 상세 페이지 전부
10초~30초 이상, 심할 땐 타임아웃. 로컬 데이터 재확인 없이 실서비스(Render)에서
직접 진단·수정·배포하며 진행함.

## 인프라 구성 (이번에 확인된 사실)

- Render 프로젝트 "My project" 안에 서비스 3개:
  - `sourcing-backend` (웹서비스, FastAPI, Docker, Free 티어) — https://sourcing-backend-ucp5.onrender.com
  - `sourcing-db` (PostgreSQL 18, Free 티어)
  - `sourcing-app` (프론트엔드로 추정, 11일 전 업데이트)
- 별도(Ungrouped) 서비스 `sourcing-e5-embedding` — **Failed deploy 상태 (11일째)**.
  하이브리드(의미) 검색용 원격 임베딩 서비스로 추정됨. 이게 죽어있어서 검색어가 있는
  모든 검색이 임베딩 호출 실패 후 폴백하기까지 오래 걸리는 문제가 있었음 (아래 참고).
- Render 무료 웹서비스: 유휴 시 spin down, 첫 요청 콜드스타트 50초+ (Render 자체 배너로 확인).
- DB 커넥션 풀: `pool_size=3, max_overflow=2` (총 5개) — [database.py](backend/database.py).
  0.1 vCPU / 512MB RAM 무료 인스턴스 기준으로 일부러 작게 잡아둔 값.
- **중요 발견**: 오늘 세션 초반, `main` 브랜치에 여러 번 push했는데도 Render
  `sourcing-backend`의 Live 커밋이 5일 전(`7983ffb`)에 머물러 있었음 — Auto-Deploy가
  꺼져 있었거나 안 걸리고 있었던 것으로 보임. 사용자가 이번에 **Auto-Deploy를
  "On Commit"으로 변경함** (Render dashboard → sourcing-backend → Settings). 이제부터는
  push하면 자동 배포됨.
- Postgres 접속 정보(External Connection String)는 Render dashboard →
  `sourcing-db` → **Connect** 탭에서 확인 가능. (이 문서엔 비밀번호를 적지 않음 —
  아까 스크린샷으로 한 번 노출됐으니 Render에서 재발급 권장.)

## 지금까지 적용한 변경 (커밋 순서, 전부 `main`에 push됨)

1. **`ranking.py` 랭킹 계산 결과 캐싱** — 국가/제조사 상세 페이지의 top5/수입횟수/
   성장추세 등급·종합점수 계산이 매 요청마다 `import_history` 원본을 다시 GROUP BY
   하던 것을, 국가별/제조사별로 캐싱. 업로드·크롤링 후 `refresh_mvs()` 시점에만
   무효화. **안전하게 적용됨, 유지.**
2. ~~`hybrid_search.py` market_status_mv 조인을 페이지네이션 이후로 미루는 시도~~ —
   운영에서 오히려 타임아웃 유발(Postgres가 나쁜 실행계획 선택한 것으로 추정).
   **롤백 완료, 원래 조인 순서로 복구됨.**
3. **`hybrid_search.py` 검색어 없는 기본 리스트 응답 캐싱** — 정렬/페이지/필터 조합별로
   전체 응답을 캐싱. 서버 기동 시(`_startup_bg`) 기본 1페이지를 미리 한 번 계산해
   캐시 예열. **적용 확인됨 — 두 번째 요청부터 0.3~0.5초로 확인.**
4. **`hybrid_embeddings.py` 원격 임베딩 서비스 서킷 브레이커** — 임베딩 서비스가
   죽어있으면 검색 한 번에 재시도 3회 x 타임아웃(30초)까지 다 채워 최대 90초 가까이
   걸리던 것을, 첫 실패 후 90초간 재시도 생략하고 즉시 폴백하도록 수정. **배포는 됐으나
   아래 이슈 때문에 실제 효과 검증은 아직 못 함.**

## 🔴 미해결 — 지금 막혀있는 지점

**DB 커넥션 풀 고갈**: `/api/search-hybrid`에 검색어를 넣으면 20분 넘게 계속
아래 에러로 500이 남:

```
TimeoutError: QueuePool limit of size 3 overflow 2 reached, connection timed out, timeout 10.00
```

- 검색어 없는 기본 리스트(`/api/search-hybrid?page=1`)는 캐시 덕분에 정상(0.3~0.5초).
- 검색어 있는 요청만 커넥션을 못 받고 타임아웃 — 뭔가(아마 MV refresh 또는 시작 시
  캐시 예열 쿼리, 혹은 둘 다)가 커넥션을 오래 붙잡고 있는 것으로 추정.
- 20분 그냥 기다려도 안 풀림 → 시간이 지나면 자연 해소되는 일시적 부하가 아니라
  뭔가 붙잡힌 채 안 풀리는 상태로 보임.
- **다음 세션에서 제일 먼저 할 일**: Render dashboard → `sourcing-backend` →
  우측 상단 메뉴에서 **"Restart service"** 실행 (재배포 아니고 단순 재시작 —
  새 빌드 없이 지금 붙잡힌 커넥션들만 정리됨). 그 다음 검색(`/api/search-hybrid?search=올리브유...`)
  다시 테스트.
- 재시작 후에도 재발하면: `pg_stat_activity`로 실제 오래 걸리는 쿼리를 찾아야 함
  (사용자가 psql 접속을 시도했으나 사용자명이 화면에서 잘려서 실패함 — Render
  Connect 탭에서 전체 External Connection String을 다시 확인 필요).

## 남은 작업 우선순위

1. **[긴급] `sourcing-backend` 재시작** → 커넥션 풀 정리
2. 재시작 후 검색(`올리브유` 등) 정상 속도 확인 — 서킷 브레이커가 실제로 도는지 검증
3. 국가별 제조사 목록 페이지(`/api/countries/{country}/manufacturers`)는 랭킹 점수만
   캐싱됐고, 그 외 집계 쿼리(제조사별 SKU/수입건수, MC 목록)는 아직 캐싱 안 됨 —
   같은 국가 재방문 시에도 느리면 이 부분도 캐싱 검토
4. `sourcing-e5-embedding` 서비스 자체가 왜 "Failed deploy"인지 원인 파악 및 복구
   (지금은 서킷 브레이커로 느려지는 것만 막았지, 의미 검색 기능 자체는 안 되는 상태로 추정)
5. keep-warm 워크플로(`.github/workflows/keep_warm.yml`)가 실제로 작동하는지 확인
   (GitHub Actions 탭에서 최근 실행 기록 확인) — 무료 티어 콜드스타트(50초+) 방지 목적으로
   있으나 오늘 밤 계속 콜드스타트 증상이 있었어서 제대로 도는지 재확인 필요
