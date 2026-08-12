# crawl-product-sourcing

아마존/이온몰에서 83개 상품유형(`type-query-map.csv`)별 베스트셀러 순위를 크롤링해서, 백엔드의
**이력(history) 테이블**(`product_sourcing_crawl_run` / `product_sourcing_crawl_snapshot_item`)에
매 회차(run)로 쌓는다. **메인페이지가 참조하는 `product_sourcing_item` 테이블은 건드리지 않는다** —
수작업으로 채운 원산지/병행수입/HS코드 등 검증 데이터가 이 스크립트로 인해 사라지는 일은 없다.

`D:\AI 프로젝트\유통사크롤러\amazon-aeon-enrich`에서 실전 검증된 `r.jina.ai` 리더 프록시 방식
스크래퍼(`scrapers/amazon.js`, `scrapers/aeon.js`)를 그대로 이식했다 — Playwright 불필요, 순수 fetch라
GitHub Actions에서도 동작할 것으로 예상되지만, **실제 GitHub Actions 러너에서 r.jina.ai 아웃바운드가
되는지는 아직 실측 전**이다 (기존 로컬 개발환경의 클라우드 샌드박스는 막혀 있었음).

## 로컬 실행

```bash
cd scripts/crawl-product-sourcing
node crawl.js --site=all --limit=40 --results=results.jsonl
BACKEND_URL=https://sourcing-backend-ucp5.onrender.com node upload.js --results=results.jsonl
```

- `--site=amazon|aeon|all` (기본 all)
- `--limit=N` (기본 40, 유형당 상위 N개)
- `--only=텍스트` (product_type 또는 대분류에 포함된 텍스트로 필터 — 소량 테스트용)
- `crawl.js`는 `results.jsonl`에 한 줄씩 append하고, 이미 기록된 (유형, 유통사) 조합은 재실행 시
  자동 스킵한다 — 중단 후 재시작 안전.
- `upload.js`는 `results.jsonl`을 통째로 읽어 백엔드에 한 번의 배치로 POST한다 (여러 번 실행해도
  매번 새 run으로 쌓이므로, 같은 결과를 두 번 업로드하지 않도록 주의).

## GitHub Actions

`.github/workflows/crawl_product_sourcing_monthly.yml` — 매달 1일 자동 실행 + `workflow_dispatch`로
수동/소량 테스트 가능 (`limit`, `site`, `only` 입력값 지원).

## 파일 구조
```
crawl.js              # 메인 크롤 루프 (type-query-map.csv → results.jsonl)
upload.js              # results.jsonl → 백엔드 /api/product-sourcing/crawl-snapshot
csv.js                 # 의존성 없는 최소 CSV 파서
fx.js                  # JPY/MYR → USD 환율 조회 (아마존 이외 통화 가격 환산용)
scrapers/amazon.js      # amazon-aeon-enrich에서 이식 (r.jina.ai 기반)
scrapers/aeon.js        # amazon-aeon-enrich에서 이식 (일본 shop.aeon.com + 말레이시아 myaeon2go.com)
translate.js            # aeon.js가 검색어를 일본어로 번역할 때 사용
type-query-map.csv      # 83개 유형별 대분류/product_type/카테고리 검색어 매핑
```
