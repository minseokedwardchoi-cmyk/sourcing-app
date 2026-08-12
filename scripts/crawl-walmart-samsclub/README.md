# crawl-walmart-samsclub (Part B — B1 구현됨)

월마트/샘스클럽은 PerimeterX "Press & Hold" 봇차단이 있어서 아마존/이온몰처럼 완전 무인
자동화가 불가능하다. 대신 **GitHub Actions 러너 안에서 headed 브라우저를 띄우고, 그 화면을
VNC로 공유해서 사람이 캡차만 직접 눌러주는** 반자동 방식을 쓴다.

**B0 스파이크(실현가능성 검증)는 2026-08-12 통과함** — GitHub Actions 러너 IP에서도 사람이
VNC로 직접 캡차를 눌러 통과시킬 수 있는 것까지 실측 확인됨. `_spike_vnc_walmart.yml`은 이제
삭제되고 아래 정식 워크플로(`crawl_walmart_samsclub.yml`)로 대체됐다.

## 흐름 (B1)

1. 대시보드 "최신화" 버튼 클릭 → 백엔드가 GitHub Actions(`crawl_walmart_samsclub.yml`)를 트리거
2. 워크플로가 Xvfb + headed Chromium + x11vnc(비밀번호 없음, B0에서 확인된 이유 — 로그/주소창이
   `password=` 값을 자동 마스킹해서 사람이 못 꺼내는 문제가 있었음) + noVNC + cloudflared 터널을 띄우고,
   준비되면 백엔드 `session-callback`을 호출
3. 프론트가 폴링 중이던 대시보드에 "지금 접속해서 캡차 풀어주세요" 배너 + 링크가 뜸
4. `crawl.js`가 headed 브라우저 **하나를 계속 재사용**하면서 83개 유형 × (월마트/샘스클럽)을 순회 —
   캡차가 뜰 때마다 `human-check.js`(최대 90초 대기)가 사람이 눌러줄 때까지 기다림
5. 끝나면 `upload.js`가 결과를 백엔드 `POST /api/product-sourcing/crawl-snapshot`에 업로드(아마존/
   이온몰과 같은 엔드포인트 재사용, `product_sourcing_item`은 안 건드림) → `session-finished` 호출
6. 이 워크플로가 끝나면 `crawl_amazon_aeon.yml`이 `workflow_run`으로 자동 이어서 실행됨

## 파일
```
crawl.js               # 메인 크롤 루프 — 브라우저 하나 재사용, type-query-map.csv(crawl-product-sourcing과 공유) 순회
upload.js               # results.jsonl → 백엔드 /api/product-sourcing/crawl-snapshot
vnc-spike.js            # B0 전용 — 이제 안 쓰임(B1로 대체), 참고용으로만 남겨둠
scrapers/walmart.js      # oliveoil-scraper에서 이식
scrapers/samsclub.js     # oliveoil-scraper에서 이식 (실제 마크업 미검증 — selector 보강 필요할 수 있음)
scrapers/human-check.js  # 캡차 뜨면 최대 90초 사람 대기 로직
package.json             # playwright 의존성
```

## 로컬 실행 (참고용 — 실제로는 워크플로가 자동으로 돌림)
```bash
cd scripts/crawl-walmart-samsclub
node crawl.js --site=all --limit=40 --results=results.jsonl
BACKEND_URL=https://sourcing-backend-ucp5.onrender.com node upload.js --results=results.jsonl
```
