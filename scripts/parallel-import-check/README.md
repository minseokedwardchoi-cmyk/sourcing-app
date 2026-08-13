# parallel-import-check (2026-08-13 이전 세션에서 복구)

병행수입 가능여부 판정 파이프라인 원본. 이전 세션(`e76666e0-2989-4bf8-9cb0-d00e0da04c99`)의
스크래치패드 파일이 이미 정리(삭제)된 뒤였지만, Claude Code가 로컬에 남기는 세션 대화
로그(`~/.claude/projects/.../*.jsonl`)에서 실제 파일 내용을 그대로 복구했다 —
**4개 스크립트 전부 복구 완료**(`build_cross_check_queue.py`는 Write+Edit 4번을 순서대로
재적용해서 재구성함).

## 로직 요약 (4단계)
1. **`parallel_import_check.py`** — 영문 상품명을 `sourcing-app`의
   `/api/internal/english-lookup`으로 검색해서 후보 행을 가져오고(`fetch_candidates`),
   `mode="contains"` 매칭으로 진짜 동일 제품인지 판단(`match_candidates`,
   `match_with_brand_factory_fallback`). 매칭된 행을 **factory 단위**로 묶어서
   distinct 수입업체 수를 센다 — 1곳이면 독점, 2곳 이상이면 병행수입 가능 후보.
   (참고: 한글 `sku_name`은 표기가 제각각이라 매칭 기준에서 제외, 영문 상품명 기준.)
   실행하면 `{type, distributor, rank_col, brand_en, target_en, status, verdicts}` 형태의
   1차 결과 파일(원본에선 `pilot_results.json`/`batch_results_*.json`)을 만든다.
2. **`build_cross_check_queue.py`** — 1차 결과 중 `exclusive`/`parallel_possible`만 골라
   `cross_check.py`의 매칭 함수로 **다시 조회**해서(1차와 같은 계산인지 검증 겸) 애매한
   매칭(느슨한 매칭으로만 걸린 것)만 사람이 검토할 큐(`cross_check_review_items.json`)로
   추려낸다. 동시에 `cross_check_matched_cache.json`(apply 단계에서 API 재조회 없이 쓸
   캐시)을 저장한다.
3. **`cross_check.py`** — 1차 파이프라인의 매칭 함수를 **읽기 전용으로 import**만 해서
   재사용(1차 로직 자체는 절대 수정 안 함). `build_cross_check_queue.py`와
   `apply_cross_check.py` 양쪽에서 공용으로 쓰는 라이브러리 역할.
4. **`apply_cross_check.py`** — 사람이 keep/reject 판단한 결과(`cross_check_decisions.json`,
   dedup_key → true/false)를 반영해서 최종 `revised_status`(exclusive/parallel_possible/
   no_history)를 `cross_check_results.json`으로 계산.

`cross_check_decisions.json.example` — 원본 실행 때 실제로 사람이 내린 판단 41건을
그대로 복구해둔 것(예시/참고용 — 지금 크롤링 이력을 새로 돌리면 대상 자체가 다르므로
그대로 재사용은 안 되고, 형식 참고용).

## ⚠️ 실행 전 확인된 것 / 안 된 것

**확인 완료**:
- `/api/internal/english-lookup` 엔드포인트가 지금도 살아있고 파라미터(`search`, `limit`)도
  `fetch_candidates()`가 보내는 것과 정확히 일치함 — 실제 호출 테스트 통과(Bertolli 검색 시
  `import_history`의 실제 factory/importer 데이터 정상 반환 확인).
- 4개 스크립트 전부 문법 검증(`py_compile`) 통과.
- 경로 하드코딩(`sys.path.insert(...)`, `SCRATCH` 상수)은 전부 이 폴더 기준 상대경로로 수정함.

**아직 안 됨**:
- **실제 실행 검증 자체는 아직 안 함** — 문법/엔드포인트 확인만 했고, 4단계를 실제로
  순서대로 돌려서 끝까지 성공하는지는 미검증.
- `/api/internal/english-lookup` 응답의 한글 필드(importer/category/mc/country/sku_name)가
  깨져서 오는 문제를 발견함(별도 이슈로 분리해둠, 이 파이프라인의 매칭 로직 자체는 영문
  필드만 쓰므로 매칭 정확도엔 영향 없어 보이지만, 최종 결과에 한글 정보를 노출하려면
  그 버그부터 고쳐야 함).

## 크롤링 이력에 적용하려면 (다음 단계, 아직 미착수)
사용자가 원하는 최종 형태는 "크롤링 끝나면 자동으로 병행수입까지 판정"인데, 이 원본
파이프라인은 **1차 자동매칭 + 사람 크로스체크**가 세트로 설계돼 있었다(브랜드검증을
Gemini로 완전자동화한 것과 같은 트레이드오프 — 사람 검토를 빼면 정확도가 원래보다
낮아질 수 있음). 필요한 작업:
1. `parallel_import_check.py`의 입력 소스를 예전 리서치 엑셀 대신
   `product_sourcing_crawl_snapshot_item`(백엔드 `GET /api/product-sourcing/crawl-runs*`,
   `verify_brands_gemini.py`가 브랜드 후보 뽑을 때 쓰는 것과 같은 API)으로 바꾸는 작은
   래퍼 스크립트 작성.
2. 크로스체크(사람 재검토) 단계를 어떻게 할지 결정 — 완전 스킵(정확도 낮음 감수) /
   브랜드검증처럼 AI로 대체 / 진짜 사람이 주기적으로 검토.
3. (선택) `import_history` 한글 필드 인코딩 버그 수정 — 최종 결과에 한글 정보를
   노출하려면 필요.
