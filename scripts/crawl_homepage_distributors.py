#!/usr/bin/env python3
"""
scripts/crawl_homepage_distributors.py
제조사 홈페이지를 직접 크롤링해서 페이지 안에 주요 유통사 이름이 언급되는지
확인하는 무료(AI 미사용) 프로토타입 스크립트.

research_distributors.py(Claude 웹서치 기반, 유료)와 달리 뉴스·블로그는
보지 않고 제조사 자신의 홈페이지(+ 고객사/파트너 관련 하위 페이지 최대 2개)만
들여다본다. 비용이 전혀 들지 않아 대량(수만 건)에도 적용 가능하지만:
  - 회사가 스스로 홈페이지에 거래처를 공개한 경우만 잡아낸다 (뉴스/블로그
    근거로 찾던 것보다 재현율이 낮을 수 있음 — 특히 OEM/PB 공급사는 거래처를
    비공개로 두는 경우가 많음)
  - 단순 문자열 매칭이라 오탐 가능성이 있다 (사람이 근거 스니펫을 눈으로
    확인하는 걸 권장)
  - JS로 콘텐츠를 그리는 사이트는 이 방식(requests만 사용)으로는 못 읽는다

홈페이지 URL이 없는 행은 DuckDuckGo 검색 결과 페이지를 스크래핑해서 홈페이지를
자동으로 추정한 뒤 그 URL로 크롤링한다 (검색 API 키 없이 무료). 다만 이 방식은:
  - 상위 검색결과 1개를 그대로 신뢰하는 방식이라 잘못된 도메인을 집을 수 있다
    (결과 CSV의 homepage_source=discovered 행은 실제로 맞는 회사 홈페이지인지
    사람이 한 번 확인하는 걸 권장)
  - 검색엔진이 스크래핑을 막거나(차단/캡차) HTML 구조를 바꾸면 이 부분만
    갑자기 안 될 수 있다 — 정식 검색 API가 아니라 페이지 스크래핑이라 원래
    있던 홈페이지 크롤링보다 훨씬 불안정하다. 대량 실행 전에 소량으로 먼저
    성공률을 확인할 것을 권장한다.

필요 패키지:
  pip install requests beautifulsoup4

사용법:
  python3 scripts/crawl_homepage_distributors.py manufacturers.csv --out result.csv
  python3 scripts/crawl_homepage_distributors.py --single "회사명" "https://example.com"
  python3 scripts/crawl_homepage_distributors.py --single "회사명"   # 홈페이지 없이 회사명만

입력 CSV는 제조사명 컬럼만 있어도 되고, 홈페이지 URL 컬럼이 있으면 그걸
우선 사용한다 (없거나 비어있는 행만 자동 검색). 컬럼명은 아래 후보 중 하나면
자동으로 인식한다 (대소문자 무시):
  이름: 제조사명, 제조사, 해외제조업소, 해외 제조업소, factory, manufacturer
  URL: 홈페이지, homepage, url, 웹사이트, website
"""

import argparse
import csv
import re
import time
from urllib.parse import parse_qs, unquote, urljoin, urlparse, quote_plus

import requests
from bs4 import BeautifulSoup

# 검색결과 1위여도 회사 공식 홈페이지가 아닐 가능성이 높은 도메인들 — 후보에서 제외
_BLOCKED_DOMAINS = [
    "wikipedia.org", "facebook.com", "linkedin.com", "instagram.com", "twitter.com", "x.com",
    "youtube.com", "alibaba.com", "made-in-china.com", "bloomberg.com", "crunchbase.com",
    "yellowpages.com", "opencorporates.com", "glassdoor.com", "indeed.com", "yelp.com",
    "duckduckgo.com", "google.com", "bing.com",
]

_NAME_COLS = ["제조사명", "제조사", "해외제조업소", "해외 제조업소", "factory", "manufacturer"]
_URL_COLS = ["홈페이지", "homepage", "url", "웹사이트", "website"]

_SUBPAGE_KEYWORDS = [
    "customer", "client", "partner", "brand", "retail", "distributor", "stockist",
    "where to buy", "our story", "고객", "파트너", "거래처", "납품", "販売店", "取引先", "经销商",
]

# 유통사명 -> 검색할 표기(영문/현지어 별칭 포함)
_MAJOR_DISTRIBUTORS = {
    "Walmart":        ["walmart", "沃尔玛"],
    "Costco":         ["costco"],
    "Kroger":         ["kroger"],
    "Target":         ["target"],
    "Sam's Club":     ["sam's club", "sams club", "山姆会员店"],
    "Trader Joe's":   ["trader joe"],
    "Whole Foods":    ["whole foods"],
    "Albertsons/Safeway": ["albertsons", "safeway"],
    "Publix":         ["publix"],
    "Carrefour":      ["carrefour"],
    "Tesco":          ["tesco"],
    "Aldi":           ["aldi"],
    "Lidl":           ["lidl"],
    "Metro":          ["metro ag", "metro group"],
    "Auchan":         ["auchan"],
    "Rewe":           ["rewe"],
    "Ahold Delhaize": ["ahold delhaize", "ahold"],
    "AEON":           ["aeon", "イオン"],
    "Seven & i (세븐일레븐/이토요카도)": ["seven & i", "seven-eleven", "セブン&アイ", "セブンイレブン", "イトーヨーカドー"],
    "Life Corporation": ["life corporation", "ライフコーポレーション"],
    "Don Quijote/PPIH": ["don quijote", "donki", "ドン・キホーテ", "ppih"],
    "Alibaba/Tmall":  ["tmall", "天猫", "阿里巴巴"],
    "JD.com":         ["jd.com", "京东"],
    "Hema":           ["hema", "盒马"],
    "Yonghui":        ["yonghui", "永辉"],
    "RT-Mart":        ["rt-mart", "大润发"],
}

_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; SourcingAppResearchBot/0.1)"}


def _find_col(fieldnames: list[str], candidates: list[str]) -> str | None:
    lowered = {f.lower(): f for f in fieldnames}
    for c in candidates:
        if c.lower() in lowered:
            return lowered[c.lower()]
    return None


def load_rows(path: str) -> list[dict]:
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        name_col = _find_col(fieldnames, _NAME_COLS) or fieldnames[0]
        url_col = _find_col(fieldnames, _URL_COLS)
        rows = []
        for r in reader:
            rows.append({
                "manufacturer": (r.get(name_col) or "").strip(),
                "homepage": (r.get(url_col) or "").strip() if url_col else "",
            })
        return [r for r in rows if r["manufacturer"]]


def fetch_page(url: str, timeout: int = 10) -> BeautifulSoup | None:
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=timeout)
        if resp.status_code != 200 or not resp.text:
            return None
        return BeautifulSoup(resp.text, "html.parser")
    except requests.RequestException:
        return None


def searchable_text(soup: BeautifulSoup) -> str:
    parts = [soup.get_text(separator=" ")]
    for img in soup.find_all("img"):
        parts.append(img.get("alt") or "")
        parts.append(img.get("src") or "")
    return " ".join(parts).lower()


def find_subpage_links(soup: BeautifulSoup, base_url: str, limit: int = 2) -> list[str]:
    found = []
    base_host = urlparse(base_url).netloc
    for a in soup.find_all("a", href=True):
        label = f"{a.get_text(' ').strip()} {a['href']}".lower()
        if any(kw in label for kw in _SUBPAGE_KEYWORDS):
            full = urljoin(base_url, a["href"])
            if urlparse(full).netloc == base_host and full not in found:
                found.append(full)
        if len(found) >= limit:
            break
    return found


def discover_homepage(company_name: str) -> str | None:
    """DuckDuckGo HTML 결과 페이지를 스크래핑해서 회사 공식 홈페이지로 보이는
    첫 번째 후보 URL을 추정한다. 검색 API가 아니라 페이지 스크래핑이라
    차단/구조변경에 취약하다 — 실패하면 None."""
    query = quote_plus(company_name)
    soup = fetch_page(f"https://duckduckgo.com/html/?q={query}")
    if soup is None:
        return None

    def _resolve(a) -> str | None:
        href = a["href"]
        parsed = urlparse(href)
        if parsed.netloc == "duckduckgo.com" and parsed.path == "/l/":
            href = unquote(parse_qs(parsed.query).get("uddg", [href])[0])
            parsed = urlparse(href)
        domain = parsed.netloc.lower()
        if not domain or any(bad in domain for bad in _BLOCKED_DOMAINS):
            return None
        return href

    # 1순위: DDG HTML 검색결과 페이지의 실제 결과 제목 링크(class="result__a").
    # 페이지 전체 <a href>를 다 훑으면 내비게이션/설정 등 검색결과가 아닌
    # 링크를 먼저 집을 수 있어서, 결과 링크로 먼저 좁힌다.
    for a in soup.select("a.result__a[href]"):
        resolved = _resolve(a)
        if resolved:
            return resolved

    # 2순위: 위 클래스가 안 잡히면(페이지 구조가 바뀐 경우) /l/ 리다이렉트를
    # 거치는 링크만 대상으로 한다 — DDG는 광고/사이트 내비게이션이 아닌
    # 실제 검색결과만 이 리다이렉트를 거친다.
    for a in soup.find_all("a", href=True):
        parsed = urlparse(a["href"])
        if parsed.netloc == "duckduckgo.com" and parsed.path == "/l/":
            resolved = _resolve(a)
            if resolved:
                return resolved
    return None


def research_one(manufacturer: str, homepage: str) -> dict:
    homepage_source = "given"
    if not homepage:
        discovered = discover_homepage(manufacturer)
        if not discovered:
            return {"matched": [], "pages_checked": 0, "error": "homepage_not_found",
                     "homepage_used": "", "homepage_source": "not_found"}
        homepage = discovered
        homepage_source = "discovered"
    if not homepage.startswith("http"):
        homepage = "https://" + homepage

    pages_checked = 0
    evidence_by_distributor: dict[str, str] = {}

    home_soup = fetch_page(homepage)
    if home_soup is None:
        return {"matched": [], "pages_checked": 0, "error": "fetch_failed",
                 "homepage_used": homepage, "homepage_source": homepage_source}
    pages_checked += 1
    _record_matches(searchable_text(home_soup), homepage, evidence_by_distributor)

    for sub_url in find_subpage_links(home_soup, homepage):
        sub_soup = fetch_page(sub_url)
        if sub_soup is None:
            continue
        pages_checked += 1
        _record_matches(searchable_text(sub_soup), sub_url, evidence_by_distributor)

    return {
        "matched": [f"{name} ({url})" for name, url in evidence_by_distributor.items()],
        "pages_checked": pages_checked,
        "error": "",
        "homepage_used": homepage,
        "homepage_source": homepage_source,
    }


def _record_matches(text: str, page_url: str, evidence_by_distributor: dict[str, str]) -> None:
    for name, aliases in _MAJOR_DISTRIBUTORS.items():
        if name in evidence_by_distributor:
            continue
        for alias in aliases:
            if alias in text:
                evidence_by_distributor[name] = page_url
                break


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("input_csv", nargs="?", help="제조사명/홈페이지 컬럼을 포함한 CSV 파일 경로")
    parser.add_argument("--single", nargs="+", metavar="회사명 [홈페이지URL]",
                         help="CSV 없이 하나만 테스트. 홈페이지 URL은 생략 가능 (자동 검색)")
    parser.add_argument("--out", default="homepage_crawl_result.csv", help="결과 저장 경로")
    parser.add_argument("--sleep", type=float, default=0.5, help="요청 간 대기 시간(초)")
    args = parser.parse_args()

    if not args.single and not args.input_csv:
        parser.error("input_csv 또는 --single 중 하나는 필수입니다.")

    if args.single:
        if len(args.single) > 2:
            parser.error("--single은 '회사명'과 (선택) '홈페이지URL' 최대 2개까지만 받습니다.")
        manufacturer = args.single[0]
        homepage = args.single[1] if len(args.single) == 2 else ""
        rows = [{"manufacturer": manufacturer, "homepage": homepage}]
    else:
        rows = load_rows(args.input_csv)
        print(f"[1/2] {len(rows)}건 로드 완료")

    fieldnames = ["manufacturer", "homepage", "homepage_used", "homepage_source",
                  "matched_distributors", "pages_checked", "error"]

    with open(args.out, "w", newline="", encoding="utf-8-sig") as out_f:
        writer = csv.DictWriter(out_f, fieldnames=fieldnames)
        writer.writeheader()

        for i, row in enumerate(rows, 1):
            print(f"[크롤링 중 {i}/{len(rows)}] {row['manufacturer']} ({row['homepage'] or '홈페이지 없음 — 자동 검색'})")
            result = research_one(row["manufacturer"], row["homepage"])
            writer.writerow({
                "manufacturer": row["manufacturer"],
                "homepage": row["homepage"],
                "homepage_used": result["homepage_used"],
                "homepage_source": result["homepage_source"],
                "matched_distributors": "; ".join(result["matched"]),
                "pages_checked": result["pages_checked"],
                "error": result["error"],
            })
            out_f.flush()
            if i < len(rows):
                time.sleep(args.sleep)

    print(f"[2/2] 완료 — 결과가 {args.out}에 저장되었습니다.")


if __name__ == "__main__":
    main()
