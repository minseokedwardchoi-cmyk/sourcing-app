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
   캡차가 뜰 때마다 `human-check.js`(최대 180초 대기)가 사람이 눌러줄 때까지 기다림. VNC로 마우스
   "누르고 있기" 조작 시 타이밍이 매끄럽게 전달 안 돼서 PerimeterX가 여러 번 재시도를 요구하는
   경우가 실측으로 확인됨(막힌 건 아니고 번거로운 정도 — 결국 통과됨) — 180초는 이걸 감안한 여유.
5. 끝나면 `upload.js`가 결과를 백엔드 `POST /api/product-sourcing/crawl-snapshot`에 업로드(아마존/
   이온몰과 같은 엔드포인트 재사용, `product_sourcing_item`은 안 건드림) → `session-finished` 호출
6. 이 워크플로가 끝나면 `crawl_amazon_aeon.yml`이 `workflow_run`으로 자동 이어서 실행됨

## 파일
```
crawl.js               # 메인 크롤 루프 — 브라우저 하나 재사용, type-query-map.csv(crawl-product-sourcing과 공유) 순회
upload.js               # results.jsonl → 백엔드 /api/product-sourcing/crawl-snapshot
scrapers/walmart.js      # oliveoil-scraper에서 이식
scrapers/samsclub.js     # oliveoil-scraper에서 이식 (실제 마크업 미검증 — selector 보강 필요할 수 있음)
scrapers/human-check.js  # 캡차 뜨면 최대 180초 사람 대기 로직
package.json             # playwright 의존성
```

## 원산지판독용 갤러리 사진 수집 (`--gallery-limit`, 옵션)

원산지 표시 문구는 대부분 패키지 **뒷면**에 있는데, 검색결과 카드에는 정면 썸네일 1장만
있다. `verify_origin_gemini.py`(백엔드, 원산지판독 파이프라인)가 아마존/이온몰은
`r.jina.ai`로 상세페이지를 스스로 다시 읽어 사진을 보강하지만, 월마트/샘스클럽은
봇차단 때문에 그 방식이 안 통할 가능성이 높다(과거 이 프로젝트에서 이미 시도했다가
막힌 이력 있음). 그래서 크롤러가 직접 상세페이지를 방문해서 갤러리 사진을 같이
수집하도록 `scrapeWalmartProductImages` / `scrapeSamsClubProductImages`
(`scrapers/*.js`)를 추가했다.

**기본값은 꺼져있음(0)** — 상세페이지 방문이 늘어나는 만큼 캡차가 더 자주 뜰 수 있고
실행 시간도 길어져서, 기존에 이미 검증된 기본 크롤 흐름에 영향 안 주려고 옵트인으로
뒀다. 켜려면:

```bash
node crawl.js --site=all --limit=40 --gallery-limit=5 --results=results.jsonl
```

`--gallery-limit=5`면 유형×사이트 조합마다 순위 상위 5개 상품만 상세페이지까지
들어가서 사진을 추가로 긁는다(전체를 다 긁으면 방문 횟수가 너무 많아짐). 수집된
사진은 `item.imageUrls`에 담기고, `upload.js`가 이걸 `image_urls`(배열)로 백엔드에
같이 올린다 — 있으면 원산지판독 스크립트가 자체 보강 없이 바로 사용한다.

⚠️ **`scrapeWalmartProductImages`/`scrapeSamsClubProductImages`는 실제 상세페이지
마크업으로 검증된 적이 없다**(이 코드를 작성한 환경엔 브라우저 접근이 없어서 실행
자체를 못 해봄) — 후보 selector 여러 개를 순서대로 시도하는 방어적 구현이라 최악의
경우 사진을 0장 못 찾을 수 있다(에러는 안 남, 그냥 `image_urls`가 비어서 백엔드가
썸네일 1장으로만 판독). 처음 켜서 돌려보고 로그에서 "갤러리 N장 확보" 건수를 보고
selector 보강이 필요한지 판단할 것.

## 로컬 실행 (참고용 — 실제로는 워크플로가 자동으로 돌림)
```bash
cd scripts/crawl-walmart-samsclub
node crawl.js --site=all --limit=40 --results=results.jsonl
BACKEND_URL=https://sourcing-backend-ucp5.onrender.com node upload.js --results=results.jsonl
```
