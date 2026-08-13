"""
unit_converter.py — 자유 텍스트 용량 표기("단량")를 kg으로 환산하거나, 상품명 원문에서
그 표기 자체를 추출한다.

parse_unit_to_kg()는 product_sourcing_item.unit처럼 사람이 이미 정리해둔 "단량" 값
("500 g" 등)을 kg으로 바꾼다 — MFDS 평균 수입단가($/kg)를 실제 상품 1개 가격(USD)으로
바꾸려면 그 상품이 몇 kg짜리인지 알아야 하는데, 그 값이 이미 있으니 파싱만 하면 된다.

extract_unit_from_product_name()은 반대로, 크롤러가 원문 상품명만 가져오는 크롤링
스냅샷(product_sourcing_crawl_snapshot_item)처럼 "단량" 컬럼 자체가 없는 경우에 그
상품명 텍스트에서 단량 표기를 직접 뽑아내기 위한 함수다 — 로컬 전용 상품매칭 도구
(amazon-aeon-enrich/match.js의 parseSizeValue)가 검증용으로 쓰던 것과 같은 방식(부피
단위를 무게 단위보다 먼저 매칭해 "16.9 fl oz"의 "oz"가 무게로 오인되지 않게 함)을
저장용으로 가져왔다. 상품명 어디에 있든(마케팅 문구에 섞여 있어도) 첫 번째로 나오는
단량 표기를 원문 그대로 뽑는다.

두 함수 다 처리 불가능한 경우(개수 표기 "36개입", 단위 자체가 없는 경우 등)는 None을
반환한다 — 잘못된 값을 추정해서 쓰는 것보다 "환산/추출 불가"로 정직하게 표시하는 쪽을
택한다. 두 함수가 인식하는 단위 어휘(약어+전체 철자)는 아래 _UNIT_TO_KG_FACTOR 하나로
공유한다 — extract가 뽑아낸 표기를 parse가 못 알아들으면 추출 자체가 무의미해지므로,
어휘가 어긋나지 않게 한곳에서만 관리한다.
"""
from __future__ import annotations

import re

_G_PER_OZ = 28.349523125
_G_PER_LB = 453.59237
_ML_PER_FLOZ = 29.5735295625

# 정규화된 단위 표기(소문자, 마침표 제거, 공백 1칸) → kg 환산 배율. ml/L/fl oz는 액체
# 밀도를 1 g/ml(물 기준)로 근사한다 — 오일류는 실제로 더 가볍고(약 0.91~0.92) 당류/
# 소스는 더 무거울 수 있어 약간의 오차가 있지만, 상품별 실측 밀도 데이터가 없어
# 채택한 근사치다.
_UNIT_TO_KG_FACTOR = {
    "kg": 1.0, "kilogram": 1.0, "kilograms": 1.0,
    "g": 0.001, "gram": 0.001, "grams": 0.001,
    "oz": _G_PER_OZ / 1000, "ounce": _G_PER_OZ / 1000, "ounces": _G_PER_OZ / 1000,
    "lb": _G_PER_LB / 1000, "lbs": _G_PER_LB / 1000,
    "pound": _G_PER_LB / 1000, "pounds": _G_PER_LB / 1000,
    "l": 1.0, "liter": 1.0, "liters": 1.0, "litre": 1.0, "litres": 1.0,
    "ml": 0.001, "milliliter": 0.001, "milliliters": 0.001,
    "millilitre": 0.001, "millilitres": 0.001,
    "fl oz": _ML_PER_FLOZ / 1000, "fluid ounce": _ML_PER_FLOZ / 1000, "fluid ounces": _ML_PER_FLOZ / 1000,
}

# 부피 표기를 무게보다 먼저 나열한다 — "fl oz"는 시작이 "fl"이라 무게 쪽 "oz\b"와 같은
# 시작 위치에서 경쟁하진 않지만(정규식이 문자열을 앞에서부터 그대로 따라가므로), 명확성을
# 위해 순서를 지킨다.
_UNIT_ALT = (
    r"fl\.?\s?oz|fluid\s?ounces?|kilograms?|kg|milliliters?|millilitres?|ml|"
    r"liters?|litres?|l\b|grams?|g\b|ounces?|oz|lbs?|pounds?"
)
_PARSE_PATTERN = re.compile(rf"([\d.,]+)\s*({_UNIT_ALT})\b", re.IGNORECASE)
_EXTRACT_PATTERN = re.compile(rf"\d+(?:\.\d+)?\s?(?:{_UNIT_ALT})\b", re.IGNORECASE)


def _normalize_unit_token(raw: str) -> str:
    s = raw.lower().strip()
    s = s.replace(".", "")
    s = re.sub(r"\s+", " ", s)
    return s


def parse_unit_to_kg(unit_text: str | None) -> float | None:
    """"500 g" → 0.5, "16 oz" → 0.4536, "2 lb"/"2 lbs"/"2 pounds" → 0.9072,
    "16.9 fl oz" → 0.5, "355ml (12 fl oz)" → 0.355(첫 번째 수치 기준, 괄호 안 환산
    표기는 무시), "36개입"처럼 개수 표기라 무게를 알 수 없으면 None."""
    if not unit_text:
        return None
    primary = unit_text.split("(")[0].strip()
    m = _PARSE_PATTERN.match(primary)
    if not m:
        return None
    try:
        value = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    factor = _UNIT_TO_KG_FACTOR[_normalize_unit_token(m.group(2))]
    kg = value * factor
    return kg if kg > 0 else None


def extract_unit_from_product_name(product_name: str | None) -> str | None:
    """상품명 원문에서 처음 나오는 부피/무게 표기를 원문 그대로 뽑아 반환한다.
    "Carapelli ... Extra Virgin Olive Oil, Cold Extracted, ..., 16.9 fl oz" →
    "16.9 fl oz". 못 찾으면(개수 표기만 있거나 단위 자체가 없으면) None."""
    if not product_name:
        return None
    m = _EXTRACT_PATTERN.search(product_name)
    if not m:
        return None
    return m.group(0).strip()
