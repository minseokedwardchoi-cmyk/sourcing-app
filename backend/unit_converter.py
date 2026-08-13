"""
unit_converter.py — product_sourcing_item.unit(자유 텍스트 용량 표기)을 kg으로 환산.

MFDS 평균 수입단가($/kg)를 실제 상품 1개 가격(USD)으로 바꾸려면 그 상품이 몇 kg
짜리인지 알아야 하는데, 그 값이 이미 메인 페이지에 노출되는 unit 컬럼("단량")에
있다 — 새로 조사할 필요 없이 파싱만 하면 된다.

처리 불가능한 경우(개수 표기 "36개입", 형식이 특이한 것 등)는 None을 반환한다 —
잘못된 값을 추정해서 쓰는 것보다 "환산 불가 → 추정 불가"로 정직하게 표시하는 쪽을
택한다.
"""
from __future__ import annotations

import re

_G_PER_OZ = 28.349523125
_G_PER_LB = 453.59237

# 단위 → kg 환산 배율. ml/L은 액체 밀도를 1 g/ml(물 기준)로 근사한다 — 오일류는
# 실제로 더 가볍고(약 0.91~0.92) 당류/소스는 더 무거울 수 있어 약간의 오차가
# 있지만, 상품별 실측 밀도 데이터가 없어 채택한 근사치다.
_UNIT_TO_KG_FACTOR = {
    "kg": 1.0,
    "g": 0.001,
    "l": 1.0,
    "ml": 0.001,
    "oz": _G_PER_OZ / 1000,
    "lb": _G_PER_LB / 1000,
    "lbs": _G_PER_LB / 1000,
    "pound": _G_PER_LB / 1000,
    "pounds": _G_PER_LB / 1000,
}

_PATTERN = re.compile(r"([\d.,]+)\s*(kg|g|ml|l|oz|lbs?|pounds?)\b", re.IGNORECASE)


def parse_unit_to_kg(unit_text: str | None) -> float | None:
    """"500 g" → 0.5, "16 oz" → 0.4536, "2 lb" / "2 lbs" / "2 pounds" → 0.9072,
    "355ml (12 fl oz)" → 0.355(첫 번째 수치 기준, 괄호 안 환산 표기는 무시),
    "36개입" 처럼 개수 표기라 무게를 알 수 없으면 None."""
    if not unit_text:
        return None
    primary = unit_text.split("(")[0].strip()
    m = _PATTERN.match(primary)
    if not m:
        return None
    try:
        value = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    factor = _UNIT_TO_KG_FACTOR[m.group(2).lower()]
    kg = value * factor
    return kg if kg > 0 else None
