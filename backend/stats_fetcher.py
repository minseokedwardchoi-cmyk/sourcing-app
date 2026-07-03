"""
stats_fetcher.py — 수입식품정보마루 국가별 통계 자동 수집

① 국가별 수입 상위 20개국 현황 (금액/천달러)
② 국가별 주요 수입품목 TOP10 — '월별 제조국가별 품목별 수입 현황' 화면에서
   국가/품목별 연간 합계(tot, 이미 1~12월 합산된 값)를 가져와 비중을 계산한다.

수집 방식: httpx 직접 호출만 사용 (Playwright는 배포 환경에 설치돼 있지 않음).

URL 탐색이 필요한 경우: 브라우저 개발자도구 Network 탭에서 해당 화면의
.action 요청을 확인해 아래 URL 상수를 수정한다.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import httpx
from country_utils import normalize_country_name

log = logging.getLogger(__name__)

# ── 사이트 상수 ───────────────────────────────────────────────────────────────
_ORIGIN   = "https://impfood.mfds.go.kr"
_MAIN_URL = f"{_ORIGIN}/ifs/websquare/websquare.html?w2xPath=/ifs/ui/index.xml"

# ① 국가별 수입 상위 20개국 현황
_TOP20_URL = f"{_ORIGIN}/ifs/CFSBB01F010/selectCFSBB01F060.action"

# ② 월별 제조국가별 품목별 수입 현황 (전체 국가를 한 번에 반환)
_ITEM_REPORT_URL = f"{_ORIGIN}/ifs/CFSBB01F010/selectCFSBB01F050.action"

_HEADERS = {
    "User-Agent":        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                         "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer":           _MAIN_URL,
    "Origin":            _ORIGIN,
    "Accept":            "application/json, text/javascript, */*; q=0.01",
    "Accept-Language":   "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    "X-Requested-With":  "XMLHttpRequest",
}

# 국가코드(ISO) → 한국어 국가명
COUNTRY_CODE_TO_KO: dict[str, str] = {
    "US": "미국",   "CN": "중국",   "AU": "호주",    "VN": "베트남",
    "BR": "브라질", "ES": "스페인", "TH": "태국",    "DE": "독일",
    "RU": "러시아", "NZ": "뉴질랜드","CA": "캐나다", "FR": "프랑스",
    "IT": "이탈리아","JP": "일본",  "NO": "노르웨이","CL": "칠레",
    "MY": "말레이시아","NL": "네덜란드","PE": "페루","PH": "필리핀",
    "UA": "우크라이나","AR": "아르헨티나","IN": "인도","ID": "인도네시아",
    "GB": "영국",   "MX": "멕시코",
    # DB 기존 국가 (92개 추가)
    "TW": "대만",   "GH": "가나",   "GN": "기니",   "NP": "네팔",
    "MT": "몰타",   "MN": "몽골",   "YE": "예멘",   "OM": "오만",
    "CZ": "체코",   "KE": "케냐",   "HK": "홍콩",   "GM": "감비아",
    "GR": "그리스", "DK": "덴마크", "LA": "라오스",  "RW": "르완다",
    "LY": "리비아", "MW": "말라위", "MC": "모나코",  "MA": "모로코",
    "MD": "몰도바", "MM": "미얀마", "BE": "벨기에",  "WS": "사모아",
    "SN": "세네갈", "SC": "세이셸", "SR": "수리남",  "SE": "스웨덴",
    "CH": "스위스", "DZ": "알제리", "UG": "우간다",  "EG": "이집트",
    "TN": "튀니지", "PA": "파나마", "PL": "폴란드",  "FI": "핀란드",
    "HU": "헝가리", "GT": "과테말라","NI": "니카라과","KR": "대한민국",
    "LV": "라트비아","RO": "루마니아","MR": "모리타니","MZ": "모잠비크",
    "VU": "바누아투","BY": "벨라루스","BG": "불가리아","SM": "산마리노",
    "RS": "세르비아","LK": "스리랑카","SG": "싱가포르","IE": "아일랜드",
    "AL": "알바니아","EC": "에콰도르","HN": "온두라스","UY": "우루과이",
    "IL": "이스라엘","JM": "자메이카","KH": "캄보디아","CO": "콜롬비아",
    "KI": "키리바시","CY": "키프로스","TZ": "탄자니아","TR": "튀르키예",
    "PY": "파라과이","PK": "파키스탄","PT": "포르투갈","GW": "기니비사우",
    "LT": "리투아니아","BD": "방글라데시","VE": "베네수엘라","SK": "슬로바키아",
    "SI": "슬로베니아","SL": "시에라리온","IS": "아이슬란드","EE": "에스토니아",
    "ET": "에티오피아","SV": "엘살바도르","AT": "오스트리아","KZ": "카자흐스탄",
    "CR": "코스타리카","HR": "크로아티아","MG": "마다가스카르","AE": "아랍에미리트",
    "UZ": "우즈베키스탄","KG": "키르기스스탄","PG": "파푸아뉴기니","PR": "푸에르토리코",
    "SA": "사우디아라비아","ZA": "남아프리카 공화국","TT": "트리니다드 토바고",
    "DO": "도미니카 공화국",
    # ISO 3166 전체 커버 (추가분 — MFDS 데이터 없으면 자동 스킵)
    "AF": "아프가니스탄", "AD": "안도라",       "AO": "앙골라",       "AG": "앤티가 바부다",
    "AM": "아르메니아",   "AZ": "아제르바이잔", "BS": "바하마",       "BH": "바레인",
    "BB": "바베이도스",   "BZ": "벨리즈",       "BJ": "베냉",         "BT": "부탄",
    "BO": "볼리비아",     "BA": "보스니아 헤르체고비나", "BW": "보츠와나", "BN": "브루나이",
    "BF": "부르키나파소", "BI": "부룬디",       "CV": "카보베르데",   "CM": "카메룬",
    "CF": "중앙아프리카공화국", "TD": "차드",    "KM": "코모로",       "CG": "콩고",
    "CD": "콩고민주공화국", "CU": "쿠바",       "DJ": "지부티",       "DM": "도미니카",
    "TL": "동티모르",     "GQ": "적도기니",     "ER": "에리트레아",   "SZ": "에스와티니",
    "FJ": "피지",         "GA": "가봉",         "GE": "조지아",       "GD": "그레나다",
    "GY": "가이아나",     "HT": "아이티",       "IQ": "이라크",       "IR": "이란",
    "CI": "코트디부아르", "JO": "요르단",       "KW": "쿠웨이트",     "LB": "레바논",
    "LS": "레소토",       "LR": "라이베리아",   "LI": "리히텐슈타인", "LU": "룩셈부르크",
    "MK": "북마케도니아", "MV": "몰디브",       "ML": "말리",         "MH": "마셜제도",
    "MU": "모리셔스",     "FM": "미크로네시아", "ME": "몬테네그로",   "MO": "마카오",
    "NA": "나미비아",     "NR": "나우루",       "NE": "니제르",       "NG": "나이지리아",
    "KP": "북한",         "PW": "팔라우",       "PS": "팔레스타인",   "QA": "카타르",
    "LC": "세인트루시아", "VC": "세인트빈센트 그레나딘", "ST": "상투메 프린시페",
    "SO": "소말리아",     "SS": "남수단",       "SD": "수단",
    "SY": "시리아",       "TO": "통가",         "TV": "투발루",
    "XK": "코소보",       "ZM": "잠비아",       "ZW": "짐바브웨",
    "CK": "쿡 제도",      "NC": "뉴칼레도니아", "PF": "프랑스령 폴리네시아",
    "GF": "프랑스령 기아나", "RE": "레위니옹",  "GP": "과들루프",     "MQ": "마르티니크",
    "YT": "마요트",       "GI": "지브롤터",     "FO": "페로 제도",
    "GL": "그린란드",     "BM": "버뮤다",       "KY": "케이맨 제도",
    "VG": "영국령 버진아일랜드", "VI": "미국령 버진아일랜드", "AS": "아메리칸사모아",
    "GU": "괌",           "MP": "북마리아나 제도", "AI": "앵귈라",
    "BL": "생바르텔레미", "SX": "신트마르턴",   "CW": "퀴라소",       "AW": "아루바",
}

# 한국어 국가명 → 국가코드 역매핑
KO_TO_CODE: dict[str, str] = {v: k for k, v in COUNTRY_CODE_TO_KO.items()}


# ── 결과 타입 ─────────────────────────────────────────────────────────────────

@dataclass
class CountryStat:
    country_ko: str
    country_code: str
    amount_usd_k: int
    share_pct: float


@dataclass
class TopItem:
    rank: int
    item_name: str
    chart_rate: float


@dataclass
class FetchResult:
    year: str
    top20: list[CountryStat]
    top_items: dict[str, list[TopItem]]    # country_code → items
    errors: list[str] = field(default_factory=list)


# ── httpx 수집 ───────────────────────────────────────────────────────────────

async def _init_session(client: httpx.AsyncClient) -> None:
    try:
        await client.get(_MAIN_URL, timeout=20)
    except Exception as e:
        log.debug("세션 초기화 경고 (계속 진행): %s", e)


async def _fetch_top20_httpx(client: httpx.AsyncClient, year: str) -> list[CountryStat]:
    resp = await client.post(
        _TOP20_URL,
        json={"dma_Search": {
            "stdrYear": year,
            "columnInfo": "rank,ccntNtncd,ccntNtnnm,ccnt,ccntRate,wtNtncd,wtNtnnm,wt,wtRate,amtNtncd,amtNtnnm,amt,amtRate",
            "mberNo": "", "transferYn": "",
        }},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()

    # rank=0 은 전체합계 행 — 제외하고 rank>=1 인 행만 사용
    rows = [r for r in (data.get("dlt_DataList") or []) if int(r.get("rank", 0)) >= 1]

    result: list[CountryStat] = []
    for r in rows:
        # 금액 기준 필드: amtNtnnm(국가명), amt(금액), amtRate(비중%)
        ko = str(r.get("amtNtnnm") or "").strip()
        amount = int(r.get("amt") or 0)
        pct = float(r.get("amtRate") or 0)
        if not ko:
            continue
        result.append(CountryStat(
            country_ko=normalize_country_name(ko),
            country_code=KO_TO_CODE.get(ko, ""),
            amount_usd_k=amount,
            share_pct=pct,
        ))

    log.info("상위 20개국 수집 (httpx): %d건", len(result))
    return result


async def _fetch_country_top_items_httpx(
    client: httpx.AsyncClient, year: str, top_n: int = 10
) -> dict[str, list[TopItem]]:
    """
    '월별 제조국가별 품목별 수입 현황' 화면(selectCFSBB01F050)에서 전체 국가의
    품목별 연간 수입금액을 한 번에 가져와 국가별 TOP N을 계산한다.

    응답 각 행은 (국가, 대분류/중분류/소분류) 계층이며, 레벨마다 '소계' 행과
    국가 전체 '합계' 행이 함께 내려온다:
      - itmLclsCd == "0"                     → 국가 합계 행
      - itmSclsNm이 없거나(null) "소계"인 행  → 대분류/중분류 소계 행
      - 그 외 (itmSclsNm에 실제 품목명이 있는 행) → 실제 최소 단위 품목 행

    tot는 이미 1~12월(jan~dec) 합계이므로 따로 더할 필요가 없다.
    """
    resp = await client.post(
        _ITEM_REPORT_URL,
        json={"dma_Search": {
            "stdrYear": year,
            "itmLclsCd": "", "itmLclsCdList": "",
            "itmMclsCd": "", "itmMclsCdList": "",
            "itmSclsCd": "", "itmSclsCdList": "", "itmSclsNm": "",
            "cnd": "amt",   # 금액(천달러) 기준
            "columnInfo": "ntncd,ntnnm,itmLclsCd,itmLclsNm,itmMclsCd,itmMclsNm,"
                           "itmSclsCd,itmSclsNm,tot,jan,feb,mar,apr,may,jun,jul,aug,sep,oct,nov,dec",
            "ntncd": "", "ntncdList": "",
            "mberNo": "", "transferYn": "",
        }},
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    rows = data.get("dlt_DataList") or []

    country_totals: dict[str, float] = {}
    items_by_country: dict[str, list[tuple[str, float]]] = {}

    for r in rows:
        code = str(r.get("ntncd") or "").strip()
        if not code:
            continue
        tot = float(r.get("tot") or 0)

        if str(r.get("itmLclsCd")) == "0":
            country_totals[code] = tot
            continue

        item_name = r.get("itmSclsNm")
        if not item_name or item_name == "소계":
            continue

        items_by_country.setdefault(code, []).append((str(item_name).strip(), tot))

    result: dict[str, list[TopItem]] = {}
    for code, items in items_by_country.items():
        total = country_totals.get(code) or sum(t for _, t in items)
        if not total:
            continue
        ranked = sorted(items, key=lambda kv: kv[1], reverse=True)[:top_n]
        result[code] = [
            TopItem(rank=i + 1, item_name=name, chart_rate=round(tot / total * 100, 2))
            for i, (name, tot) in enumerate(ranked)
        ]

    log.info("품목별 수입현황 수집 (httpx): %d개국", len(result))
    return result


# ── 전체 수집 파이프라인 ──────────────────────────────────────────────────────

async def fetch_all_stats(year: str | None = None) -> FetchResult:
    """①②를 모두 수집해 FetchResult로 반환."""
    import datetime
    if year is None:
        year = str(datetime.date.today().year - 1)  # 당해연도는 미완성이므로 전년도 사용

    result = FetchResult(year=year, top20=[], top_items={})

    async with httpx.AsyncClient(headers=_HEADERS, follow_redirects=True) as client:
        await _init_session(client)

        try:
            result.top20 = await _fetch_top20_httpx(client, year)
        except Exception as e:
            log.warning("top20 수집 실패: %s", e)
            result.errors.append(f"top20 실패: {e}")

        try:
            result.top_items = await _fetch_country_top_items_httpx(client, year)
        except Exception as e:
            log.warning("품목별 수입현황 수집 실패: %s", e)
            result.errors.append(f"품목 수집 실패: {e}")

    return result


# ── DB 업서트 ────────────────────────────────────────────────────────────────

async def upsert_stats_to_db(result: FetchResult, conn) -> dict:
    """
    FetchResult를 country_import_stat / country_top_item 테이블에 upsert.
    conn: SQLAlchemy AsyncConnection (engine.begin() 컨텍스트)
    """
    from sqlalchemy import text

    updated_countries = 0
    updated_items = 0

    # ① 전체 삭제 후 재삽입 — 순위 밖으로 빠진 나라는 자동 제거됨
    await conn.execute(text("DELETE FROM country_import_stat"))
    for stat in result.top20:
        if not stat.country_ko or stat.amount_usd_k == 0:
            continue
        await conn.execute(text("""
            INSERT INTO country_import_stat (country, total_amount_usd_k)
            VALUES (:country, :amount)
        """), {"country": normalize_country_name(stat.country_ko), "amount": stat.amount_usd_k})
        updated_countries += 1

    for code, items in result.top_items.items():
        ko = COUNTRY_CODE_TO_KO.get(code, code)
        for item in items:
            await conn.execute(text("""
                INSERT INTO country_top_item (country, rank, item_name, pct)
                VALUES (:country, :rank, :item_name, :pct)
                ON CONFLICT (country, rank)
                DO UPDATE SET item_name = EXCLUDED.item_name, pct = EXCLUDED.pct
            """), {
                "country":   normalize_country_name(ko),
                "rank":      item.rank,
                "item_name": item.item_name,
                "pct":       item.chart_rate,
            })
            updated_items += 1

    log.info("DB upsert 완료: 국가 %d개, 품목 %d건", updated_countries, updated_items)
    return {
        "year":              result.year,
        "countries_updated": updated_countries,
        "items_updated":     updated_items,
        "errors":            result.errors,
    }
