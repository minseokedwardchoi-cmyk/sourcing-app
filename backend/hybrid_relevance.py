from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from hybrid_config import (
    RELEVANCE_CATEGORY_INTENT_BONUS,
    RELEVANCE_CATEGORY_MISMATCH_PENALTY,
    RELEVANCE_KEYWORD_BONUS,
    RELEVANCE_MC_INTENT_BONUS,
    RELEVANCE_MC_MISMATCH_PENALTY,
)


def normalize_key(value: Optional[str]) -> str:
    return " ".join((value or "").strip().lower().split())


# High-confidence lexical aliases complement semantic similarity. Keep this
# list for true interchangeable product terms only; broad related concepts
# should continue to be handled by embeddings and taxonomy inference.
PRODUCT_SYNONYM_GROUPS: tuple[tuple[str, ...], ...] = (
    ("어묵", "오뎅"),
)


def expand_query_terms(query: str) -> tuple[str, ...]:
    normalized = normalize_key(query)
    if not normalized:
        return ()
    expanded = [normalized]
    for group in PRODUCT_SYNONYM_GROUPS:
        normalized_group = tuple(normalize_key(term) for term in group)
        if not any(term in normalized for term in normalized_group):
            continue
        for term in normalized_group:
            if term not in expanded:
                expanded.append(term)
    return tuple(expanded)


@dataclass(frozen=True)
class IntentRule:
    triggers: tuple[str, ...]
    mc_intent: Optional[str]
    category_intent: tuple[str, ...]
    keyword_terms: tuple[str, ...]


# Minimal MVP intent rules. Not exhaustive - extend this table as new
# search intents need MC/category-aware bonuses, rather than hardcoding
# per-query logic elsewhere.
INTENT_RULES: tuple[IntentRule, ...] = (
    IntentRule(
        triggers=("참치캔", "캔참치", "참치 캔", "캔 참치", "canned tuna", "tuna can"),
        mc_intent="참치",
        category_intent=("가공식품", "통조림"),
        keyword_terms=("참치", "tuna"),
    ),
    IntentRule(
        triggers=("냉동", "frozen", "fillet", "필렛"),
        mc_intent=None,
        category_intent=("수산물",),
        keyword_terms=("냉동", "필렛", "frozen", "fillet"),
    ),
    IntentRule(
        triggers=("김치", "kimchi"),
        mc_intent="김치",
        category_intent=("가공식품",),
        keyword_terms=("김치", "kimchi"),
    ),
)


@dataclass(frozen=True)
class QueryIntent:
    mc_intent: Optional[str]
    category_intent: tuple[str, ...]
    keyword_terms: tuple[str, ...]


NULL_INTENT = QueryIntent(mc_intent=None, category_intent=(), keyword_terms=())


def detect_intent(query: str) -> QueryIntent:
    normalized = normalize_key(query)
    if not normalized:
        return NULL_INTENT

    mc_intent: Optional[str] = None
    category_intent: list[str] = []
    keyword_terms: list[str] = []
    for rule in INTENT_RULES:
        if not any(trigger in normalized for trigger in rule.triggers):
            continue
        if mc_intent is None and rule.mc_intent:
            mc_intent = normalize_key(rule.mc_intent)
        for cat in rule.category_intent:
            cat_key = normalize_key(cat)
            if cat_key not in category_intent:
                category_intent.append(cat_key)
        for kw in rule.keyword_terms:
            kw_key = normalize_key(kw)
            if kw_key not in keyword_terms:
                keyword_terms.append(kw_key)

    return QueryIntent(
        mc_intent=mc_intent,
        category_intent=tuple(category_intent),
        keyword_terms=tuple(keyword_terms),
    )


@dataclass(frozen=True)
class RelevanceBreakdown:
    mc_intent_bonus: float = 0.0
    category_intent_bonus: float = 0.0
    best_keyword_bonus: float = 0.0
    mc_mismatch_penalty: float = 0.0
    category_mismatch_penalty: float = 0.0


def compute_relevance_components(
    *,
    mc: Optional[str],
    category: Optional[str],
    sku_name: Optional[str],
    intent: QueryIntent,
) -> RelevanceBreakdown:
    mc_key = normalize_key(mc)
    category_key = normalize_key(category)
    sku_key = normalize_key(sku_name)

    mc_intent_bonus = 0.0
    mc_mismatch_penalty = 0.0
    if intent.mc_intent:
        if mc_key == intent.mc_intent:
            mc_intent_bonus = RELEVANCE_MC_INTENT_BONUS
        elif mc_key:
            mc_mismatch_penalty = RELEVANCE_MC_MISMATCH_PENALTY

    category_intent_bonus = 0.0
    category_mismatch_penalty = 0.0
    if intent.category_intent:
        if category_key in intent.category_intent:
            category_intent_bonus = RELEVANCE_CATEGORY_INTENT_BONUS
        elif category_key:
            category_mismatch_penalty = RELEVANCE_CATEGORY_MISMATCH_PENALTY

    best_keyword_bonus = 0.0
    if intent.keyword_terms and category_mismatch_penalty == 0.0:
        if any(kw in sku_key for kw in intent.keyword_terms):
            best_keyword_bonus = RELEVANCE_KEYWORD_BONUS

    return RelevanceBreakdown(
        mc_intent_bonus=mc_intent_bonus,
        category_intent_bonus=category_intent_bonus,
        best_keyword_bonus=best_keyword_bonus,
        mc_mismatch_penalty=mc_mismatch_penalty,
        category_mismatch_penalty=category_mismatch_penalty,
    )


def clamp_relevance_score(semantic_score: float, breakdown: RelevanceBreakdown) -> float:
    raw = (
        semantic_score
        + breakdown.mc_intent_bonus
        + breakdown.category_intent_bonus
        + breakdown.best_keyword_bonus
        - breakdown.mc_mismatch_penalty
        - breakdown.category_mismatch_penalty
    )
    return max(0.0, min(1.0, raw))
