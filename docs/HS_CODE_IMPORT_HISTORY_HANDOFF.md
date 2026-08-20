# SKU/OEM 수입이력 HS코드/원가 — 이어서 작업하기 위한 가이드

브랜치: `claude/sku-oem-hs-code-cost-6sfk1r` (커밋 `55e080b`)

## 배경

메인페이지(`product_sourcing_item`)에는 이미 HS코드 업로드 → 관세율 조회 →
MFDS 평균단가 기반 원가 자동계산이 구현되어 있다. 이번 세션에서 그 로직을
"SKU/OEM 수입이력" 페이지(`import_history` 테이블, `MainDashboard`/
`FactoryViewDashboard` 컴포넌트)에도 동일하게 적용하는 **인프라**를 미리
만들어뒀다. 남은 건 **HS코드 파일을 실제로 업로드해서 매칭시키는 부분**뿐이다.

## 이번 세션에서 이미 해둔 것

1. **DB 컬럼 추가** (`backend/models.py`의 `ImportHistory`, `backend/main.py`
   startup 마이그레이션): `hs_code`, `hs_code_confidence`, `unit`,
   `tariff_rate_pct`, `tariff_basis`, `estimated_landed_cost_krw`,
   `landed_cost_is_per_kg`
2. **원가 자동계산 함수**: `backend/main.py`의
   `_recompute_and_store_import_history_cost_estimates()` — hs_code가 채워진
   행을 `product_type + country(원산지 대신) + unit`으로 관세율/MFDS
   평균단가를 조회해 원가를 계산하고 캐시 컬럼에 저장. 메인테이블의
   `_recompute_and_store_cost_estimates()`와 완전히 동일한 로직
   (`cost_estimator.py`, `mfds_pricing.py` 그대로 재사용).
3. **집계 파이프라인 전파**: `sku_history_mv`(구체화 뷰), `hybrid_search.py`
   (검색 API), `/api/factory-view` 전체에 새 컬럼을 `MAX()`로 통과시킴 —
   SKU 단위 속성이라 그룹 내에서 항상 하나의 값만 존재한다고 가정.
4. **수동 편집 API**: `PATCH /api/import-history/hs-code` — body
   `{sku_name, hs_code}`, 같은 SKU명의 모든 행에 일괄 반영 + 자동 재계산 +
   MV 리프레시. 관세율표 재업로드(`/api/upload-tariff-rates`) 시에도 이
   테이블 원가가 같이 재계산되도록 연결해둠.
5. **프론트**: `ALL_COLS`(App.jsx)에 "HS코드"/"원가" 열 추가.
   `ImportHistoryHsCodeCell`(인라인 입력, sku_name 기준 저장) +
   `EstimatedCostCell`(메인테이블 것 그대로 재사용) 컴포넌트로 렌더링.
   `MainDashboard`, `FactoryViewDashboard` 양쪽 테이블에 다 적용됨.

## 아직 안 한 것 — 이번에 할 일

**HS코드 파일 대량 업로드 → `import_history` 매칭 반영.**

메인테이블의 `hs_code_importer.py`(`import_hs_codes()`)가 참고 템플릿이다:
- 엑셀을 읽어서 (거기서는 `product_type, retailer, rank` 3중 키로) UPDATE
- confidence 등급별 처리: high/medium만 반영, low/very_low는 skip
- 매칭 실패 건은 이유별로 집계해서 응답에 담아 돌려줌 (`unmatched_samples`)

`import_history`용으로 만들 때 확인/결정해야 할 것:

1. **매칭 키가 뭔지 파일을 열어서 확인.** 이전 세션에서 사용자가 "매칭 키가
   정확히 기억 안 난다"고 해서 보류해뒀다. SKU명(`sku_name`)일 가능성이
   높지만, OEM코드나 다른 식별자일 수도 있음 — 파일의 헤더를 보고 판단.
2. 매칭 키가 `sku_name`이면 `PATCH /api/import-history/hs-code`가 이미
   같은 일을 하므로, 그 로직을 대량 업로드용으로 확장하면 된다
   (`backend/hs_code_importer.py`처럼 `backend/import_history_hs_code_importer.py`
   같은 새 파일 + `POST /api/upload-import-history-hs-codes` 엔드포인트).
3. 업로드 처리 마지막에 반드시:
   - `await _recompute_and_store_import_history_cost_estimates(db)` 호출
   - `asyncio.create_task(_refresh_mvs_safe())` 호출 (sku_history_mv를
     새로 고쳐야 SKU/OEM 수입이력 페이지에 반영됨)
4. **unit(용량) 컬럼이 비어 있으면** 원가가 "1kg당 금액"으로만 나온다
   (`landed_cost_is_per_kg=True`) — 파일에 용량 정보가 있으면 같이
   채워주는 게 좋다(없어도 동작은 함, 표시만 달라짐).

## 빠르게 위치 찾는 법

| 무엇 | 파일 | 참고 지점 |
|---|---|---|
| 메인테이블 HS코드 업로드 (템플릿) | `backend/hs_code_importer.py` | 전체 |
| 메인테이블 업로드 API (템플릿) | `backend/main.py` | `/api/upload-hs-codes` |
| import_history 원가 재계산 함수 (이미 있음) | `backend/main.py` | `_recompute_and_store_import_history_cost_estimates` |
| import_history 수동 편집 API (이미 있음) | `backend/main.py` | `/api/import-history/hs-code` |
| import_history DB 컬럼 | `backend/models.py` | `ImportHistory` 클래스 하단 |
| 프론트 HS코드/원가 열 | `frontend/src/App.jsx` | `ALL_COLS`, `ImportHistoryHsCodeCell`, `EstimatedCostCell` |

## 검증 방법

1. 백엔드 켜고 `PATCH /api/import-history/hs-code`로 임의 SKU 하나에
   테스트 HS코드를 넣어본다.
2. SKU/OEM 수입이력 페이지(메인 대시보드)에서 그 SKU를 검색 — HS코드 열에
   값이 보이고, 관세율표가 업로드돼 있다면 원가 열에도 값이 뜨는지 확인.
   (관세율표가 없으면 "추정불가"만 뜨는 게 정상)
