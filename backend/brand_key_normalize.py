"""
brand_key_normalize.py — brand_verification.brand_key 정규화 함수.

product_sourcing_item에 이미 있던 brand_group_key(정렬/렌더링 전용 키, 생성 스크립트
분실됨)와는 완전히 독립적인 별도 정규화다. 목적이 다르다 — 이건 "같은 브랜드를
Gemini로 두 번 검증하지 않기 위한 조회 키"라서, brand_group_key처럼 품목유형 안에서만
안정적이면 되는 게 아니라 전체 브랜드 범위에서 안정적이어야 한다.

완벽한 결정론적 정규화는 불가능하다(Hellmann's vs Hellmann처럼 표기 편차는 항상 있다) —
브랜드 신규 등장 시 이 키로 못 찾으면 새로 검증되는 정도의 실패는 감수한다(중복 검증이
검증 누락보다 낫다).
"""
from __future__ import annotations

import re
import unicodedata

_SUFFIX_WORDS = {
    "inc", "incorporated", "co", "corp", "corporation", "company",
    "ltd", "llc", "group", "gmbh", "srl", "spa", "sa", "plc",
}

_PUNCT_RE = re.compile(r"[.,·・()\[\]{}\"“”]")
_WS_RE = re.compile(r"\s+")


def normalize_brand_key(name: str | None) -> str:
    """브랜드 원문 → 정규화된 조회 키. 빈 값이면 빈 문자열 반환."""
    if not name:
        return ""

    s = unicodedata.normalize("NFKC", name).strip().lower()
    s = s.replace("’", "'").replace("‘", "'")
    s = _PUNCT_RE.sub(" ", s)
    s = _WS_RE.sub(" ", s).strip()

    words = [w for w in s.split(" ") if w and w not in _SUFFIX_WORDS]
    # 언더스코어로 join하는 이유: product_sourcing_item.brand_group_key가 이
    # 표기(예: "trident_seafoods")를 쓰고 있어서, 오염되지 않은 브랜드명이면
    # 우연이 아니라 형식적으로도 같은 키가 나오게 맞춘 것. brand_group_key는
    # 단순 정규화가 아니라(예: "Silicone Gummy" → "silicone_gummy_mold") 지저분한
    # 원본에서 실제 브랜드를 추출한 결과라 이 함수로 재현은 안 되고, 이건 그
    # 생성 스크립트를 못 찾은 상태에서 최선의 근사치일 뿐이다.
    return "_".join(words)
