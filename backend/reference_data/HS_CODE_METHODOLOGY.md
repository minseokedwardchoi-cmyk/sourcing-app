# HS코드 추정 방법론 (v2/direct) — 세션 인계 문서

## 배경

`product_sourcing_item` 테이블(월마트/샘스클럽/아마존/이온몰 품목별 Top40 크롤링 데이터, 7,397행)의 hs_code를
재조사해서 만든 게 `hs_final_7397.csv`야. 기존에는 품목(유형) 단위로 대표 HS코드 하나를 정해서 그 유형 아래
크롤링된 상품 전체(순위표 40개)에 일괄 적용했는데, 이 방식은 순위표 안에 실제로는 다른 카테고리 상품이나
아예 무관한 크롤링 노이즈가 섞여 있어도 전부 같은 HS코드가 찍히는 문제가 있었음. v2/direct 방식은 이걸
**상품 하나하나 전수 스캔**해서 바로잡은 결과물임.

이 문서는 그 방법론을 정리한 것 — 다음 작업(`import_history`/`sku_history_mv`에 있는 약 18만~28만개
제품으로 확장 적용)을 진행할 세션이 참고할 수 있게 남겨둠.

## 핵심 원칙

1. **이름유사 ≠ 동일 제품.** 같은 "유형(카테고리)"으로 크롤링됐다고 해서 실제로 같은 상품인 건 아님.
   브랜드가 다르거나, 포맷이 다르거나(냉동 vs 상온, 낱개 vs 세트), 아예 다른 카테고리 상품이 섞여
   들어오는 경우가 흔함 (예: "명란젓갈" 카테고리 순위표에 낚싯대가 끼어있는 식 — 크롤러 노이즈).
2. **전수 스캔 필수.** 카테고리 대표 HS코드를 정해놓고 나머지를 거기 맞춰 넣는 게 아니라, 카테고리 안의
   **모든 개별 상품명(영어 상품명 기준)을 하나씩** 확인해서 그 상품 자체가 뭔지 판정함.
3. **판정 결과는 두 갈래**:
   - **진짜 무관한 상품(비식품, 완전히 다른 카테고리)** → HS코드를 **비워두고** `status=flagged_non_food_mismatch`로
     표시. 억지로 카테고리 코드를 끼워맞추지 않음.
   - **카테고리는 다르지만 실존하는 유사식품** (예: 냉동 프렌치프라이 카테고리인데 실제로는 감자칩/케첩/잼/라면인 경우)
     → 그 상품 **실체에 맞는 정확한 개별 HS코드**를 새로 부여.

## 참조 자료 (분류 근거)

- `references/hsk_code_table_20260101.xlsx` — 관세청 2026년 HSK 품목분류코드표
- `references/hsk_item_names_20260101.xlsx` — 관세청 2026년 HSK 품목명 매핑
- 위 두 파일로 상품을 매칭해서 코드를 정하고, CSV의 `evidence_url` 컬럼엔 항상 `"관세청 2026 HSK"`로 기록.

## 처리 절차 (실제 작업 흔적 기준)

1. **전수 덤프**: 품목(유형)별로 크롤링된 모든 고유 상품명을 뽑아냄 (`all_unique_by_type.json`,
   `scan_remaining.py`, `classify_remaining.py`). 결과를 `batch_a.txt`~`batch_h.txt`,
   `candy1_review.txt`, `chips1_review.txt`, `crackers1_review.txt`, `sauces1_review.txt`,
   `oils_review.txt`, `flagged_scan.txt`, `deep_recheck2.txt` 같은 리뷰용 텍스트 파일로 카테고리별 정리.
2. **개별 판정**: 리뷰 파일의 상품명을 하나씩 훑으면서 위 "핵심 원칙"에 따라 판정.
3. **적용 스크립트 작성**: 판정 결과를 `fix_batch_a.py`~`fix_batch_h.py` 같은 스크립트로 만들어서,
   품목(유형) + 영어상품명 substring 매칭으로 `hs_final_7397.csv`를 직접 패치.
   패턴 예시 (`fix_batch_h.py`):
   ```python
   def setcode(r, code, conf, reason):
       r['hs_code']=code; r['confidence']=conf; r['reason']=reason
       r['evidence_url']='관세청 2026 HSK'; r['status']='researched_v2_direct'

   for r in rows:
       ln = (r['영어상품명'] or '').lower()
       if r['유형'] != 'CAVENDISH 크리스피 스파이시':
           continue
       if "ass kickin' ketchup" in ln:
           setcode(r, '2103.20-1000', 'high', '하바네로맛 케첩(프렌치프라이 자체 아님): 토마토케첩')
       elif 'doritos spicy nacho' in ln:
           setcode(r, '1904.10-9000', 'medium', '옥수수 토르티야칩(감자 프렌치프라이 아님): 곡물 팽화가공식품으로 잠정분류')
       # ...
   ```
4. 마지막에 `db_verified` 검증 — 이 상품이 여전히 실제 사이트 DB(`product_sourcing_item`)에 존재하는지
   확인해서 `ok` / `not_in_db_excluded`로 표시 (삭제되거나 재크롤링에서 빠진 행 걸러내기용).

## CSV 컬럼 의미 (`hs_final_7397.csv`)

| 컬럼 | 의미 |
|---|---|
| 유형 | 품목 카테고리 (product_type) |
| 유통사 | 월마트/샘스클럽/아마존/이온몰 (+ 부가 설명) |
| rank_col | 유통사 내 순위 |
| 브랜드(원본) | 크롤링된 브랜드명(한글) |
| 영어상품명 | 실제 분류 판정에 쓰는 핵심 필드 |
| 판정 | **HS코드와 무관함** — 병행수입 가능 여부 판정(O/X/수입이력 없음 등). 헷갈리지 말 것 |
| 판정근거 | 위 병행수입 판정의 근거 |
| 기존작업여부 | 이 행이 이전 파이프라인(v74/v126)에서 이미 처리됐었는지, 신규인지 표시 |
| **hs_code** | 최종 판정된 HS코드 (10자리, 비어있으면 미분류/무관 상품) |
| **confidence** | `high`(HSK표에서 명확히 매칭) / `medium`(애매해서 근접 카테고리로 잠정분류) / `very_low` |
| **reason** | 판정 근거 텍스트. "~아님: ~로 분류" 형식이 많음 |
| **evidence_url** | 항상 `"관세청 2026 HSK"` |
| product_identity_note | (대체로 비어있음, 예비 필드) |
| **status** | 아래 표 참고 |
| db_verified | `ok` / `not_in_db_excluded` |

### status 값

- `researched_v2_direct` — 이번 v2 전수 스캔으로 새로 판정 (6,776건, 대다수)
- `reused_matched_by_sku_name_en` — 영어상품명이 같은 기존 판정 결과를 재사용
- `flagged_non_food_mismatch` — 카테고리와 무관한 상품으로 판정, hs_code 비움 (193건)
- `needs_manual_review` — 아직 사람 확인 필요 (일부는 hs_code 비어있고, 일부는 예전 값이 그대로 남아있을 수 있음 — 이 경우가 이전에 "CSV는 비어있는데 사이트엔 값 있음" 172건 중 다수를 차지했음)
- `reused_confirmed_group` — 같은 브랜드/제품 그룹으로 확인된 값 재사용

## 이미 완료된 것 / 다음 단계

- `hs_final_7397.csv` ↔ 사이트(`product_sourcing_item`, 7,397행) **완전 동기화 완료** (2026-08-13,
  996건 값 교체/채움 + 172건 비움 + 관세율/착지원가 재계산까지 반영).
- **다음 목표**: `import_history`(114만행 원본) / `sku_history_mv`(28.6만행, 사이트에 노출되는 OEM·수입이력
  집계뷰)에도 같은 방식론 적용. 단, 규모가 7,397건 → 최소 18.2만건(고유 sku_name 기준)으로 약 25배
  커지기 때문에, 이 문서에 나온 "카테고리별 리뷰 파일 훑고 사람이 if/elif 스크립트 짜는" 수작업 방식을
  그대로는 적용 불가능함. 미해결 의사결정 사항:
  1. 판정 단위를 `sku_name`(18.2만) 기준으로 할지, `sku_history_mv` 행(28.6만, 수입업체별 중복 포함)
     그대로 할지 — sku_name 기준 권장(HS코드는 수입업체가 아니라 제품 자체 속성이므로).
  2. hs_code 저장 위치: `import_history`에 컬럼 추가(sku_name별 UPDATE) vs 별도 매핑 테이블 신설 후
     MV에 JOIN — sku_history_mv는 백엔드 재배포 시 DROP 후 재생성되는 구조라 MV 자체에 저장하면 유실 위험.
  3. 18만+ 건을 감당할 반자동화 전략 — `mc`/`product_type`/`product_category` 같은 기존 분류 필드를
     1차 자동 매칭에 활용하고, 애매하거나 매칭 실패한 것만 사람이 리뷰하는 식의 설계가 필요함
     (이 문서의 원칙 1·2·3은 그대로 유지하되, 적용 방식은 스케일에 맞게 재설계해야 함).
