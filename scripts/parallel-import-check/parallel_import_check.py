# -*- coding: utf-8 -*-
"""
영문 제품명 기반 병행수입 가능성 체크.

1. 엑셀(아마존/월마트/샘스클럽/이온몰 베스트셀러)의 영문 상품명을
   sourcing-app의 /api/internal/english-lookup 으로 검색해서 후보 행들을 가져오고
2. 영문 상품명 기준 mode="contains" 매칭으로 진짜 같은 제품인지 판단하고
   (한글 sku_name은 담당자가 손으로 입력해서 표기가 제각각이라 매칭 기준으로
   쓰지 않음)
3. 매칭된 행들을 factory 기준으로만 묶어서 그 안의 distinct 수입업체 수를 센다.
   -> 1곳이면 독점, 2곳 이상이면 병행수입 가능 후보.
   (과거엔 (factory, 한글 core_name)으로 묶었으나, 같은 제품도 한글 표기가
   갈려서 병행가능 건이 독점으로 오판정되는 사고가 있어 factory 단위로 변경함)
"""
import re
import json
import unicodedata
import difflib
import urllib.request
import urllib.parse

API_BASE = "https://sourcing-backend-ucp5.onrender.com"

# ── 텍스트 정규화 ────────────────────────────────────────────────────────────
_PUNCT_RE = re.compile(r"[.,;:!?'\"()\[\]{}\-_/\\]+")
_WS_RE = re.compile(r"\s+")


def _strip_diacritics(s: str) -> str:
    """악센트 제거 (é→e, ñ→n, ü→u 등). 스페인/이탈리아/프랑스어 브랜드명이
    크롤링 쪽엔 악센트 포함(예: "LA ESPAÑOLA"), 우리가 붙인 영문 브랜드명엔
    악센트 없이(예: "Espanola") 들어가는 경우가 실제로 있어서, 악센트
    유무만으로 브랜드 매칭이 깨지는 걸 막기 위함."""
    decomposed = unicodedata.normalize("NFKD", s)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def normalize_text(s: str) -> str:
    s = _strip_diacritics(s)
    s = s.lower()
    s = _PUNCT_RE.sub(" ", s)
    s = _WS_RE.sub(" ", s).strip()
    return s


# ── EN/IT/ES 상투어구 등가 치환 ──────────────────────────────────────────
# 이탈리아·스페인 유통사(Alcampo, Cosi Comodo, Conad 등) 올리브유 건은 DB의
# sourcing-app 수입이력이 영어로 들어올 때도, 원어(이탈리아어/스페인어)로 들어올
# 때도 있는데 크롤링 상품명은 항상 원어라서, DB가 하필 영어로 들어온 행은
# "extra virgin olive oil" ↔ "olio extra vergine di oliva" ↔ "aceite de oliva
# virgen extra"처럼 공유 단어가 전혀 없어 어떤 방식으로도 매칭이 안 되는 사고가
# 있었다(예: DE CECCO EXTRA VIRGIN OLIVE OIL vs "De Cecco Olio Extra Vergine Di
# Oliva Classico"). 상품명 전체를 일본어처럼 딕셔너리로 캐싱하기엔 과함 —
# "엑스트라버진 올리브유"라는 상투어구 하나만 3개 언어 공통 표준형으로 치환하면
# 됨. 정규화(normalize_text) 이후 텍스트에 적용.
#
# 이탈리아어 "d'oliva"(oliva 앞 모음 축약형)가 DB에는 아포스트로피 없이
# "doliva"로 통째로 입력된 경우가 실제로 있었고(DE CECCO OLIO EXTRAVERGINE
# DOLIVA CLASSICO), 크롤링 쪽은 축약 안 한 "di oliva"로 나와서 철자가 달랐다
# — 이것도 아래 패턴이 "d(i)?oliva" 형태를 전부 흡수하므로 같이 해결됨.
_EVOO_CANON_TOKEN = "xevoocanonx"
_EVOO_CANON = f" {_EVOO_CANON_TOKEN} "
_EVOO_PATTERNS = [
    # 전체 상투어구부터 먼저 치환 (더 구체적인 패턴을 먼저 적용해야
    # "aceite de oliva virgen extra" 안의 "virgen extra"만 따로 앞의
    # 짧은 패턴에 걸려 "aceite de oliva"가 남는 일이 없음)
    # extra/virgin 사이는 \s*(공백 없어도 됨)로 관대하게 — DB에 "EXTRAVIRGIN"처럼
    # 붙여쓴 경우가 실제로 있었고(Filippo Berio 건), 여기서 \s+로 빡빡하게
    # 걸어두면 그쪽만 캐노니컬 치환이 안 돼서 엑셀 쪽만 치환된 상태와 어긋나
    # 오히려 매칭이 깨지는 회귀가 있었음(공백 제거 매칭 단계보다 먼저 도는
    # 단계이므로 여기서부터 관대해야 함).
    re.compile(r"\bextra\s*virgin\s+olive\s+oil\b"),
    re.compile(r"\bolio\s+extra\s*vergine\s+d\s*i?\s*oliva\b"),
    re.compile(r"\baceite\s+de\s+oliva\s+(?:virgen\s+extra|extra\s+virgen)\b"),
    # DB(sourcing-app) 쪽 sku_name_en이 상투어구를 다 안 쓰고 축약해서
    # "CARBONELL VIRGEN EXTRA"처럼 짧게만 적어놓는 경우가 실제로 있었음
    # (전체 문구 패턴엔 안 걸리는데, 이게 안 걸리면 엑셀 쪽만 canonical
    # 토큰으로 바뀌고 DB 쪽은 원문 그대로 남아 있던 문구가 서로 어긋나서
    # 오히려 매칭이 깨지는 회귀가 있었음). 짧은 폴백 패턴은 항상 위의 전체
    # 문구 패턴들 다음에 적용되므로, 이미 치환된 자리를 다시 건드리지 않음.
    re.compile(r"\bextra\s*virgin\b"),
    re.compile(r"\bextra\s*vergine\b"),
    re.compile(r"\b(?:virgen\s+extra|extra\s+virgen)\b"),
]


def canonicalize_evoo_phrase(normalized_text: str) -> str:
    s = normalized_text
    for pat in _EVOO_PATTERNS:
        s = pat.sub(_EVOO_CANON, s)
    return _WS_RE.sub(" ", s).strip()


# ── 마케팅 필러 단어 화이트리스트 ────────────────────────────────────────
# DB(sourcing-app) sku_name_en에는 있는데 크롤링 상품명엔 없는(또는 그 반대)
# 단어 때문에 매칭이 깨지는 경우, "그 단어가 있으나 없으나 실제로 같은 제품인지"
# 사람이 확인한 것만 여기 추가한다. 절대 임의로 채우지 말 것 — 예를 들어
# "virgin"/"light"/"organic"/"refined"/"pomace" 같은 단어는 제품 등급/종류
# 자체를 바꾸므로 여기 들어가면 안 되고(예전에 Extra Virgin과 Extra Light를
# 혼동한 사고), 순수 마케팅 수식어라고 확인된 것만 넣는다.
# - "classic": Iliada("GREEK CLASSIC EXTRA VIRGIN OLIVE OIL KALAMATA PDO" vs
#   크롤링 "Iliada Kalamata PDO Extra Virgin Olive Oil..."), De Cecco 건에서
#   사용자가 "다른 라인 아니고 같은 제품"이라고 직접 확인함(2026-08-03).
_FILLER_WORDS = {"classic"}


def _strip_filler_words(normalized_text: str) -> str:
    return " ".join(w for w in normalized_text.split() if w not in _FILLER_WORDS)


# ── 개별 확정 예외(수동 매칭) ────────────────────────────────────────────
# 자동 규칙으로는 못 잡는 개별 케이스(주로 DB sku_name_en 자체의 오타/오역)를
# 사람이 눈으로 확인해서 "이 DB 행 = 이 크롤링 상품 맞음"이라고 확정한 것만
# 등록. 일반 규칙을 느슨하게 푸는 대신, 건별로만 예외를 인정하는 방식 —
# run_top40_check_TEMPLATE.py의 MANUAL_VERDICTS와 같은 철학.
# key: normalize_text(DB sku_name_en) 그대로, value: 확정 사유.
# 예) "DE CECCO NATIVES OLIVE OIL EXTRA (1L)" — DB 표기가 "Natives Olive Oil
#     Extra"로 깨져 있어("올리브유네이티브스"라는 표현은 표준 영어가 아님,
#     오타/오역으로 추정) 어떤 언어 정규화로도 De Cecco의 이탈리아어 상품명과
#     자동으로는 안 이어지지만, 사용자가 같은 제품이 맞다고 확인함(2026-08-03).
MANUAL_MATCH_OVERRIDES = {
    normalize_text("DE CECCO NATIVES OLIVE OIL EXTRA (1L)"): "사용자 확인(2026-08-03) — De Cecco Olio Extra Vergine Di Oliva와 동일 제품, DB 표기 오타/오역",
    normalize_text("DE CECCO OLIVE OIL EXTRA (1L)"): "사용자 확인(2026-08-03) — De Cecco Olio Extra Vergine Di Oliva와 동일 제품",
}


# 단어-집합 비교에서 제외할 용량/단위 토큰. "1L"(공백없음) vs "1 L"(공백있음)처럼
# 같은 용량도 표기에 따라 토큰이 "1l" 하나 또는 "1"+"l" 둘로 갈라져서 부분집합
# 검사를 깨뜨리는 걸 막기 위함 — 용량은 어차피 제품 식별 기준이 아님.
_SIZE_TOKEN_RE = re.compile(r"^\d+(\.\d+)?(ml|l|kg|g|oz|lb|fl)?$", re.IGNORECASE)


def _merge_elided_tokens(words: list) -> list:
    """이탈리아어/프랑스어류의 모음 앞 축약형("d'Oro", "l'Espanola")은
    normalize_text()가 아포스트로피를 공백으로 바꾸면서 "d oro"/"l espanola"
    처럼 외톨이 한 글자 토큰 + 다음 단어로 쪼개진다. 근데 DB(sourcing-app)
    쪽 sku_name_en엔 같은 이름이 아포스트로피 없이 그냥 "doro"/"despanola"로
    붙어 입력된 경우가 실제로 있어서(Costa D'Oro 건 — DB엔 "COSTA DORO"로
    입력됨), 토큰 단위 비교에서 "doro" ≠ "d"+"oro"로 갈라져 매칭이 깨졌다.
    외톨이 한 글자 토큰은 다음 단어에 붙여서 두 표기가 같은 토큰으로 비교되게
    한다."""
    merged = []
    i = 0
    while i < len(words):
        w = words[i]
        if len(w) == 1 and w.isalpha() and i + 1 < len(words):
            merged.append(w + words[i + 1])
            i += 2
        else:
            merged.append(w)
            i += 1
    return merged


def _content_tokens(s: str) -> set:
    words = _merge_elided_tokens([w for w in s.split() if w])
    return {w for w in words if w and not _SIZE_TOKEN_RE.match(w)}


# sku_name 뒤에 붙는 용량/단량 표기 제거 (핵심 제품명 추출용)
# 예: "베르톨리 엑스트라버진 올리브오일 (1L)" -> "베르톨리 엑스트라버진 올리브오일"
_SIZE_SUFFIX_RE = re.compile(
    r"""
    \s*\([^)]*\)\s*$                                  # 맨 끝 괄호 전체: "(1L)", "(500ml)"
    |
    \s*\d+(\.\d+)?\s*(ml|l|kg|g|oz|lb|fl\s*oz|개입|입|팩|매|정|캡슐)\s*$   # 맨 끝 숫자+단위
    """,
    re.IGNORECASE | re.VERBOSE,
)


def core_name(sku_name: str) -> str:
    prev = None
    s = sku_name.strip()
    while prev != s:
        prev = s
        s = _SIZE_SUFFIX_RE.sub("", s).strip()
    return s


# ── API 호출 ─────────────────────────────────────────────────────────────
def fetch_candidates(search_term: str, limit: int = 2000):
    q = urllib.parse.urlencode({"search": search_term, "limit": limit})
    url = f"{API_BASE}/api/internal/english-lookup?{q}"
    with urllib.request.urlopen(url, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ── 유사도 매칭 ───────────────────────────────────────────────────────────
_STOPWORDS = {"the", "a", "an", "of", "with", "for", "and", "in", "pack", "count", "fl", "oz", "ml", "l"}


def _tokens(s: str) -> set:
    return {w for w in s.split() if w and w not in _STOPWORDS and not w.replace(".", "").isdigit()}


def containment_score(long_text: str, short_text: str) -> float:
    """short_text(보통 DB의 짧은 이름)의 단어들이 long_text(엑셀의 긴 마케팅 문구)
    안에 얼마나 포함되는지 비율. 크롤링 파일 특유의 부가 설명 문구 때문에
    전체 문자열 유사도가 낮게 나오는 문제를 피하기 위함."""
    short_tokens = _tokens(short_text)
    long_tokens = _tokens(long_text)
    if not short_tokens:
        return 0.0
    return len(short_tokens & long_tokens) / len(short_tokens)


def similarity(a: str, b: str) -> float:
    """difflib 유사도와 양방향 containment 중 최댓값. 순서/길이 차이에 강건하게."""
    seq_ratio = difflib.SequenceMatcher(None, a, b).ratio()
    contain_ab = containment_score(a, b)  # b(짧은쪽 가정)가 a 안에 얼마나 있는지
    contain_ba = containment_score(b, a)
    return max(seq_ratio, contain_ab, contain_ba)


# ── "contains" 모드의 3단계 비교를 재사용 가능한 함수로 분리 ────────────────
# match_candidates()와, 브랜드+factory 폴백 매칭(match_with_brand_factory_fallback)
# 둘 다 이 함수를 쓴다. 인자는 이미 normalize_text + canonicalize_evoo_phrase +
# _strip_filler_words까지 다 적용된 상태여야 함(멱등이라 다시 걸어도 안전은
# 하지만 중복 계산이라 굳이 그럴 필요 없음).
def _contains_style_match(norm_cand: str, norm_target: str) -> bool:
    if not norm_cand or not norm_target:
        return False
    shorter, longer = (norm_cand, norm_target) if len(norm_cand) <= len(norm_target) else (norm_target, norm_cand)
    if shorter in longer:
        return True
    compact_cand = norm_cand.replace(" ", "")
    compact_target = norm_target.replace(" ", "")
    c_shorter, c_longer = (
        (compact_cand, compact_target) if len(compact_cand) <= len(compact_target) else (compact_target, compact_cand)
    )
    if c_shorter and c_shorter in c_longer:
        return True
    cand_tokens = _content_tokens(norm_cand)
    target_tokens = _content_tokens(norm_target)
    if not cand_tokens or not target_tokens:
        return False
    t_shorter, t_longer = (
        (cand_tokens, target_tokens) if len(cand_tokens) <= len(target_tokens) else (target_tokens, cand_tokens)
    )
    return len(t_shorter) >= 2 and t_shorter.issubset(t_longer)


def _brand_in_text(norm_brand: str, norm_text: str) -> bool:
    """브랜드 문자열이 factory/sku_name_en 텍스트 안에 있는지, 공백 유무에
    관용적으로 확인. 그냥 파이썬 `in`으로 딱 붙여서 비교하면 "Hunt's"→정규화
    후 "hunt s"(아포스트로피가 공백으로 바뀜)가 factory의 "HUNTS FOOD INC"
    (아포스트로피 없이 붙어있음, 정규화해도 "huntsfoodinc")처럼 공백 유무만
    다른 표기와 안 걸리는 문제가 있었음(Filippo Berio/De Cecco 건과 같은
    계열의 붙여쓰기 문제가 여기서도 재발). 공백 제거한 버전으로도 한 번 더
    확인해서 이런 표기차를 흡수한다."""
    if not norm_brand or not norm_text:
        return False
    if norm_brand in norm_text:
        return True
    return norm_brand.replace(" ", "") in norm_text.replace(" ", "")


def match_candidates(target_en: str, candidates: list, threshold: float = 0.85, brand_phrase: str | None = None,
                      exact_only: bool = False, mode: str = "contains"):
    """target_en과 일치하는 후보 행들만 반환.

    mode="contains" (기본, 권장): 정규화(소문자/구두점 제거/공백 정리) 후, 둘 중
    더 짧은 쪽 문자열이 긴 쪽 안에 "그 순서 그대로 연속으로" 통째로 들어있으면
    매칭으로 인정한다. DB의 sku_name_en은 보통 짧고(예: "Bertolli Extra Virgin
    Olive Oil"), 엑셀 쪽 영문 상품명은 마케팅 문구가 길게 붙어있어서(예: "Bertolli
    Extra Virgin Olive Oil, Rich Taste, First Cold Pressed, ... 25.4 fl oz")
    완전일치(exact_only)는 절대 안 걸리지만, 단어가 몇 개 겹치는지만 세는 유사도
    점수 방식과 달리 문구 순서/연속성을 요구하기 때문에 "Bertolli Olive Oil, Extra
    Light"(다른 제품 라인, "Extra Virgin"이라는 연속 문구 자체가 없음)같은 오탐은
    걸러진다.

    exact_only=True: 정규화 후 완전히 같은 문자열만 인정 (실전에서는 엑셀/DB 표기
    스타일이 달라 거의 항상 0건이 나와서 비추천 — 참고용으로만 남겨둠).

    mode="similarity" (구버전): threshold/brand_phrase 기반 유사도 점수 매칭.
    "Bertolli Extra Virgin Olive Oil"과 "Bertolli Olive Oil, Extra Light"를
    같은 제품으로 오인하는 사고가 있었던 방식이라 쓰지 말 것.
    """
    norm_target = _strip_filler_words(canonicalize_evoo_phrase(normalize_text(target_en)))
    norm_brand = normalize_text(brand_phrase) if brand_phrase else None
    brand_is_multiword = bool(norm_brand) and len(norm_brand.split()) > 1

    matched = []
    for row in candidates:
        cand_en = row.get("sku_name_en") or ""
        norm_cand_raw = normalize_text(cand_en)

        # 개별 확정 예외: 자동 규칙보다 먼저 체크 — 걸리면 그걸로 확정.
        if norm_cand_raw in MANUAL_MATCH_OVERRIDES:
            matched.append({**row, "_similarity": 1.0, "_manual_override": MANUAL_MATCH_OVERRIDES[norm_cand_raw]})
            continue

        norm_cand = _strip_filler_words(canonicalize_evoo_phrase(norm_cand_raw))

        if exact_only:
            if norm_cand == norm_target:
                matched.append({**row, "_similarity": 1.0})
            continue

        if mode == "contains":
            # 가드: brand_phrase가 주어졌는데 DB 텍스트에 그 브랜드가 아예
            # 없으면(예: "TOMATO KETCHUP"처럼 브랜드 없이 일반명만 있는 행)
            # 여기서 매칭시키면 안 된다. 이런 일반명 텍스트는 사실상 그
            # 카테고리의 모든 상품의 부분집합이라, 그대로 두면 전혀 다른
            # 브랜드의 크롤링 상품과도 다 걸려버리는 사고가 실제로 있었음
            # (코스타도로 검증 중 발견: 올리브유 일반명이 무관한 공장 상품과도
            # 매칭됨 / Hunt's 케첩 건에서도 "TOMATO KETCHUP"이 브랜드 확인 없이
            # 그대로 매칭되는 걸 재확인 — 올리브유 전용 EVOO 토큰 가드로는 다른
            # 카테고리를 못 막았음). 이런 행은 브랜드 식별을 factory 필드로
            # 대신하는 match_with_brand_factory_fallback() 쪽에서만 다루게
            # 하고, 여기서는 건너뛴다. brand_phrase가 없는 호출(레거시)은
            # 이 가드를 건너뛰므로 호출 측에서 가능하면 항상 brand_phrase를
            # 넘길 것.
            if norm_brand and not _brand_in_text(norm_brand, norm_cand):
                continue
            # 연속부분문자열 → 공백무시 부분문자열 → 순서무관 단어집합 3단계
            # 비교(Filippo Berio 붙여쓰기 건, ORGANIC ATLAS 단어순서 건 등에서
            # 실제로 필요했던 순서대로 관대해짐). 세부 구현은
            # _contains_style_match() 참고.
            if _contains_style_match(norm_cand, norm_target):
                matched.append({**row, "_similarity": 1.0})
            continue

        # mode == "similarity" (레거시)
        if brand_is_multiword and norm_brand not in norm_cand:
            continue
        score = similarity(norm_target, norm_cand)
        if score >= threshold:
            matched.append({**row, "_similarity": round(score, 3)})
    return sorted(matched, key=lambda r: -r["_similarity"])


def _row_key(row: dict):
    return (row.get("sku_name"), row.get("factory"), row.get("importer"), row.get("txn_date"))


def _strip_brand_phrase(normalized_text: str, normalized_brand: str) -> str:
    """이미 정규화된 텍스트에서 정규화된 브랜드 문구를 한 번(단어 경계 기준)
    제거하고 남은 부분(=상품 설명구)을 반환. 브랜드가 없으면 원본 그대로."""
    if not normalized_brand:
        return normalized_text
    pat = re.compile(r"\b" + re.escape(normalized_brand) + r"\b")
    stripped = pat.sub(" ", normalized_text, count=1)
    return _WS_RE.sub(" ", stripped).strip()


def match_with_brand_factory_fallback(target_en: str, brand: str, candidates: list, mode: str = "contains"):
    """match_candidates()에 이어 붙이는 2차 폴백.

    이마트처럼 일부 수입업체는 sourcing-app에 수입이력을 등록할 때 sku_name_en에
    브랜드명을 안 적고 "EXTRA VIRGIN OLIVE OIL"처럼 일반명만 적어놓는 경우가
    있다. 이런 행은 sku_name_en 텍스트 검색/매칭으로는 애초에 찾을 수도
    맞출 수도 없다 — 비교할 브랜드 정보 자체가 없기 때문. 대신 factory/
    manufacturer 필드엔 보통 제조사명(=브랜드인 경우가 많음, 예: 올리브유는
    "브랜드가 곧 자사 공장 이름"인 경우가 흔함)이 들어있으므로 이걸로 신원을
    확인한다.

    판정 조건 (AND, 둘 다 있어야 함):
    1. row의 factory/manufacturer에 브랜드가 포함됨 → 신원 확인
    2. row의 sku_name_en에서 (있다면) 브랜드를 뺀 상품 설명구가, 크롤링
       상품명에서 브랜드를 뺀 상품 설명구와 기존 EVOO 등가처리/필러단어
       로직으로 여전히 매칭됨 → 카테고리 가드
       (mc 필드 대신 이 방식을 쓰는 이유: 크롤링 엑셀엔 mc 같은 내부
       taxonomy가 없어서 직접 대응이 안 됨. 상품 설명구 자체가 이미 "올리브유"
       같은 카테고리 정보를 담고 있으므로, 같은 factory가 만드는 완전히 다른
       품목—예: 발사믹 식초—은 이 단계에서 자연스럽게 걸러진다.)

    sku_name_en에 브랜드가 이미 있는 행은 match_candidates()의 1차 매칭에서
    이미 걸렀어야 하므로, 여기서 다시 안 걸리는 경우(브랜드는 있는데 상품
    설명구가 다름)는 진짜 다른 제품일 가능성이 높아 폴백 대상에서 제외한다.
    """
    norm_brand = normalize_text(brand) if brand else ""
    if not norm_brand:
        return []

    primary = match_candidates(target_en, candidates, mode=mode, brand_phrase=brand)
    already_matched = {_row_key(r) for r in primary}

    target_descriptor = _strip_brand_phrase(
        _strip_filler_words(canonicalize_evoo_phrase(normalize_text(target_en))), norm_brand
    )

    fallback = []
    for row in candidates:
        if _row_key(row) in already_matched:
            continue

        cand_en = row.get("sku_name_en") or ""
        norm_cand_raw = normalize_text(cand_en)
        if _brand_in_text(norm_brand, norm_cand_raw):
            # sku_name_en에 브랜드가 이미 있는데 1차 매칭에서 안 걸렸다면
            # 상품 설명구가 안 맞는 진짜 다른 제품일 가능성이 높음 — 폴백 대상 아님.
            continue

        factory_text = normalize_text((row.get("factory") or "") + " " + (row.get("manufacturer") or ""))
        if not _brand_in_text(norm_brand, factory_text):
            continue  # 브랜드가 factory/manufacturer에도 없으면 무관한 행

        cand_descriptor = _strip_filler_words(canonicalize_evoo_phrase(norm_cand_raw))
        if _contains_style_match(cand_descriptor, target_descriptor):
            fallback.append({**row, "_similarity": 1.0, "_matched_via": "factory_brand_fallback"})

    return fallback


# ── 제조사별 수입업체 집계 ───────────────────────────────────────────────
def aggregate_by_factory(matched_rows: list):
    """
    factory 기준으로만 묶어서 distinct importer 집합을 만든다.

    과거에는 (factory, core_name(sku_name)) 기준으로 묶었으나, sku_name은
    수입업체 담당자가 손으로 입력한 한글 텍스트라 같은 제품도 표기가 갈린다
    (예: "엑스트라 버진 올리브유" vs "엑스트라버진 올리브오일", 또는 뒤에
    "/ 10002790" 같은 사내 SKU 코드가 붙는 경우 — core_name()의 용량표기
    제거 정규식이 이런 코드는 못 걸러냄). 그 결과 실제로는 2곳 이상이 수입하는
    같은 제품이 core_name 문자열 차이 때문에 여러 개의 "1곳짜리" 그룹으로
    쪼개져서 병행수입 가능 건이 독점으로 오판정되는 사고가 있었다
    (예: Filippo Berio Extra Virgin Olive Oil / SALOV S.P.A. — 선한물산·쿠팡
    2곳이 수입 중인데 sku_name 표기 차이로 3개 그룹에 각 1곳씩 잡혀 전부
    "독점"으로 나옴).

    match_candidates()가 이미 영문 상품명(mode="contains") 기준으로 "같은
    제품"이라고 확인한 행들만 여기 들어오므로, 그 안에서 다시 한글 sku_name으로
    잘게 쪼갤 필요가 없다 — factory 하나로만 묶어도 된다. core_name은 참고용
    (skus 필드)으로만 남겨서, 그룹 안에 진짜 다른 라인이 섞여 있을 경우 사람이
    skus 목록을 보고 확인할 수 있게 한다.

    반환: {factory: {"importers": set(...), "skus": set(...), "cores": set(...)}}
    """
    groups: dict[str, dict] = {}
    for row in matched_rows:
        factory = (row.get("factory") or "").strip()
        importer = (row.get("importer") or "").strip()
        sku = (row.get("sku_name") or "").strip()
        if not factory or not importer or not sku:
            continue
        g = groups.setdefault(factory, {"importers": set(), "skus": set(), "cores": set()})
        g["importers"].add(importer)
        g["skus"].add(sku)
        g["cores"].add(core_name(sku))
    return groups


def judge(groups: dict):
    """각 factory 그룹에 대해 독점/병행가능 판정."""
    results = []
    for factory, info in groups.items():
        n = len(info["importers"])
        status = "독점 (연락 불가)" if n <= 1 else f"병행수입 가능 (수입업체 {n}곳)"
        results.append({
            "factory": factory,
            "core_names": sorted(info["cores"]),
            "importer_count": n,
            "importers": sorted(info["importers"]),
            "skus": sorted(info["skus"]),
            "status": status,
        })
    return sorted(results, key=lambda r: -r["importer_count"])


# ── 비영어권(일본어 등) 원어 상품명 번역 딕셔너리 ──────────────────────────
# 이온몰(AEON, 일본) 크롤링본은 "영어(원어) 상품명" 컬럼이 실제로는 일본어
# 원문 그대로인 경우가 대부분(사이트 자체가 일본어라 "원어"=일본어가 됨).
# 페이지에도 진짜 영어 텍스트가 없어서(직접 확인함) 매칭용 영문명을 만들 수
# 없었고, 사람이 매번 수작업으로 번역할 수도 없어서 번역을 1회 배치로 미리
# 만들어 이 딕셔너리에 캐싱해둔다. 앞으로 크롤링이 새 이온몰 상품을 가져오면
# 이 딕셔너리에서 먼저 조회하고, 없는 신규 항목만 번역해서 추가하는 방식으로
# 운영한다. (상품유형과 무관한 화장품/생활용품/젓가락 등은 애초에 이 딕셔너리에
# 안 넣고 __EXCLUDE_*__ 로 표시해 별도 리포트로 뺐음 — 매칭 대상이 아님)
_JA_EN_DICT_PATH = None  # 호출 측에서 필요시 오버라이드 가능


def load_ja_en_dictionary(path: str | None = None) -> dict:
    p = path or _JA_EN_DICT_PATH or "ja_en_dictionary.json"
    with open(p, encoding="utf-8") as f:
        raw = json.load(f)
    return {k: v for k, v in raw.items() if not str(v).startswith("__EXCLUDE")}


def _has_non_latin(s: str) -> bool:
    for ch in s:
        o = ord(ch)
        if (0x3040 <= o <= 0x30FF) or (0x4E00 <= o <= 0x9FFF) or (0xAC00 <= o <= 0xD7A3):
            return True
    return False


def resolve_target_en(raw_product_name: str, ja_en_dict: dict | None = None) -> str | None:
    """원어 상품명이 비영어(주로 일본어)면 딕셔너리에서 영어 번역을 찾아 반환.
    이미 영어면 그대로 반환. 딕셔너리에 없는 신규 비영어 항목이면 None을
    반환하니, 호출 측에서 "번역 필요" 항목으로 따로 모아서 나중에 배치 번역할 것."""
    if not _has_non_latin(raw_product_name):
        return raw_product_name
    if ja_en_dict is None:
        ja_en_dict = load_ja_en_dictionary()
    return ja_en_dict.get(raw_product_name)


# ── 메인: 엑셀 상품 하나에 대해 실행 ─────────────────────────────────────
def check_product(brand_en: str, product_en: str, search_hint: str | None = None):
    """
    brand_en: 영문 브랜드명 (예: "Carapelli")
    product_en: 영문 상품명 (예: "Carapelli Original Extra Virgin Olive Oil ...")
    search_hint: DB 검색에 쓸 키워드. 기본은 브랜드명.
    """
    term = search_hint or brand_en
    candidates = fetch_candidates(term)
    matched = match_candidates(product_en, candidates, threshold=0.85, brand_phrase=brand_en)
    matched += match_with_brand_factory_fallback(product_en, brand_en, candidates)
    groups = aggregate_by_factory(matched)
    return judge(groups)


if __name__ == "__main__":
    # 사용 예시
    import sys
    brand = sys.argv[1] if len(sys.argv) > 1 else "Carapelli"
    product = sys.argv[2] if len(sys.argv) > 2 else "Carapelli Extra Virgin Olive Oil"
    results = check_product(brand, product)
    print(json.dumps(results, ensure_ascii=False, indent=2))
