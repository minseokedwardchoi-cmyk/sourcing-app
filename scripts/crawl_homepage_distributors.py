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

필요 패키지:
  pip install requests beautifulsoup4

사용법:
  python3 scripts/crawl_homepage_distributors.py manufacturers.csv --out result.csv
  python3 scripts/crawl_homepage_distributors.py --single "회사명" "https://example.com"

입력 CSV는 제조사명과 홈페이지 URL 컬럼을 포함해야 한다. 컬럼명은 아래 후보
중 하나면 자동으로 인식한다 (대소문자 무시):
  이름: 제조사명, 제조사, 해외제조업소, 해외 제조업소, factory, manufacturer
  URL: 홈페이지, homepage, url, 웹사이트, website
"""

import argparse
import csv
import re
import time
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

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


def research_one(manufacturer: str, homepage: str) -> dict:
    if not homepage:
        return {"matched": [], "pages_checked": 0, "error": "no_homepage"}
    if not homepage.startswith("http"):
        homepage = "https://" + homepage

    pages_checked = 0
    combined_text = ""
    evidence_by_distributor: dict[str, str] = {}

    home_soup = fetch_page(homepage)
    if home_soup is None:
        return {"matched": [], "pages_checked": 0, "error": "fetch_failed"}
    pages_checked += 1
    combined_text += searchable_text(home_soup)
    _record_matches(combined_text, homepage, evidence_by_distributor)

    for sub_url in find_subpage_links(home_soup, homepage):
        sub_soup = fetch_page(sub_url)
        if sub_soup is None:
            continue
        pages_checked += 1
        sub_text = searchable_text(sub_soup)
        _record_matches(sub_text, sub_url, evidence_by_distributor)

    return {
        "matched": [f"{name} ({url})" for name, url in evidence_by_distributor.items()],
        "pages_checked": pages_checked,
        "error": "",
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
    parser.add_argument("--single", nargs=2, metavar=("제조사명", "홈페이지URL"), help="CSV 없이 하나만 테스트")
    parser.add_argument("--out", default="homepage_crawl_result.csv", help="결과 저장 경로")
    parser.add_argument("--sleep", type=float, default=0.5, help="요청 간 대기 시간(초)")
    args = parser.parse_args()

    if not args.single and not args.input_csv:
        parser.error("input_csv 또는 --single 중 하나는 필수입니다.")

    if args.single:
        manufacturer, homepage = args.single
        rows = [{"manufacturer": manufacturer, "homepage": homepage}]
    else:
        rows = load_rows(args.input_csv)
        print(f"[1/2] {len(rows)}건 로드 완료")

    fieldnames = ["manufacturer", "homepage", "matched_distributors", "pages_checked", "error"]

    with open(args.out, "w", newline="", encoding="utf-8-sig") as out_f:
        writer = csv.DictWriter(out_f, fieldnames=fieldnames)
        writer.writeheader()

        for i, row in enumerate(rows, 1):
            print(f"[크롤링 중 {i}/{len(rows)}] {row['manufacturer']} ({row['homepage'] or '홈페이지 없음'})")
            result = research_one(row["manufacturer"], row["homepage"])
            writer.writerow({
                "manufacturer": row["manufacturer"],
                "homepage": row["homepage"],
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
