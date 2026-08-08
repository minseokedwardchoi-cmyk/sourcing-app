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

# 부분문자열 매칭은 원래 2글자 이상 카테고리만 대상으로 한다 — 1글자 카테고리는
# ("파", "배", "무"처럼) 브랜드/제품명 아무 데나 우연히 끼어 있을 확률이 높아서다.
# 다만 "잼"처럼 실제로 다른 단어 안에 우연히 낄 위험이 거의 없고 실사용 빈도가
# 높은 건 예외로 허용한다.
_SHORT_ITEM_NAME_ALLOWLIST = {"잼"}

# product_type이 이 접미어로 끝나면 "가공품"이라는 뜻이라, 매칭된 item_name이
# 그 가공 형태 자체(예: "소스", "젓갈")가 아니라 안에 들어간 원재료명(예: "포도",
# "무화과", "바질")이면 오매칭일 가능성이 크다 — "포도씨유"가 "포도"(생포도) 평균
# 수입단가로 잡히는 식. 이 경우 신뢰도를 substring이라도 high로 주지 않고
# medium으로 낮춰 사람 확인을 거치게 한다.
_PROCESSING_FORM_SUFFIXES = (
    "오일", "씨유", "잼", "페스토", "에이드", "주스", "완탕", "버터", "드레싱",
    "시럽", "식초", "크래커", "케찹", "케첩",
)

# 표기 차이 흡수용 별칭(정규화 시 왼쪽 → 오른쪽으로 치환). "케찹"/"케첩"처럼
# MFDS 표준 표기와 실제 상품명 표기가 갈리는 경우를 위한 것.
_SPELLING_ALIASES = {
    "케찹": "케첩",
}

# MFDS에는 "쿠키/크래커/감자칩/프레첼/팝콘/그래놀라바/시리얼" 같은 세분류가 아예
# 없고 전부 "과자" 하나로 뭉뚱그려진다(실제 MFDS 소분류 목록 확인 결과). 부분문자열/
# bigram으로도 못 찾은 경우에 한해, 이런 형태 키워드가 있으면 "과자"(또는 해당하는
# 다른 존재하는 카테고리)로 강등된 신뢰도(low)로 매칭한다 — 없는 것보다는 낫지만
# 쿠키/칩/프레첼을 다 같은 평균단가로 뭉치는 거친 근사치라는 걸 명시하기 위해
# high/medium과 분리된 별도 신뢰도 등급을 쓴다.
_CATEGORY_FALLBACK_KEYWORDS: dict[str, tuple[str, ...]] = {
    "과자": ("크래커", "칩스", "칩", "비스킷", "프레첼", "팝콘", "그래놀라", "시리얼",
             "스낵믹스", "그레이엄", "크런치", "토스트", "모나카", "마카롱", "쿠키",
             "퍼프콘"),
    "캔디류": ("젤리", "구미"),
    "초콜릿가공품": ("초콜릿", "누텔라"),
    "조제커피": ("커피",),
}


@dataclass
class MatchResult:
    product_type: str
    matched_item_name: str | None
    confidence: str        # "high" | "medium" | "none"
    method: str             # "substring" | "bigram" | "none"
    score: float            # 참고용 유사도(0~1). substring 매칭은 1.0 고정.


def _normalize(text: str) -> str:
    normalized = "".join(ch for ch in text.lower() if not ch.isspace())
    for src, dst in _SPELLING_ALIASES.items():
        normalized = normalized.replace(src, dst)
    return normalized


def _strip_trailing_parenthetical(text: str) -> str:
    """"오렌지자몽주스 (착즙)" → "오렌지자몽주스". 가공 형태 판정(끝말잇기)이
    괄호 부연설명 때문에 놓치는 걸 막기 위한 전처리."""
    text = text.rstrip()
    while text.endswith(")"):
        open_idx = text.rfind("(")
        if open_idx == -1:
            break
        text = text[:open_idx].rstrip()
    return text


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
    # 가공 형태 판정("...주스 (착즙)"처럼 괄호 부연설명이 끝에 붙어 endswith를
    # 방해하는 걸 막기 위해 괄호를 뗀 버전을 따로 정규화해둔다.
    norm_product_no_paren = _normalize(_strip_trailing_parenthetical(product_type))

    substring_hits = [
        c for c in usable_candidates
        if (len(c) >= 2 or c in _SHORT_ITEM_NAME_ALLOWLIST) and _normalize(c) in norm_product
    ]
    if substring_hits:
        best = max(substring_hits, key=len)
        norm_best = _normalize(best)
        is_processed_form = any(
            norm_product_no_paren.endswith(_normalize(suf)) for suf in _PROCESSING_FORM_SUFFIXES
        )
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

    # MFDS에 세분류가 아예 없는 형태(쿠키/칩/프레첼 등)는 존재하는 가장 가까운
    # 상위 카테고리로 낮은 신뢰도(low)로나마 매칭해둔다 — "추정불가"보다는 낫다는
    # 판단이나, high/medium과 정확도 성격이 달라 신뢰도를 분리해 표시한다.
    for fallback_item, keywords in _CATEGORY_FALLBACK_KEYWORDS.items():
        if fallback_item not in usable_candidates:
            continue
        if any(_normalize(kw) in norm_product for kw in keywords):
            return MatchResult(product_type, fallback_item, "low", "category_fallback", 0.0)

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
