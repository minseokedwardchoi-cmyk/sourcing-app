# 브랜드검증 + 원산지판독(Gemini) 자동화 — 크롤링 세션 인수인계

> 이 문서를 크롤링 자동화 작업 중인 세션에 붙여넣어 주세요. 브랜드검증(리콜/품질·표시/
> 법적·평판 리스크)과 원산지판독 파이프라인이 둘 다 이미 만들어져 있고, 크롤링이
> 완성되면 사람 개입 없이 자동으로 이어서 실행됩니다 — 크롤링 쪽에서 알아두면 좋을
> 것들만 정리했습니다. (파일명은 브랜드검증만 있던 때 이름 그대로 유지 — 링크 안 깨지게)

## 요약

기존엔 Claude Code + WebSearch/비전으로 사람이 직접 조사하던 두 작업을 Gemini API로
자동화했습니다:
- **브랜드검증**: 브랜드마다 리콜/품질표시/법적평판 웹서치 → Gemini(`google_search`
  그라운딩). **브랜드 단위** 캐시(`brand_verification`) — 같은 브랜드는 여러 유통사·
  품목유형에서 재사용.
- **원산지판독**: 상품 패키지 사진에서 "Made in ~" 문구 읽기 → Gemini 비전 API.
  **상품(URL) 단위** 캐시(`product_origin_verification`) — 같은 브랜드도 유통사·
  용량마다 원산지가 다를 수 있어서 브랜드 단위로는 재사용 안 함.

둘 다 크롤링 체인이 끝나면 GitHub Actions가 자동으로 이어서 실행됩니다(서로 독립적,
병렬로 돌아도 무방):

```
crawl_walmart_samsclub.yml (VNC, 사람이 캡차)
  → workflow_run →
crawl_amazon_aeon.yml (자동)
  → workflow_run →
verify_brands.yml (Gemini 브랜드검증)
verify_origin.yml (Gemini 원산지판독)
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

그리고 원산지판독:

- **DB**: `product_origin_verification` 테이블 — url(원문)/url_hash(sha256, 유니크
  조회키)/origin_found(Y=실측/E=추정/N=확인불가)/origin_text/note/images_used/
  verification_model/verified_at
- **API**: `POST /api/product-origin-verification/check`(이미 캐시된 url 목록 확인,
  배치), `POST /api/product-origin-verification/upsert`
- **검증 스크립트**: `backend/verify_origin_gemini.py` — 사진(있으면)+상품 컨텍스트를
  Gemini 비전 API 1회 호출(그라운딩 불필요 — 원산지 표시 규정은 정적 지식이라 바로
  구조화 출력 받음)로 판독. 판정 기준(라벨 문구 우선 → 없으면 국가별 추정규칙 → 그래도
  없으면 확인불가)은 원본 수작업 지침을 83개 유형 전체에 맞게 일반화해서 이식.
- **사진 확보**: `product_sourcing_crawl_snapshot_item`에 크롤러가 `image_urls`(복수)를
  안 보내주면, 아마존/이온몰에 한해 이 스크립트가 상품 상세페이지를 `r.jina.ai`로 직접
  다시 읽어서 갤러리 사진을 추가로 뽑아냅니다(검색결과 썸네일 1장보다 후면 라벨이 보일
  확률이 높음). **월마트/샘스클럽은 이 자체보강이 안 됩니다** — 아래 3-1번 참고.
- **워크플로**: `.github/workflows/verify_origin.yml`

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

### 3. ⚠️ 원산지판독용 월마트/샘스클럽 갤러리 사진 — 코드는 이미 짜뒀음, 실행 테스트만 필요
원산지 문구는 대부분 패키지 **뒷면**에 있는데, 검색결과 카드에는 정면 썸네일 1장만
있습니다. 아마존/이온몰은 `verify_origin_gemini.py`가 상세페이지를 `r.jina.ai`로 스스로
다시 읽어서 사진을 보강하지만, 월마트/샘스클럽은 봇차단 때문에 그게 안 통할 가능성이
높아서(과거 이 프로젝트에서 r.jina.ai를 월마트/샘스클럽에 시도했다가 막힌 이력 있음),
**크롤러가 직접 상세페이지를 방문해서 갤러리 사진을 수집하는 기능을 이번에 추가했습니다**
(`scrapers/walmart.js`의 `scrapeWalmartProductImages`, `scrapers/samsclub.js`의
`scrapeSamsClubProductImages`, `crawl.js`의 `--gallery-limit=N` 옵트인 플래그,
`crawl_walmart_samsclub.yml`의 `gallery_limit` 입력값 — 자세한 사용법은
`scripts/crawl-walmart-samsclub/README.md`의 "원산지판독용 갤러리 사진 수집" 절 참고).

**⚠️ 이 코드는 실행 자체를 한 번도 못 해봤습니다** — 이 세션 환경엔 브라우저/VNC 접근이
없어서 상세페이지 실제 마크업을 볼 수가 없었고, 그래서 후보 selector 여러 개를 순서대로
시도하는 방어적 구현으로만 짜뒀습니다. **다음에 월마트/샘스클럽 크롤링을 VNC로 직접
돌릴 때(대시보드 버튼 말고 `workflow_dispatch`로 `gallery_limit`을 5 정도로 수동
실행), 로그에서 "갤러리 N장 확보" 메시지가 실제로 몇 장씩 잡히는지 꼭 확인해주세요** —
0장만 계속 나오면 selector가 실제 마크업과 안 맞는 거라 보강이 필요합니다. 대시보드
"최신화" 버튼으로 트리거되는 정기 실행은 `gallery_limit` 기본값이 0(꺼짐)이라 이 기능
때문에 기존 크롤링이 느려지거나 캡차가 늘어나는 일은 없습니다 — 수동으로 켜서 검증하기
전까진 안전합니다.

### 4. 아마존은 상대적으로 안전해 보임
`scrapers/amazon.js`는 상품 제목과 별개로 마크다운의 "## 브랜드명" 헤딩에서 brand를
따로 뽑는 로직이 있어서, 올리브유 전용이 아니라 구조적으로 더 일반화된 방식입니다
(실측 검증까진 안 했지만 코드상 그렇게 보임).

### 5. 크롤링 워크플로 이름을 바꾸면 트리거가 깨짐
`verify_brands.yml`, `verify_origin.yml` 둘 다 `workflow_run: workflows: ["아마존/이온몰
순위 크롤링"]`으로 `crawl_amazon_aeon.yml`의 `name:` 필드를 정확히 문자열 매칭합니다.
이 워크플로 이름을 바꾸거나 체인 마지막 단계를 다른 워크플로로 바꾸게 되면, 두 워크플로
전부 이 부분을 같이 고쳐줘야 자동 체인이 유지됩니다.

### 6. product_sourcing_item(메인페이지)은 전혀 안 건드림
`brand_verification`, `product_origin_verification` 둘 다 완전히 독립된 캐시입니다.
크롤링 결과를 메인 테이블로 승격하는 로직(아직 없음, 다른 세션 작업으로 알고 있음)을
만들 때, 그 로직에서 브랜드별로 `brand_verification`을(현재는 `GET
/api/brand-verification/keys`만 있고 개별 조회용 GET은 없음 — 필요하면 그때 추가하면
됨), 상품 URL별로 `product_origin_verification`을 조회해서 각각 `recall_status` 등
5개 컬럼 / `origin` 컬럼을 채우면 재검증 없이 바로 반영됩니다.

### 7. 크롤링 이력에 테스트 더미 데이터 하나 남아있음
`product_sourcing_crawl_snapshot_item`에 `brand="테스트브랜드"`, `product_name_en="Test"`
행이 1건 있습니다(크롤러 개발 중 테스트로 남은 것으로 추정). 지워도 되고 안 지워도
브랜드검증/원산지판독이 이걸 조사해서 캐시에 의미없는 항목 하나 남기는 정도라 치명적이진
않습니다.

## 확인 완료된 것

- `brand_verification` 캐시: 기존 1,926개 브랜드 백필 완료
- `verify_brands.yml` GitHub Actions 왕복(백엔드 HTTP API 호출) 정상 동작 확인(dry-run)
- `GEMINI_API_KEY`, `BACKEND_URL` GitHub Secrets 등록 완료

## 아직 안 한 것 (이 세션 범위 밖)

- Gemini 실전 대량 검증/판독 실행 (지금까진 dry-run·소량 테스트만, `verify_origin_gemini.py`는
  아직 실행 자체를 한 번도 안 해봄 — r.jina.ai 상세페이지 재읽기 부분이 특히 미검증)
- 이온몰 brand 필드 채우기 (위 1번)
- 월마트/샘스클럽 갤러리 사진 수집 코드 실행 검증 (위 3번, 코드는 있음 — 실행이 안 됐을 뿐)
- 크롤링 결과 → `product_sourcing_item` 승격 로직
