"""
cost_estimator.py — HS코드 + 원산지 국가 기준으로 관세율을 찾아 착지원가(추정)를 계산.

전제와 한계 (반드시 읽을 것):
  - 매입원가(FOB) 근사치는 mfds_pricing.estimate_purchase_price_usd()가 계산해서
    넘겨준다 — 식품안전나라(MFDS) 국가×품목 평균 수입단가($/kg) × 상품 실제
    용량(unit 컬럼)으로 산출한 값이다. 예전에는 product_sourcing_item.price_usd
    (유통사 진열가/소비자가)를 그대로 썼는데, 이건 미국 현지 유통마진·수수료가
    다 얹힌 가격이라 매입원가로 쓰기엔 부적절해서(2026-08) 교체했다. 실제 공급사
    견적을 받으면 이 근사치도 실측값으로 교체해야 한다.
  - 해상운임/보험료/통관/국내물류 비용은 실측 데이터가 없어 DEFAULT_ASSUMPTIONS의
    가정치를 쓴다. 물동량·품목마다 실제 비용은 다르므로 이 값은 나중에 실제
    포워딩/관세사 견적으로 교체 가능하도록 상수로 분리해뒀다.
  - 부가세(10%)는 사업자 매입세액공제 대상이라 기본적으로 "원가"에 포함하지
    않는다 (INCLUDE_VAT_IN_COST=False로 필요시 켤 수 있음).
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import date

from fta_country_map import get_fta_codes_for_country

DEFAULT_ASSUMPTIONS = {
    "fx_rate_krw_per_usd": 1380.0,     # 환율 (원/$) — 실제 수입일 환율로 교체 필요
    "freight_insurance_ratio": 0.10,   # 해상운임+보험료를 FOB(가정) 대비 비율로 근사
    "customs_broker_fee_ratio": 0.02,  # 통관/관세사 수수료 (CIF 대비 비율로 근사)
    "domestic_logistics_ratio": 0.015, # 국내운송+보세창고료 (CIF 대비 비율로 근사)
}
INCLUDE_VAT_IN_COST = False
VAT_RATE = 0.10


@dataclass
class TariffLookup:
    rate_pct: float
    rate_type: str        # 'A'(기본세율) 또는 FTA 코드 (예: 'FEU1')
    basis_label: str       # 화면 표시용 (예: "한-EU FTA(0%)")


def resolve_tariff_rate(
    tariff_rows: list[dict],
    origin_country_kr: str | None,
    on_date: date | None = None,
) -> TariffLookup | None:
    """특정 HS코드에 대한 tariff_rate 행들(hs_code로 이미 필터된 상태) 중에서
    오늘 유효하고, 원산지 국가에 실제로 적용 가능한 세율 후보 중 최저값을 고른다.
    tariff_rows: [{"rate_type":..., "rate_pct":..., "effective_from":date|None, "effective_to":date|None}, ...]
    """
    if not tariff_rows:
        return None
    on_date = on_date or date.today()

    applicable_codes = {"A"} | set(get_fta_codes_for_country(origin_country_kr or ""))

    candidates: list[tuple[float, str]] = []
    for row in tariff_rows:
        rate_type = row["rate_type"]
        if rate_type not in applicable_codes:
            continue
        eff_from = row.get("effective_from")
        eff_to = row.get("effective_to")
        if eff_from and on_date < eff_from:
            continue
        if eff_to and on_date > eff_to:
            continue
        candidates.append((float(row["rate_pct"]), rate_type))

    if not candidates:
        return None

    rate_pct, rate_type = min(candidates, key=lambda c: c[0])
    label = "기본세율" if rate_type == "A" else f"FTA 협정세율({rate_type})"
    return TariffLookup(rate_pct=rate_pct, rate_type=rate_type, basis_label=f"{label} {rate_pct:g}%")


def estimate_landed_cost_krw(
    purchase_price_usd: float | None,
    tariff: TariffLookup | None,
    assumptions: dict | None = None,
) -> float | None:
    """purchase_price_usd(매입원가 근사치, mfds_pricing 산출값) → 관세 포함
    착지원가(원) 추정. tariff가 없으면(HS코드 미확정 등) None을 반환 — "추정 불가"로 취급."""
    if purchase_price_usd is None or tariff is None:
        return None

    a = {**DEFAULT_ASSUMPTIONS, **(assumptions or {})}

    fob_krw = purchase_price_usd * a["fx_rate_krw_per_usd"]
    freight_insurance = fob_krw * a["freight_insurance_ratio"]
    cif_krw = fob_krw + freight_insurance

    duty_krw = cif_krw * (tariff.rate_pct / 100)
    dutiable_base = cif_krw + duty_krw

    customs_fee = cif_krw * a["customs_broker_fee_ratio"]
    domestic_logistics = cif_krw * a["domestic_logistics_ratio"]

    total = dutiable_base + customs_fee + domestic_logistics
    if INCLUDE_VAT_IN_COST:
        total += dutiable_base * VAT_RATE

    return round(total, 0)
