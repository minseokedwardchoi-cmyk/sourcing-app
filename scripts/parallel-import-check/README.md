# parallel-import-check (2026-08-13 이전 세션에서 복구)

병행수입 가능여부 판정 파이프라인 원본. 이전 세션(`e76666e0-2989-4bf8-9cb0-d00e0da04c99`)의
스크래치패드 파일이 이미 정리(삭제)된 뒤였지만, Claude Code가 로컬에 남기는 세션 대화
로그(`~/.claude/projects/.../*.jsonl`)에서 실제 파일 내용을 그대로 복구했다.

## 로직 요약
1. **`parallel_import_check.py`** — 영문 상품명을 `sourcing-app`의
   `/api/internal/english-lookup`으로 검색해서 후보 행을 가져오고(`fetch_candidates`),
   `mode="contains"` 매칭으로 진짜 동일 제품인지 판단(`match_candidates`,
   `match_with_brand_factory_fallback`). 매칭된 행을 **factory 단위**로 묶어서
   distinct 수입업체 수를 센다 — 1곳이면 독점, 2곳 이상이면 병행수입 가능 후보.
   (참고: 한글 `sku_name`은 표기가 제각각이라 매칭 기준에서 제외, 영문 상품명 기준.)
2. **`cross_check.py`** — 1차 파이프라인의 매칭 함수를 **읽기 전용으로 import**만 해서
   재사용(1차 로직 자체는 절대 수정 안 함). 느슨한 매칭(공백무시/토큰집합/factory폴백)
   으로만 걸린 애매한 매칭을 사람이 검토할 큐로 분류.
3. **`apply_cross_check.py`** — 사람이 keep/reject 판단한 결과(`cross_check_decisions.json`)를
   반영해서 최종 `revised_status`(exclusive/parallel_possible/no_history)를 계산.

## ⚠️ 복구 상태 — 아직 이대로 실행은 안 됨

- **누락된 스크립트**: `cross_check.py` 주석에 언급된 `build_cross_check_queue.py`
  (매칭 재조회 + `cross_check_matched_cache.json` 생성)가 이 세션 로그에서는 못 찾음 —
  다른 세션 로그에 있을 수 있음, 필요하면 더 찾아볼 것.
- **누락된 중간 데이터**: `cross_check_matched_cache.json`, `cross_check_decisions.json`,
  `batch_results_*.json` 등은 코드가 아니라 실행 산출물이라 로그에서 복구가 안 됨 —
  파이프라인을 다시 돌려야 새로 생김.
- **경로 하드코딩 정리**: 원본의 `sys.path.insert(...)`가 옛 스크래치패드 절대경로였던
  것만 이 폴더 기준 상대경로로 고쳤음 — 그 외 로직은 원본 그대로(문법 검증만 통과 확인,
  실행 검증은 아직 안 함).
- **`API_BASE`가 `/api/internal/english-lookup`을 호출** — 이 엔드포인트가 지금
  `backend/main.py`에 그대로 있는지 확인 필요(안 만졌으면 있을 가능성 높음, 확인 전).

## 다음 결정할 것 (구현 전 논의 필요)
사용자가 원하는 최종 형태는 "크롤링 끝나면 자동으로 병행수입까지 판정"인데, 이 원본
파이프라인은 **1차 자동매칭 + 사람 크로스체크**가 세트로 설계돼 있었다(브랜드검증을
Gemini로 완전자동화한 것과 같은 트레이드오프 — 사람 검토를 빼면 정확도가 원래보다
낮아질 수 있음). 크롤링 이력(`product_sourcing_crawl_snapshot_item`) 데이터를 대상으로
돌리려면 `fetch_candidates`가 지금 무엇을 검색 대상으로 삼는지(기존 `import_history`
API인지)부터 확인하고, 크로스체크 단계를 완전자동으로 스킵할지/브랜드검증처럼 AI 재검토를
넣을지 결정이 필요하다.
