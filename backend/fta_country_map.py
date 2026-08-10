"""
fta_country_map.py — 관세청_품목번호별 관세율표(tariff_rate.rate_type)의 협정세율
코드가 어느 원산지 국가에 적용되는지 매핑.

관세율표 원본에는 국가명이 없고 rate_type 코드(FEU1, FUS1, FCL1 ...)와
applies_country_group(1/2 구분자)만 있어서, 실제로 "이탈리아산이면 몇 %인지"를
알려면 이 코드가 어느 FTA/국가에 해당하는지 별도로 알아야 한다. 아래 매핑은
한국이 체결한 FTA 상대국 기준으로 정리한 것 — 관세율구분 코드 체계상 유추한
것이므로, 실제 계약/통관 전에는 관세청 FTA포털(customs.go.kr/ftaportalkor)에서
HS코드 기준으로 재확인할 것.

'A'(기본세율)는 국가 무관 공통 적용이라 여기 넣지 않는다 — cost_estimator.py가
후보 세율에 항상 'A'를 포함시킨다.
"""
from __future__ import annotations

# EU 27개 회원국 (한-EU FTA, rate_type='FEU1')
_EU_COUNTRIES = [
    "오스트리아", "벨기에", "불가리아", "크로아티아", "키프로스", "체코", "덴마크",
    "에스토니아", "핀란드", "프랑스", "독일", "그리스", "헝가리", "아일랜드",
    "이탈리아", "라트비아", "리투아니아", "룩셈부르크", "몰타", "네덜란드", "폴란드",
    "포르투갈", "루마니아", "슬로바키아", "슬로베니아", "스페인", "스웨덴",
]

# 한-아세안 FTA(FAS1) 대상 중 개별 FTA가 별도로 있는 베트남/인도네시아/필리핀/
# 싱가포르/캄보디아는 제외한 나머지 아세안 회원국
_ASEAN_REST = ["브루나이", "라오스", "말레이시아", "미얀마", "태국"]

# 한-중미 FTA(국가별 코드가 따로 있음)
_CENTRAL_AMERICA = {
    "FCECR1": ["코스타리카"],
    "FCEHN1": ["온두라스"],
    "FCENI1": ["니카라과"],
    "FCEPA1": ["파나마"],
    "FCESV1": ["엘살바도르"],
}

# RCEP — 한국과 별도 양자 FTA가 없던 국가들(일본 등)에 대해 RCEP으로
# 처음 관세 혜택이 생긴 케이스. 아세안/호주/뉴질랜드/중국은 이미 개별 FTA가
# 있어 보통 그쪽이 더 낮지만, 세율 비교를 위해 후보에는 포함시킨다.
_RCEP = {
    "FRCAS1": _ASEAN_REST + ["베트남", "인도네시아", "필리핀", "싱가포르", "캄보디아"],
    "FRCAU1": ["호주"],
    "FRCCN1": ["중국"],
    "FRCJP1": ["일본"],
    "FRCNZ1": ["뉴질랜드"],
}

# rate_type → 적용 국가(한글) 목록
FTA_CODE_TO_COUNTRIES: dict[str, list[str]] = {
    "FEU1": _EU_COUNTRIES,
    "FGB1": ["영국"],
    "FUS1": ["미국"],
    "FCL1": ["칠레"],
    "FCA1": ["캐나다"],
    "FCN1": ["중국"],
    "FVN1": ["베트남"],
    "FNZ1": ["뉴질랜드"],
    "FAU1": ["호주"],
    "FID1": ["인도네시아"],
    "FIN1": ["인도"],
    "FIL1": ["필리핀"],
    "FKH1": ["캄보디아"],
    "FPE1": ["페루"],
    "FSG1": ["싱가포르"],
    "FTR1": ["튀르키예"],
    "FAS1": _ASEAN_REST,
    # EFTA(스위스/노르웨이/아이슬란드/리히텐슈타인) — 코드별 정확한 국가 구분은
    # 미확인이라 EFTA 4개국 전체를 후보로 걸어두고 최저세율로 비교되게 함
    "FEFIS": ["스위스", "아이슬란드", "리히텐슈타인"],
    "FEFNO": ["노르웨이"],
    **_CENTRAL_AMERICA,
    **_RCEP,
}

# 국가명(한글) → 적용 가능한 rate_type 코드 목록 (역매핑, 캐시)
_COUNTRY_TO_CODES: dict[str, list[str]] = {}
for _code, _countries in FTA_CODE_TO_COUNTRIES.items():
    for _c in _countries:
        _COUNTRY_TO_CODES.setdefault(_c, []).append(_code)


def get_fta_codes_for_country(country_kr: str) -> list[str]:
    """원산지 국가명(한글)에 적용 가능한 협정세율(rate_type) 코드 목록.
    매칭 안 되면 빈 리스트 (기본세율 'A'만 적용된다는 뜻)."""
    if not country_kr:
        return []
    country_kr = country_kr.strip()
    return _COUNTRY_TO_CODES.get(country_kr, [])


# 국가명 길이 내림차순 — "니카라과"가 "라과" 같은 부분 문자열에 잘못 먼저
# 걸리는 걸 막기 위해 긴 이름부터 검사한다.
_KNOWN_COUNTRIES_BY_LEN = sorted(_COUNTRY_TO_CODES.keys(), key=len, reverse=True)


def match_all_countries_in_text(origin_text: str | None) -> list[str]:
    """product_sourcing_item.origin은 자유 텍스트(예: "이탈리아", "이탈리아 (풀리아)")라
    정확히 국가명만 들어있다는 보장이 없다. "다국적 블렌드(이탈리아·그리스·
    스페인 등)"처럼 국가명이 여러 개 들어있는 origin을 위해 매칭되는 국가를
    전부(중복 없이, 텍스트에 등장하는 순서대로) 반환한다.
    이 앱의 FTA 코드가 커버하는 국가명(_KNOWN_COUNTRIES_BY_LEN)만 대상이라, MFDS
    통계에는 있지만 우리가 FTA 매핑을 안 해둔 국가는 여기서 못 찾을 수 있다."""
    if not origin_text:
        return []
    found: list[str] = []
    seen: set[str] = set()
    for country in _KNOWN_COUNTRIES_BY_LEN:
        if country in origin_text and country not in seen:
            found.append(country)
            seen.add(country)
    # 텍스트에 등장하는 위치 순서로 정렬 (블렌드 문구에서 언급 순서가 보통
    # 비중 순서와 어느 정도 상관관계가 있어, 동률 처리 시 참고가 됨)
    found.sort(key=lambda c: origin_text.find(c))
    return found
