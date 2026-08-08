"""Utilities for normalizing country names stored in the database."""

OTHER_COUNTRY_LABELS = {"기타(ZZ)", "기타 (ZZ)"}


def normalize_country_name(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = str(value).strip()
    if not cleaned:
        return None
    return "기타" if cleaned in OTHER_COUNTRY_LABELS else cleaned


def match_all_countries_in_text_broad(text: str | None) -> list[str]:
    """origin 자유텍스트에서 매칭되는 국가명을 전부 찾는다.

    fta_country_map.match_all_countries_in_text()는 한국이 FTA를 맺은 나라만
    인식한다(관세율 매칭용으로는 그게 맞음). 근데 MFDS 평균 수입단가 조회는
    FTA 여부와 무관하게 그 나라산 수입 실적만 있으면 되는데, FTA 목록으로
    걸러버리면 모로코처럼 FTA가 없는 나라산 상품은 origin에 나라 이름이
    또렷이 적혀 있어도 "국가 인식 실패"로 잘못 처리된다. 그래서 MFDS 단가
    조회 쪽은 stats_fetcher가 이미 갖고 있는 전체 국가코드 매핑(190여개국,
    FTA 여부 무관)을 기준으로 매칭한다."""
    if not text:
        return []
    from stats_fetcher import COUNTRY_CODE_TO_KO  # 순환 임포트 방지용 지연 임포트

    all_countries = sorted(set(COUNTRY_CODE_TO_KO.values()), key=len, reverse=True)
    found: list[str] = []
    seen: set[str] = set()
    for country in all_countries:
        if country in text and country not in seen:
            found.append(country)
            seen.add(country)
    found.sort(key=lambda c: text.find(c))
    return found
