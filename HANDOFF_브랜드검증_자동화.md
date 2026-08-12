# 브랜드검증(Gemini) 자동화 — 크롤링 세션 인수인계

> 이 문서를 크롤링 자동화 작업 중인 세션에 붙여넣어 주세요. 브랜드검증(리콜/품질·표시/
> 법적·평판 리스크) 파이프라인은 이미 만들어져 있고, 크롤링이 완성되면 사람 개입 없이
> 자동으로 이어서 실행됩니다 — 크롤링 쪽에서 알아두면 좋을 것들만 정리했습니다.

## 요약

기존엔 Claude Code + WebSearch로 브랜드마다 사람처럼 조사해서 엑셀/CSV에 채워넣던
브랜드검증(`brand_verify.csv` 방식)을, Gemini API(웹검색 그라운딩)로 자동화했습니다.
브랜드 단위 캐시 테이블(`brand_verification`)에 브랜드 1건당 1번만 검증해서 쌓고,
크롤링 체인이 끝나면 GitHub Actions가 자동으로 이어서 실행됩니다:

```
crawl_walmart_samsclub.yml (VNC, 사람이 캡차)
  → workflow_run →
crawl_amazon_aeon.yml (자동)
  → workflow_run →
verify_brands.yml (Gemini 브랜드검증, 이번에 추가됨)
```

## 이미 만들어진 것

- **DB**: `brand_verification` 테이블 (`backend/models.py`) — brand_key(정규화된 브랜드명,
  유니크), recall_status/quality_label_status/legal_risk_status/five_year_issue/notes/
  sources/verification_model/verified_at
- **API**: `GET /api/brand-verification/keys`, `POST /api/brand-verification/upsert`
  (`backend/main.py`) — GitHub Actions는 이 두 엔드포인트로만 DB에 접근 (다른 크롤링
  워크플로와 동일하게 DATABASE_URL 직접 접근 없음)
- **검증 스크립트**: `backend/verify_brands_gemini.py` — 크롤링 이력(아래 참고)에서
  브랜드를 뽑아 Gemini(`google_search` 그라운딩, 2단계 호출)로 조사 후 캐시에 upsert.
  판정 기준은 기존 수작업 지침(확인필요 지양, 뚜렷한 부정적 근거 없으면 기본 통과 등)
  그대로 이식됨
- **백필**: `backend/backfill_brand_verification.py` — 기존 `product_sourcing_item`에
  이미 채워져 있던 브랜드검증 데이터 1,926개 브랜드를 캐시로 이전 완료(로컬 1회 실행,
  이미 완료됨)
- **워크플로**: `.github/workflows/verify_brands.yml`

## 크롤링 쪽에서 알아둘 것 (중요도순)

### 1. ⚠️ 이온몰(AEON) — brand 필드가 아직 없음
`scripts/crawl-product-sourcing/scrapers/aeon.js`에서 두 파싱 경로(JP/MY) 모두
`brand: null`로 하드코딩돼 있습니다. 지금 상태로는 **이온몰 크롤링 결과가 브랜드검증
대상에서 통째로 빠집니다**(에러는 안 남, 그냥 후보 0건). 이온몰 쪽에 브랜드 추출 로직을
추가하면, 브랜드검증 파이프라인은 코드 수정 없이 자동으로 그 브랜드들을 집어서 검증합니다
— `product_sourcing_crawl_snapshot_item.brand` 컬럼만 채워지면 됩니다.

### 2. ⚠️ 월마트/샘스클럽 — brand 추출이 올리브유 전용 휴리스틱
`scripts/crawl-walmart-samsclub/scrapers/walmart.js`, `samsclub.js`의 `parseBrand()`가
"상품명에서 특정 stop-word(Extra/Virgin/Olive/Oil/Organic/Cooking/Smooth/Robust/Pure/
First/Cold 등) 나오기 전까지 최대 3단어"를 브랜드로 취급합니다. 이 stop-word 목록이
올리브유 상품명 기준으로 짜여있어서, **83개 유형 전체로 확장되면 다른 품목(예: 시리얼,
과자)에서는 상품명 앞 1~3단어를 그냥 브랜드로 오인할 가능성이 높습니다**(브랜드검증
쪽에서는 치명적이지 않음 — 엉뚱한 "브랜드"가 캐시에 쌓이는 정도지 에러는 안 남 — 하지만
크롤링 데이터 품질 자체에는 영향이 있으니 83개 유형 전체 크롤링 전에 한 번 봐주시면
좋습니다).

### 3. 아마존은 상대적으로 안전해 보임
`scrapers/amazon.js`는 상품 제목과 별개로 마크다운의 "## 브랜드명" 헤딩에서 brand를
따로 뽑는 로직이 있어서, 올리브유 전용이 아니라 구조적으로 더 일반화된 방식입니다
(실측 검증까진 안 했지만 코드상 그렇게 보임).

### 4. 크롤링 워크플로 이름을 바꾸면 트리거가 깨짐
`verify_brands.yml`은 `workflow_run: workflows: ["아마존/이온몰 순위 크롤링"]`으로
`crawl_amazon_aeon.yml`의 `name:` 필드를 정확히 문자열 매칭합니다. 이 워크플로 이름을
바꾸거나 체인 마지막 단계를 다른 워크플로로 바꾸게 되면, `verify_brands.yml`의 이 부분도
같이 고쳐줘야 자동 체인이 유지됩니다.

### 5. product_sourcing_item(메인페이지)은 전혀 안 건드림
`brand_verification`은 완전히 독립된 캐시입니다. 크롤링 결과를 메인 테이블로 승격하는
로직(아직 없음, 다른 세션 작업으로 알고 있음)을 만들 때, 그 로직에서 브랜드별로
`brand_verification`을 조회해서(현재는 `GET /api/brand-verification/keys`만 있고
개별 조회용 GET은 없음 — 필요하면 그때 추가하면 됨) `recall_status` 등 5개 컬럼을
채우면 재검증 없이 바로 반영됩니다.

### 6. 크롤링 이력에 테스트 더미 데이터 하나 남아있음
`product_sourcing_crawl_snapshot_item`에 `brand="테스트브랜드"`, `product_name_en="Test"`
행이 1건 있습니다(크롤러 개발 중 테스트로 남은 것으로 추정). 지워도 되고 안 지워도
브랜드검증이 이걸 조사해서 캐시에 의미없는 항목 하나 남기는 정도라 치명적이진 않습니다.

## 확인 완료된 것

- `brand_verification` 캐시: 기존 1,926개 브랜드 백필 완료
- `verify_brands.yml` GitHub Actions 왕복(백엔드 HTTP API 호출) 정상 동작 확인(dry-run)
- `GEMINI_API_KEY`, `BACKEND_URL` GitHub Secrets 등록 완료

## 아직 안 한 것 (이 세션 범위 밖)

- Gemini 실전 대량 검증 실행 (지금까진 dry-run·소량 테스트만)
- 이온몰 brand 필드 채우기 (위 1번)
- 크롤링 결과 → `product_sourcing_item` 승격 로직
