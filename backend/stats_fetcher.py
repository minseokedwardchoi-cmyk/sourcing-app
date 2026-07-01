"""
stats_fetcher.py — 수입식품정보마루 국가별 통계 자동 수집

① 국가별 수입 상위 20개국 현황 (금액/천달러)
② 각 국가별 주요 수입품목 TOP10 (chartRate 비중 기준)

수집 방식:
  1순위: httpx 직접 호출 (세션 쿠키 선취득)  ← Render 서버 배포 시 사용
  2순위: Playwright 브라우저 자동화           ← httpx가 403 받을 때 fallback

URL 탐색이 필요한 경우:
  python scripts/discover_mfds_urls.py 실행 후 _TOP20_URL, _ITEMS_URL 상수 수정.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

log = logging.getLogger(__name__)

# ── 사이트 상수 ───────────────────────────────────────────────────────────────
_ORIGIN   = "https://impfood.mfds.go.kr"
_MAIN_URL = f"{_ORIGIN}/ifs/websquare/websquare.html?w2xPath=/ifs/ui/index.xml"

# ① 국가별 수입 상위 20개국 현황
_TOP20_URL = f"{_ORIGIN}/ifs/CFSBB01F010/selectCFSBB01F060.action"

# ② 국가별 주요품목 통계
_ITEMS_URL = f"{_ORIGIN}/ifs/CFDAA07F010/selectStatistics.action"

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


# ── 파싱 유틸 ─────────────────────────────────────────────────────────────────

def _parse_amount(raw: Any) -> int:
    if raw is None:
        return 0
    return int(str(raw).replace(",", "").strip() or "0")


def _first_list(data: dict) -> list:
    """응답 dict에서 첫 번째 list 값을 반환."""
    for v in data.values():
        if isinstance(v, list) and v:
            return v
    return []


def _find_list_with_key(data: dict, key: str) -> list:
    """특정 키를 포함하는 첫 번째 list를 반환."""
    for v in data.values():
        if isinstance(v, list) and v and key in v[0]:
            return v
    return []


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
            country_ko=ko,
            country_code=KO_TO_CODE.get(ko, ""),
            amount_usd_k=amount,
            share_pct=pct,
        ))

    log.info("상위 20개국 수집 (httpx): %d건", len(result))
    return result


async def _fetch_items_httpx(
    client: httpx.AsyncClient, year: str, code: str, top_n: int = 10
) -> list[TopItem]:
    resp = await client.post(
        _ITEMS_URL,
        json={"dma_Search": {"columnInfo": "", "stdrYear": year, "ntncd": code, "mberNo": "", "transferYn": ""}},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()

    rows = (
        data.get("dlt_DataList6")
        or data.get("dlt_DataList5")
        or _find_list_with_key(data, "itmNm")
        or _first_list(data)
    )

    items = []
    for r in sorted(rows, key=lambda x: int(x.get("rank", 999)))[:top_n]:
        items.append(TopItem(
            rank=int(r.get("rank", 0)),
            item_name=str(r.get("itmNm", "")).strip(),
            chart_rate=float(r.get("chartRate", 0)),
        ))
    log.info("국가 %s 주요품목 수집 (httpx): %d건", code, len(items))
    return items


# ── Playwright fallback ──────────────────────────────────────────────────────

async def _fetch_with_playwright(year: str) -> tuple[list[CountryStat], dict[str, list[TopItem]]]:
    """
    httpx가 403을 받을 경우 Playwright로 실제 브라우저를 구동해 XHR을 가로챈다.
    playwright 패키지와 chromium이 설치된 환경에서만 작동.
    """
    from playwright.async_api import async_playwright

    top20: list[CountryStat] = []
    top_items: dict[str, list[TopItem]] = {}

    chromium_path = "/opt/pw-browsers/chromium"  # 서버 환경 기본 경로

    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(
                executable_path=chromium_path, headless=True
            )
        except Exception:
            browser = await p.chromium.launch(headless=True)  # 로컬 설치 사용

        ctx = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = await ctx.new_page()
        captured: dict[str, Any] = {}

        async def on_response(resp):
            url = resp.url
            if ".action" not in url:
                return
            try:
                body = await resp.json()
                if isinstance(body, dict):
                    captured[url] = {"body": body, "post_data": resp.request.post_data}
            except Exception:
                pass

        page.on("response", on_response)

        await page.goto(_MAIN_URL, timeout=30000)
        await page.wait_for_timeout(3000)

        # ① 국가별 상위 20개국 메뉴 클릭
        try:
            await page.get_by_text("국가별 수입 상위 20개국 현황", exact=False).first.click(timeout=8000)
            await page.wait_for_timeout(3000)
        except Exception as e:
            log.warning("상위 20개국 메뉴 클릭 실패: %s", e)

        # ② 국가별 주요 품목 메뉴 클릭 + 미국 데이터 수집
        try:
            await page.get_by_text("국가별 주요 품목", exact=False).first.click(timeout=8000)
            await page.wait_for_timeout(3000)
        except Exception as e:
            log.warning("국가별 주요품목 메뉴 클릭 실패: %s", e)

        await browser.close()

        # 캡처된 응답 파싱
        for url, info in captured.items():
            body = info["body"]
            post = info.get("post_data") or ""

            # top20 판별
            if "ntnNm" in str(body) or ("col1" in str(body) and "col3" in str(body)):
                rows = _first_list(body)
                total_amt = sum(_parse_amount(r.get("sumAmt") or r.get("col3") or "0") for r in rows)
                for r in rows:
                    ko = (r.get("ntnNm") or r.get("col1") or "").strip()
                    amt = _parse_amount(r.get("sumAmt") or r.get("col3") or "0")
                    top20.append(CountryStat(
                        country_ko=ko,
                        country_code=KO_TO_CODE.get(ko, ""),
                        amount_usd_k=amt,
                        share_pct=round(amt / total_amt * 100, 1) if total_amt else 0.0,
                    ))

            # 품목 데이터 판별
            item_rows = _find_list_with_key(body, "itmNm")
            if item_rows:
                # post_data에서 ntncd 추출
                code = ""
                for part in post.split("&"):
                    if part.startswith("ntncd="):
                        code = part.split("=", 1)[1]
                        break
                if not code:
                    code = "US"   # 기본값
                items = [
                    TopItem(
                        rank=int(r.get("rank", 0)),
                        item_name=str(r.get("itmNm", "")).strip(),
                        chart_rate=float(r.get("chartRate", 0)),
                    )
                    for r in sorted(item_rows, key=lambda x: int(x.get("rank", 999)))[:10]
                ]
                top_items[code] = items

    log.info("Playwright 수집 완료: top20=%d개국, 품목=%d국", len(top20), len(top_items))
    return top20, top_items


# ── 전체 수집 파이프라인 ──────────────────────────────────────────────────────

async def fetch_all_stats(
    year: str | None = None,
    extra_codes: list[str] | None = None,
) -> FetchResult:
    """
    ①②를 모두 수집해 FetchResult로 반환.
    extra_codes: MFDS 상위 20개국 외에 추가로 품목을 수집할 국가코드 목록.
                 /api/refresh-country-stats 가 DB의 모든 국가를 넘겨준다.
    """
    import datetime
    if year is None:
        year = str(datetime.date.today().year - 1)  # 당해연도는 미완성이므로 전년도 사용

    result = FetchResult(year=year, top20=[], top_items={})

    # 1순위: httpx
    httpx_ok = False
    async with httpx.AsyncClient(headers=_HEADERS, follow_redirects=True) as client:
        await _init_session(client)

        try:
            result.top20 = await _fetch_top20_httpx(client, year)
            httpx_ok = True
        except Exception as e:
            log.warning("httpx top20 수집 실패: %s", e)
            result.errors.append(f"top20 httpx 실패: {e}")

        # 품목 수집 대상: MFDS 상위 20개국 + 호출자가 넘긴 추가 국가 (중복 제거)
        top20_codes = [s.country_code for s in result.top20 if s.country_code]
        codes = list(dict.fromkeys(top20_codes + (extra_codes or [])))
        if not codes:
            codes = list(COUNTRY_CODE_TO_KO.keys())

        item_errors = 0
        for code in codes:
            try:
                items = await _fetch_items_httpx(client, year, code)
                if items:
                    result.top_items[code] = items
                await asyncio.sleep(0.3)
            except Exception as e:
                log.warning("국가 %s 품목 httpx 실패: %s", code, e)
                item_errors += 1

        if item_errors == len(codes):
            httpx_ok = False

    # 2순위: Playwright (httpx 완전 실패 시)
    if not httpx_ok and not result.top20 and not result.top_items:
        log.info("Playwright fallback 시도...")
        try:
            pw_top20, pw_items = await _fetch_with_playwright(year)
            if pw_top20:
                result.top20 = pw_top20
            result.top_items.update(pw_items)
        except Exception as e:
            msg = f"Playwright fallback 실패: {e}"
            log.error(msg)
            result.errors.append(msg)

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
        """), {"country": stat.country_ko, "amount": stat.amount_usd_k})
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
                "country":   ko,
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
