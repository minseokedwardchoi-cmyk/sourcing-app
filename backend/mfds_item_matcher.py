"""
mfds_item_matcher.py — product_sourcing_item.product_type(브랜드+상품명)을
MFDS(수입식품정보마루) country_item_amount.item_name(품목분류명, 브랜드 없는
coarse 카테고리)에 자동으로 매칭한다.

용도: cost_estimator.py가 "미국 소비자가(price_usd)" 대신 "국가×품목 평균
수입단가(country_item_amount.amount_usd_k / weight_ton)"를 원가 근사치로 쓰려면,
먼저 우리 상품이 MFDS의 어느 품목분류에 해당하는지 알아야 한다.

매칭 방식(정교한 NLP 없이, 설명 가능하고 오탐이 적은 순서로):
  1. 부분문자열 매칭 — product_type 안에 MFDS item_name이 그대로 들어있으면
     가장 신뢰도가 높다("OLITALIA 엑스트라버진 올리브유" ⊃ "올리브유"). 여러
     item_name이 동시에 부분문자열로 걸리면(예: "소스"와 "토마토소스") 더 긴
     쪽이 더 구체적이므로 그걸 채택한다.
  2. 부분문자열이 없으면 문자 2-gram Jaccard 유사도로 가장 가까운 item_name을
     찾는다 — 신뢰도가 낮으므로 사람 확인이 필요하다는 표시(medium)를 남긴다.
  3. 그마저도 임계값 미만이면 매칭 실패(none) — 사람이 수동으로 매핑해야 함.

이 모듈은 DB에 직접 접근하지 않는다 — 후보 item_name 목록과 결과를 호출부(스크립트/
엔드포인트)가 주고받는다. 오탐이 있어도 되돌리기 쉽도록, 매칭 결과는 확정 필드가
아니라 "제안"으로 취급하고 사람이 검수할 것을 전제로 한다.
"""
from __future__ import annotations

from dataclasses import dataclass

# 부분문자열/2-gram 매칭 둘 다에서 걸러낼, 품목명이라 보기 어려운 극단적으로
# 짧거나 범용적인 MFDS 카테고리(오탐 방지용). 필요시 계속 추가.
_GENERIC_ITEM_NAMES_BLOCKLIST = {
    "기타", "기타식품", "기타가공품", "기타 가공품", "혼합제제", "기타류",
}

_MEDIUM_CONFIDENCE_THRESHOLD = 0.34

# product_type이 이 접미어로 끝나면 "가공품"이라는 뜻이라, 매칭된 item_name이
# 그 가공 형태 자체(예: "소스", "젓갈")가 아니라 안에 들어간 원재료명(예: "포도",
# "무화과", "바질")이면 오매칭일 가능성이 크다 — "포도씨유"가 "포도"(생포도) 평균
# 수입단가로 잡히는 식. 이 경우 신뢰도를 substring이라도 high로 주지 않고
# medium으로 낮춰 사람 확인을 거치게 한다.
_PROCESSING_FORM_SUFFIXES = (
    "오일", "씨유", "잼", "페스토", "에이드", "주스", "완탕", "버터", "드레싱",
    "시럽", "식초", "크래커", "케찹", "케첩",
)


@dataclass
class MatchResult:
    product_type: str
    matched_item_name: str | None
    confidence: str        # "high" | "medium" | "none"
    method: str             # "substring" | "bigram" | "none"
    score: float            # 참고용 유사도(0~1). substring 매칭은 1.0 고정.


def _normalize(text: str) -> str:
    return "".join(ch for ch in text.lower() if not ch.isspace())


def _bigrams(text: str) -> set[str]:
    if len(text) < 2:
        return {text} if text else set()
    return {text[i:i + 2] for i in range(len(text) - 1)}


def _bigram_jaccard(a: str, b: str) -> float:
    set_a, set_b = _bigrams(a), _bigrams(b)
    if not set_a or not set_b:
        return 0.0
    inter = len(set_a & set_b)
    union = len(set_a | set_b)
    return inter / union if union else 0.0


def match_product_to_mfds_item(
    product_type: str,
    candidate_item_names: list[str],
) -> MatchResult:
    """product_type 하나를 candidate_item_names(중복 없는 MFDS item_name 전체
    목록) 중 가장 근접한 것에 매칭한다."""
    usable_candidates = [c for c in candidate_item_names if c not in _GENERIC_ITEM_NAMES_BLOCKLIST]
    norm_product = _normalize(product_type)

    substring_hits = [c for c in usable_candidates if len(c) >= 2 and _normalize(c) in norm_product]
    if substring_hits:
        best = max(substring_hits, key=len)
        norm_best = _normalize(best)
        is_processed_form = any(norm_product.endswith(_normalize(suf)) for suf in _PROCESSING_FORM_SUFFIXES)
        matched_is_raw_ingredient = is_processed_form and not any(
            _normalize(suf) in norm_best for suf in _PROCESSING_FORM_SUFFIXES
        )
        if matched_is_raw_ingredient:
            return MatchResult(product_type, best, "medium", "substring_raw_ingredient", 0.5)
        return MatchResult(product_type, best, "high", "substring", 1.0)

    best_item, best_score = None, 0.0
    for c in usable_candidates:
        score = _bigram_jaccard(norm_product, _normalize(c))
        if score > best_score:
            best_item, best_score = c, score

    if best_item and best_score >= _MEDIUM_CONFIDENCE_THRESHOLD:
        return MatchResult(product_type, best_item, "medium", "bigram", round(best_score, 3))

    return MatchResult(product_type, None, "none", "none", round(best_score, 3))


def match_all(
    product_types: list[str],
    candidate_item_names: list[str],
) -> list[MatchResult]:
    return [match_product_to_mfds_item(pt, candidate_item_names) for pt in product_types]


def resolve_origin_country_for_item(
    candidate_countries: list[str],
    item_name: str | None,
    amount_lookup: dict[tuple[str, str], float],
) -> str | None:
    """origin 텍스트에서 여러 국가가 후보로 나온 경우(블렌드 등), 그 국가들 중
    해당 품목(item_name)을 실제로 가장 많이 수입하는 국가를 고른다.
    amount_lookup: {(country, item_name): amount_usd_k, ...} — 호출부가
    country_item_amount 테이블에서 미리 조회해 넘긴다.
    금액 데이터가 하나도 없으면(품목 매칭 실패 등) origin 텍스트에 먼저 등장한
    국가로 폴백한다."""
    if not candidate_countries:
        return None
    if len(candidate_countries) == 1:
        return candidate_countries[0]
    if not item_name:
        return candidate_countries[0]

    best_country, best_amount = candidate_countries[0], -1.0
    for country in candidate_countries:
        amount = amount_lookup.get((country, item_name), 0.0) or 0.0
        if amount > best_amount:
            best_country, best_amount = country, amount
    return best_country
