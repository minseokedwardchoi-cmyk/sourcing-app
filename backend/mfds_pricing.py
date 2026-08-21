"""
mfds_pricing.py — product_sourcing_item 한 행(product_type, origin, unit)을
MFDS(수입식품정보마루) 평균 수입단가 기반 "매입원가 근사치(USD)"로 바꾼다.

price_usd(유통사 진열가/소비자가)를 원가로 잘못 쓰던 문제의 대체 로직이다.
계산 순서:
  1. product_type → MFDS 소분류 (mfds_manual_overrides 수동표 우선, 없으면
     mfds_item_matcher 자동매칭 폴백)
  2. origin 텍스트 → 원산지 국가(들). 여러 국가(블렌드)면 그 소분류를 실제
     가장 많이 수입하는 국가를 선택 (resolve_origin_country_for_item)
  3. (국가, 소분류) → country_item_amount의 amount_usd_k/weight_ton → $/kg
  4. unit(단량, 예 "500 g") → kg (unit_converter)
  5. $/kg × kg = 상품 1개당 매입원가 추정치(USD)

국가/소분류/중량 중 하나라도 못 찾으면 None을 반환한다 — price_usd로 조용히
폴백하지 않는다. 잘못된 소비자가를 다시 쓰는 것보다 "추정 불가"가 낫다는 게
이번 작업의 핵심 전제이기 때문이다.
"""
from __future__ import annotations

from dataclasses import dataclass

from country_utils import match_all_countries_in_text_broad
from mfds_item_matcher import match_product_to_mfds_item, resolve_origin_country_for_item
from mfds_manual_overrides import get_mfds_item
from unit_converter import parse_unit_to_kg

# (country, mfds_item_name) -> (amount_usd_k, weight_ton|None)
PriceLookupTable = dict[tuple[str, str], tuple[float, float | None]]


@dataclass
class MfdsPriceLookup:
    mfds_item: str
    country: str
    price_usd_per_kg: float


def _resolve_mfds_item(product_type: str | None, all_item_names: list[str]) -> str | None:
    if not product_type:
        return None
    mfds_item = get_mfds_item(product_type)
    if mfds_item is None:
        match = match_product_to_mfds_item(product_type, all_item_names)
        mfds_item = match.matched_item_name
    return mfds_item


def resolve_origin_country(
    product_type: str,
    origin: str | None,
    all_item_names: list[str],
    price_lookup: PriceLookupTable,
) -> str | None:
    """origin 텍스트에서 원산지 국가를 하나로 확정한다. 여러 국가(블렌드)가
    후보면 resolve_origin_country_for_item()으로 해당 품목을 실제 가장 많이
    수입하는 나라를 고른다.

    cost_estimator.py의 관세율 조회가 여기서 고른 국가를 그대로 쓰도록
    공유하기 위한 함수다 — 관세율은 국가(FTA 그룹)별로 값이 다르므로, 블렌드일
    때 매입원가 계산과 다른 나라를 기준으로 삼으면 한 상품에 대해 서로 다른
    원산지를 근거로 한 숫자가 같이 표시되는 불일치가 생긴다."""
    candidates = match_all_countries_in_text_broad(origin)
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    mfds_item = _resolve_mfds_item(product_type, all_item_names)
    if not mfds_item:
        return candidates[0]

    amount_lookup = {
        (c, mfds_item): price_lookup[(c, mfds_item)][0]
        for c in candidates if (c, mfds_item) in price_lookup
    }
    return resolve_origin_country_for_item(candidates, mfds_item, amount_lookup)


def resolve_mfds_price(
    product_type: str,
    origin: str | None,
    all_item_names: list[str],
    price_lookup: PriceLookupTable,
) -> MfdsPriceLookup | None:
    mfds_item = _resolve_mfds_item(product_type, all_item_names)
    if not mfds_item:
        return None

    resolved_country = resolve_origin_country(product_type, origin, all_item_names, price_lookup)
    if not resolved_country:
        return None

    entry = price_lookup.get((resolved_country, mfds_item))
    if not entry:
        return None
    amount_usd_k, weight_ton = entry
    if not weight_ton:
        return None
    return MfdsPriceLookup(
        mfds_item=mfds_item,
        country=resolved_country,
        price_usd_per_kg=amount_usd_k / weight_ton,
    )


@dataclass
class PurchasePriceEstimate:
    price_usd: float
    is_per_kg: bool  # True면 unit을 kg으로 못 바꿔서 "상품 1개당"이 아니라
                      # "1kg당" 가격을 그대로 쓴 것 — 화면에 원/kg으로 표시해야 함
    country: str      # 매입원가 계산에 실제로 쓰인 원산지 국가(블렌드면 최다
                       # 수입국). 관세율 조회도 같은 국가를 쓰도록 호출부에 넘겨준다.


def estimate_purchase_price(
    product_type: str,
    origin: str | None,
    unit_text: str | None,
    all_item_names: list[str],
    price_lookup: PriceLookupTable,
) -> PurchasePriceEstimate | None:
    """매입원가 추정치(USD). unit이 "36개입"처럼 중량으로 환산이 안 되거나
    아예 없으면, 상품 1개당 금액 대신 1kg당 금액(is_per_kg=True)을 돌려준다 —
    "추정 불가"로 버리기보다 단가(원/kg) 형태로라도 보여주는 쪽을 택했다.
    MFDS 매칭/국가/중량 데이터 자체가 없으면 None ("추정 불가")."""
    lookup = resolve_mfds_price(product_type, origin, all_item_names, price_lookup)
    if lookup is None:
        return None
    unit_kg = parse_unit_to_kg(unit_text)
    if unit_kg is not None:
        return PurchasePriceEstimate(lookup.price_usd_per_kg * unit_kg, is_per_kg=False, country=lookup.country)
    return PurchasePriceEstimate(lookup.price_usd_per_kg, is_per_kg=True, country=lookup.country)
