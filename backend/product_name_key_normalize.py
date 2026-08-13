"""
product_name_key_normalize.py — hs_code_estimation.name_key 정규화 함수.

HS_CODE_METHODOLOGY.md의 "영어상품명이 같으면 기존 판정 결과를 재사용"(status=
reused_matched_by_sku_name_en) 원칙을 조회 키로 그대로 구현한 것 — brand_key_normalize.py와
같은 이유로 완벽한 결정론적 매칭은 못 하지만(용량 표기 편차 등), 실패해도 그냥
새로 판정될 뿐이라 감수한다.
"""
from __future__ import annotations

import re
import unicodedata

_PUNCT_RE = re.compile(r"[.,·・()\[\]{}\"“”]")
_WS_RE = re.compile(r"\s+")


def normalize_product_name_key(name: str | None) -> str:
    """영어상품명 원문 → 정규화된 조회 키. 빈 값이면 빈 문자열 반환."""
    if not name:
        return ""

    s = unicodedata.normalize("NFKC", name).strip().lower()
    s = s.replace("’", "'").replace("‘", "'")
    s = _PUNCT_RE.sub(" ", s)
    s = _WS_RE.sub(" ", s).strip()
    return s
