# -*- coding: utf-8 -*-
"""
병행수입 1차 판정 파이프라인(parallel_import_check.py)에 대한 독립 크로스체크 레이어.

중요: 이 파일은 1차 파이프라인의 매칭 함수를 "읽기 전용"으로 import만 해서 재사용한다.
parallel_import_check.py는 절대 수정하지 않는다. 여기서 하는 일은:
1. 1차와 동일한 매칭 함수(match_candidates/match_with_brand_factory_fallback)를 그대로
   호출해서 matched rows를 얻고(=1차와 완전히 같은 계산을 다시 함),
2. 그 matched rows가 "어떤 방식으로" 매칭됐는지(tier)를 독립적으로 분류해서,
3. 느슨한 방식(공백무시/토큰집합/factory폴백)으로만 걸린 "애매한 매칭"만 골라
   사람(또는 나중엔 LLM)이 검토할 큐로 뽑아낸다.

1차 결과(batch_results_*.json, pilot_results.json, 엑셀에 이미 쓴 값)는 이 파일이
절대 건드리지 않는다 — 검증 결과는 항상 별도 파일/시트로만 쌓는다.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 1차 파이프라인 함수 — 읽기 전용 재사용, 수정 없음
from parallel_import_check import (  # noqa: F401
    normalize_text, canonicalize_evoo_phrase, _strip_filler_words, _content_tokens,
    fetch_candidates, match_candidates, match_with_brand_factory_fallback,
    aggregate_by_factory, judge, _row_key, MANUAL_MATCH_OVERRIDES,
)


# ── 매칭 방식(tier) 독립 분류 ────────────────────────────────────────────
# parallel_import_check._contains_style_match()와 동일한 3단계 로직을 여기 별도로
# 재구현한다(원본 함수는 그대로 둠). 1차 matched row가 실제로 어느 단계에서
# 걸렸는지를, 1차 로직과 무관하게 이 파일 안에서 독립적으로 재확인하기 위함.
def classify_tier(norm_cand: str, norm_target: str) -> str | None:
    if not norm_cand or not norm_target:
        return None
    shorter, longer = (norm_cand, norm_target) if len(norm_cand) <= len(norm_target) else (norm_target, norm_cand)
    if shorter in longer:
        return "continuous"
    compact_cand = norm_cand.replace(" ", "")
    compact_target = norm_target.replace(" ", "")
    c_shorter, c_longer = (
        (compact_cand, compact_target) if len(compact_cand) <= len(compact_target) else (compact_target, compact_cand)
    )
    if c_shorter and c_shorter in c_longer:
        return "compact"
    cand_tokens = _content_tokens(norm_cand)
    target_tokens = _content_tokens(norm_target)
    if not cand_tokens or not target_tokens:
        return None
    t_shorter, t_longer = (
        (cand_tokens, target_tokens) if len(cand_tokens) <= len(target_tokens) else (target_tokens, cand_tokens)
    )
    if len(t_shorter) >= 2 and t_shorter.issubset(t_longer):
        return "token_subset"
    return None


def is_ambiguous(tier: str | None, matched_via: str) -> bool:
    """continuous(연속부분문자열) 매칭이나 사람이 이미 확정한 manual override만
    확실한 매칭으로 본다. factory 폴백은 브랜드 확인이 간접적이라 항상 애매로,
    compact/token_subset은 느슨한 방식이라 애매로 분류한다."""
    if matched_via == "manual":
        return False
    if matched_via == "factory_brand_fallback":
        return True
    return tier in ("compact", "token_subset")


def llm_judge_match(target_en: str, brand_en: str, candidate_row: dict, judge_fn=None) -> bool | None:
    """애매한 매칭 1건을 판단. judge_fn(target_en, brand_en, candidate_row) -> bool 을
    넘기면 그걸로 자동 판단(나중에 실제 Claude API 호출 함수를 여기 꽂으면 됨).
    judge_fn이 없으면 자동 판단하지 않고 None(검토 대기)을 반환 — 호출 측이 큐로
    모아서 사람이 직접 검토하게 한다."""
    if judge_fn is None:
        return None
    return judge_fn(target_en, brand_en, candidate_row)


def cross_check_verdict(target_en: str, brand_en: str, candidates: list) -> dict:
    """1차와 동일한 매칭 함수를 그대로 호출해서 matched rows를 재획득하고,
    각 row를 tier로 분류해서 confirmed(확실)/ambiguous(애매) 로 나눠 반환한다.

    원본 1차 파이프라인 코드를 전혀 바꾸지 않고 그대로 재호출하므로, 여기서 나온
    factory/importer 집합은 1차 결과(batch_results/pilot_results에 이미 저장된
    verdicts)와 반드시 동일해야 정상이다 — 다르면 그 자체가 이상 신호.
    """
    norm_target = _strip_filler_words(canonicalize_evoo_phrase(normalize_text(target_en)))

    primary = match_candidates(target_en, candidates, brand_phrase=brand_en)
    fallback = match_with_brand_factory_fallback(target_en, brand_en, candidates)

    confirmed = []
    ambiguous = []

    for row in primary:
        cand_en = row.get("sku_name_en") or ""
        norm_cand_raw = normalize_text(cand_en)
        if norm_cand_raw in MANUAL_MATCH_OVERRIDES:
            confirmed.append({**row, "_cc_tier": "manual_override", "_cc_matched_via": "manual"})
            continue
        norm_cand = _strip_filler_words(canonicalize_evoo_phrase(norm_cand_raw))
        tier = classify_tier(norm_cand, norm_target)
        row_out = {**row, "_cc_tier": tier, "_cc_matched_via": "primary"}
        if is_ambiguous(tier, "primary"):
            ambiguous.append(row_out)
        else:
            confirmed.append(row_out)

    for row in fallback:
        # match_with_brand_factory_fallback의 matched row는 이미 descriptor 비교를
        # 거쳤으므로, 여기선 factory_brand_fallback이라는 matched_via 자체가 항상
        # 애매 판정 사유가 된다(tier는 참고용으로만 재계산해서 같이 기록).
        cand_en = row.get("sku_name_en") or ""
        norm_cand = _strip_filler_words(canonicalize_evoo_phrase(normalize_text(cand_en)))
        tier = classify_tier(norm_cand, norm_target)
        row_out = {**row, "_cc_tier": tier, "_cc_matched_via": "factory_brand_fallback"}
        ambiguous.append(row_out)

    return {"confirmed": confirmed, "ambiguous": ambiguous, "all_matched": confirmed + ambiguous}
