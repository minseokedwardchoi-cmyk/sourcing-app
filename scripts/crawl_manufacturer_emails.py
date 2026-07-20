#!/usr/bin/env python3
"""
scripts/crawl_manufacturer_emails.py
제조사 홈페이지(없으면 B2B 디렉토리)에서 "회사 대표 이메일"(info@, sales@,
contact@ 등)만 찾아서 DB에 채워 넣는 스크립트. 담당자 개인 이메일은 절대
수집하지 않는다.

수집 대상을 회사 대표 이메일로만 한정하는 이유:
  - 개인 이메일은 개인정보보호법상 개인정보라 별도 동의 없이 수집·재배포하면
    법적 리스크가 있음. 반면 info@company.com 같은 법인 대표 연락처는
    회사 자체의 채널이라 문제가 없음.
  - 그래서 로컬파트가 info/contact/sales 등 통상적인 대표 계정 패턴과 일치하는
    이메일만 채택한다. 홈페이지가 있는 경우에는 추가로 이메일 도메인이 그
    홈페이지 도메인과 일치하는지도 확인한다 (다른 회사 이메일 오채택 방지).

탐색 순서 (제조사 1건 기준):
  1. 홈페이지가 있으면 직접 크롤링 (홈페이지 + 연락처/회사소개 페이지)
  2. 위에서 못 찾았거나 홈페이지 자체가 없으면, 직접 접근이 막힌 경우에 한해
     Wayback Machine(archive.org) 아카이브본으로 재시도
  3. 그래도 못 찾았으면 DuckDuckGo에서 회사명으로 검색해 공식 홈페이지로
     보이는 링크를 찾아 1~2단계를 다시 시도 (알리바바/MIC보다 차단이 덜함)
  4. 그래도 못 찾았으면 알리바바 / Made-in-China에서 회사명으로 검색해
     프로필 페이지의 이메일 또는 "공식 홈페이지" 링크를 탐색
  5. 이 중 어디서든 도메인 후보를 하나라도 찾았다면, 마지막으로 그 도메인의
     WHOIS 등록 정보에 노출된 이메일을 확인 (대표 계정 형식인 경우만 채택)

알리바바/Made-in-China는 자동화 접근을 막는 방화벽과 스크래핑 금지 약관을
운영하는 경우가 많아, 이 단계는 철저히 best-effort로 동작한다 — 차단되거나
파싱에 실패하면 조용히 건너뛰고 다음 소스로 넘어간다 (사이트 구조가 바뀌면
파싱 규칙도 다시 손봐야 할 수 있음). DuckDuckGo도 마찬가지로 차단되면 조용히
건너뛴다.

사용법:
  python3 scripts/crawl_manufacturer_emails.py                  # 기본 200건
  python3 scripts/crawl_manufacturer_emails.py --limit 500
  python3 scripts/crawl_manufacturer_emails.py --dry-run        # DB 반영 없이 결과만 출력

환경변수:
  BACKEND_URL   백엔드 주소 (기본: http://localhost:8000)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import sys
import urllib.robotparser
from urllib.parse import parse_qs, quote_plus, unquote, urljoin, urlparse

import httpx
import whois as whois_lib
from bs4 import BeautifulSoup

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

BACKEND_URL = os.environ.get("BACKEND_URL", "http://localhost:8000").rstrip("/")
USER_AGENT = "Mozilla/5.0 (compatible; SourcingAppContactBot/1.0)"
REQUEST_TIMEOUT = 10
MAX_CONTACT_PAGES = 2          # 홈페이지 외에 추가로 확인할 연락처/회사소개 페이지 수
CONCURRENCY = 5

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

CONTACT_LINK_KEYWORDS = [
    "contact", "about", "company", "profile",
    "연락처", "문의", "찾아오시는", "회사소개", "오시는",
    "会社概要", "問い合わせ", "联系我们", "关于我们",
]

HOMEPAGE_LINK_KEYWORDS = [
    "website", "homepage", "official site", "official website",
    "company website", "官网", "官方网站", "公司网站",
]

# 회사 "대표" 이메일로 볼 수 있는 로컬파트만 화이트리스트로 허용.
# 담당자 개인 이메일(예: jane.kim@, jkim@)은 여기 없으면 채택하지 않는다.
GENERIC_LOCAL_PARTS_PRIORITY = [
    "info", "contact", "contactus", "sales", "salesteam", "export", "exports",
    "trade", "trading", "inquiry", "inquiries", "general", "cs", "support",
    "customerservice", "service", "admin", "office", "hello", "marketing",
    "biz", "business", "global", "overseas", "help", "webmaster",
]
GENERIC_LOCAL_PARTS = set(GENERIC_LOCAL_PARTS_PRIORITY)

_MULTI_LABEL_TLDS = {
    "co.kr", "or.kr", "ne.kr", "go.kr", "pe.kr",
    "co.jp", "or.jp", "ne.jp",
    "com.cn", "net.cn", "org.cn",
    "com.tw", "org.tw",
    "co.uk", "org.uk",
    "com.au", "net.au", "org.au",
    "co.in", "com.br", "com.hk", "com.sg",
    "co.nz", "co.th", "co.id", "com.vn", "com.my",
}

# B2B 디렉토리 검색 URL 템플릿. 방화벽/약관상 접근이 막히면 그냥 건너뛴다.
DIRECTORY_SOURCES = [
    ("alibaba",       "https://www.alibaba.com/trade/search?tab=supplier&SearchText={q}"),
    ("made-in-china", "https://www.made-in-china.com/multi-search/{q}/F1"),
]
DIRECTORY_HOSTS = {"alibaba.com", "made-in-china.com"}

_robots_cache: dict[str, urllib.robotparser.RobotFileParser | None] = {}


def registered_domain(host: str) -> str:
    host = host.lower().strip().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    labels = host.split(".")
    if len(labels) <= 2:
        return host
    last_two = ".".join(labels[-2:])
    if last_two in _MULTI_LABEL_TLDS and len(labels) >= 3:
        return ".".join(labels[-3:])
    return last_two


def _ensure_scheme(url: str) -> str:
    url = url.strip()
    if not re.match(r"^https?://", url, re.IGNORECASE):
        url = f"http://{url}"
    return url


async def _get_robots(client: httpx.AsyncClient, base_url: str) -> urllib.robotparser.RobotFileParser | None:
    parsed = urlparse(base_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    if origin in _robots_cache:
        return _robots_cache[origin]

    rp = urllib.robotparser.RobotFileParser()
    try:
        resp = await client.get(f"{origin}/robots.txt", timeout=REQUEST_TIMEOUT)
        if resp.status_code == 200:
            rp.parse(resp.text.splitlines())
        else:
            rp = None
    except Exception:
        rp = None

    _robots_cache[origin] = rp
    return rp


async def _allowed(client: httpx.AsyncClient, url: str) -> bool:
    rp = await _get_robots(client, url)
    if rp is None:
        return True
    try:
        return rp.can_fetch(USER_AGENT, url)
    except Exception:
        return True


async def _fetch(client: httpx.AsyncClient, url: str, check_robots: bool = True) -> str | None:
    try:
        if check_robots and not await _allowed(client, url):
            log.info("robots.txt 차단: %s", url)
            return None
        resp = await client.get(url, timeout=REQUEST_TIMEOUT, follow_redirects=True)
        if resp.status_code != 200:
            return None
        content_type = resp.headers.get("content-type", "")
        if "text/html" not in content_type and "application/xhtml" not in content_type:
            return None
        return resp.text
    except Exception as e:
        log.info("페이지 요청 실패 %s: %s", url, e)
        return None


def _find_contact_links(html: str, base_url: str, same_domain_only: bool = True) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    base_host = urlparse(base_url).netloc
    candidates: list[str] = []
    seen: set[str] = set()

    for a in soup.select("a[href]"):
        href = a.get("href", "").strip()
        if not href or href.startswith("mailto:") or href.startswith("javascript:") or href.startswith("#"):
            continue
        text_and_href = f"{href} {a.get_text(' ', strip=True)}".lower()
        if not any(kw in text_and_href for kw in CONTACT_LINK_KEYWORDS):
            continue

        full_url = urljoin(base_url, href)
        if same_domain_only and urlparse(full_url).netloc != base_host:
            continue
        if full_url in seen:
            continue
        seen.add(full_url)
        candidates.append(full_url)
        if len(candidates) >= MAX_CONTACT_PAGES:
            break

    return candidates


def _extract_emails(html: str) -> set[str]:
    soup = BeautifulSoup(html, "html.parser")
    emails: set[str] = set()

    for a in soup.select('a[href^="mailto:"]'):
        addr = a["href"][len("mailto:"):].split("?")[0].strip()
        if addr:
            emails.add(addr)

    emails.update(EMAIL_RE.findall(soup.get_text(" ")))
    return {e.strip().strip(".,;:").lower() for e in emails if e.strip()}


def _is_generic_local_part(email: str) -> bool:
    local = email.split("@")[0].lower()
    local = re.sub(r"[0-9]+$", "", local)  # sales1@ 같은 변형 허용
    return local in GENERIC_LOCAL_PARTS


def _priority(email: str) -> int:
    local = re.sub(r"[0-9]+$", "", email.split("@")[0].lower())
    try:
        return GENERIC_LOCAL_PARTS_PRIORITY.index(local)
    except ValueError:
        return len(GENERIC_LOCAL_PARTS_PRIORITY)


def _pick_company_email(emails: set[str], homepage: str | None) -> str | None:
    """homepage가 있으면 도메인 일치 + 대표계정 형식을 모두 요구.
    homepage가 없으면(디렉토리에서 바로 발견한 이메일) 대표계정 형식만 확인한다."""
    home_domain = registered_domain(urlparse(_ensure_scheme(homepage)).netloc) if homepage else None

    matched: list[str] = []
    for email in emails:
        if "@" not in email:
            continue
        if not _is_generic_local_part(email):
            continue
        if home_domain:
            domain = email.split("@")[1]
            if registered_domain(domain) != home_domain:
                continue  # 도메인 불일치 → 이 회사 소유가 아닌 이메일로 보고 제외
        matched.append(email)

    if not matched:
        return None
    return sorted(matched, key=_priority)[0]


async def _wayback_snapshot_url(client: httpx.AsyncClient, url: str) -> str | None:
    try:
        resp = await client.get(
            "https://archive.org/wayback/available",
            params={"url": url},
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        snap = (data.get("archived_snapshots") or {}).get("closest")
        if snap and snap.get("available"):
            return snap.get("url")
    except Exception as e:
        log.info("Wayback Machine 조회 실패 %s: %s", url, e)
    return None


async def _crawl_homepage(client: httpx.AsyncClient, homepage: str) -> str | None:
    """홈페이지 + 연락처 페이지를 직접 크롤링. 막히거나 이메일이 없으면
    archive.org 아카이브본으로 한 번 더 시도한다."""
    homepage = _ensure_scheme(homepage)

    html = await _fetch(client, homepage)
    all_emails: set[str] = set()
    if html:
        all_emails |= _extract_emails(html)
        for url in _find_contact_links(html, homepage):
            sub_html = await _fetch(client, url)
            if sub_html:
                all_emails |= _extract_emails(sub_html)

    email = _pick_company_email(all_emails, homepage)
    if email:
        return email

    # 직접 접근이 막혔거나(robots/방화벽) 이메일을 못 찾은 경우, 아카이브본으로 재시도
    snapshot_url = await _wayback_snapshot_url(client, homepage)
    if not snapshot_url:
        return None
    snap_html = await _fetch(client, snapshot_url)
    if not snap_html:
        return None
    snap_emails = _extract_emails(snap_html)
    return _pick_company_email(snap_emails, homepage)


def _find_homepage_link(html: str, base_url: str) -> str | None:
    """B2B 디렉토리 프로필 페이지에서 '공식 홈페이지' 성격의 외부 링크를 찾는다."""
    soup = BeautifulSoup(html, "html.parser")
    base_host = urlparse(base_url).netloc

    for a in soup.select("a[href]"):
        href = a.get("href", "").strip()
        if not href.lower().startswith("http"):
            continue
        text_and_href = f"{href} {a.get_text(' ', strip=True)}".lower()
        if not any(kw in text_and_href for kw in HOMEPAGE_LINK_KEYWORDS):
            continue

        host = urlparse(href).netloc
        if not host or host == base_host:
            continue
        if any(dh in host for dh in DIRECTORY_HOSTS):
            continue  # 디렉토리 사이트 내부 링크는 제외
        return href

    return None


async def _search_directories(client: httpx.AsyncClient, company_name: str) -> tuple[str | None, str | None]:
    """알리바바/Made-in-China에서 회사명을 검색해 프로필 페이지의 공식 홈페이지
    링크나 대표 이메일을 찾는다. 차단/파싱 실패 시 조용히 다음 소스로 넘어간다."""
    if not company_name or not company_name.strip():
        return None, None

    query = quote_plus(company_name.strip())
    for source_name, template in DIRECTORY_SOURCES:
        search_url = template.format(q=query)
        search_html = await _fetch(client, search_url, check_robots=True)
        if not search_html:
            log.info("%s 검색 접근 실패/차단: %s", source_name, company_name)
            continue

        soup = BeautifulSoup(search_html, "html.parser")
        profile_link = None
        for a in soup.select("a[href]"):
            href = a.get("href", "")
            if any(k in href.lower() for k in ["company", "profile", "supplier"]):
                profile_link = urljoin(search_url, href)
                break

        if not profile_link:
            log.info("%s 검색 결과에서 프로필 링크 없음: %s", source_name, company_name)
            continue

        profile_html = await _fetch(client, profile_link)
        if not profile_html:
            continue

        homepage_link = _find_homepage_link(profile_html, profile_link)
        profile_emails = {e for e in _extract_emails(profile_html) if _is_generic_local_part(e)}
        direct_email = sorted(profile_emails, key=_priority)[0] if profile_emails else None

        if homepage_link or direct_email:
            log.info("%s에서 발견: %s → homepage=%s email=%s",
                      source_name, company_name, homepage_link, direct_email)
            return homepage_link, direct_email

    return None, None


_SEARCH_RESULT_SKIP_HOSTS = (
    "duckduckgo.com", "google.com", "bing.com",
    "wikipedia.org", "facebook.com", "linkedin.com", "instagram.com",
    "twitter.com", "x.com", "youtube.com", "pinterest.com",
    "alibaba.com", "made-in-china.com", "yellowpages.com", "tiktok.com",
)


def _unwrap_ddg_redirect(href: str) -> str | None:
    if href.startswith("//"):
        href = f"https:{href}"
    if "duckduckgo.com/l/" in href or "uddg=" in href:
        qs = parse_qs(urlparse(href).query)
        real = qs.get("uddg", [None])[0]
        return unquote(real) if real else None
    if href.startswith("http"):
        return href
    return None


async def _search_duckduckgo_homepage(client: httpx.AsyncClient, company_name: str) -> str | None:
    """DuckDuckGo HTML 검색으로 회사 공식 홈페이지로 보이는 첫 외부 링크를 찾는다.
    알리바바/MIC보다 차단이 덜하지만, 이것도 막히면 조용히 None을 반환한다."""
    if not company_name or not company_name.strip():
        return None

    query = quote_plus(company_name.strip())
    url = f"https://html.duckduckgo.com/html/?q={query}"
    html = await _fetch(client, url)
    if not html:
        return None

    soup = BeautifulSoup(html, "html.parser")
    for a in soup.select("a.result__a, a.result__url, a[href]"):
        href = a.get("href", "").strip()
        if not href:
            continue
        real_url = _unwrap_ddg_redirect(href)
        if not real_url:
            continue
        host = urlparse(real_url).netloc.lower()
        if not host or any(skip in host for skip in _SEARCH_RESULT_SKIP_HOSTS):
            continue
        return real_url

    return None


async def _whois_email(domain: str) -> str | None:
    """도메인 등록 정보에 노출된 이메일 확인. 요즘 대부분 프라이버시 보호로
    가려져 있지만, 일부 국가 도메인(.cn 등)은 실명 등록이라 노출되는 경우가
    있다. 대표 계정 형식(GENERIC_LOCAL_PARTS)인 경우에만 채택한다."""
    if not domain:
        return None
    try:
        w = await asyncio.wait_for(asyncio.to_thread(whois_lib.whois, domain), timeout=15)
    except Exception as e:
        log.info("whois 조회 실패 %s: %s", domain, e)
        return None

    raw_emails = getattr(w, "emails", None) or []
    if isinstance(raw_emails, str):
        raw_emails = [raw_emails]
    raw_emails = set(raw_emails) | set(EMAIL_RE.findall(str(w)))

    candidates = [e.lower() for e in raw_emails if _is_generic_local_part(e)]
    if not candidates:
        return None
    return sorted(candidates, key=_priority)[0]


async def crawl_one(client: httpx.AsyncClient, sem: asyncio.Semaphore, target: dict) -> dict:
    async with sem:
        homepage = (target.get("homepage") or "").strip() or None
        company_name = (target.get("manufacturer") or target.get("factory") or "").strip()
        email = None
        best_homepage = homepage

        if homepage:
            email = await _crawl_homepage(client, homepage)

        if not email:
            ddg_homepage = await _search_duckduckgo_homepage(client, company_name)
            if ddg_homepage:
                email = await _crawl_homepage(client, ddg_homepage)
                if not best_homepage:
                    best_homepage = ddg_homepage

        if not email:
            discovered_homepage, directory_email = await _search_directories(client, company_name)
            if not email and discovered_homepage:
                email = await _crawl_homepage(client, discovered_homepage)
                if not best_homepage:
                    best_homepage = discovered_homepage
            if not email and directory_email:
                email = directory_email

        if not email and best_homepage:
            domain = registered_domain(urlparse(_ensure_scheme(best_homepage)).netloc)
            email = await _whois_email(domain)

        if email:
            log.info("찾음: %s (%s) → %s", target["manufacturer"], target["factory"], email)
        else:
            log.info("못 찾음: %s (%s)", target["manufacturer"], target["factory"])

        return {**target, "email": email}


BACKEND_RETRIES = 5
BACKEND_RETRY_BACKOFF = 5  # 초, 시도마다 배로 증가


async def _call_backend_with_retry(fn):
    """백엔드 재배포 중 502/503처럼 일시적인 오류가 나면 잠깐 쉬었다가 재시도.
    재배포는 보통 몇 십 초 안에 끝나므로, 여기서 죽으면 배치 전체(최대 300건
    분량의 크롤링 결과)가 그대로 유실된다."""
    last_err = None
    for attempt in range(1, BACKEND_RETRIES + 1):
        try:
            return await fn()
        except (httpx.HTTPStatusError, httpx.TransportError) as e:
            last_err = e
            if attempt < BACKEND_RETRIES:
                wait = BACKEND_RETRY_BACKOFF * attempt
                log.warning("백엔드 호출 실패 (%d/%d), %d초 후 재시도: %s",
                            attempt, BACKEND_RETRIES, wait, e)
                await asyncio.sleep(wait)
    raise last_err


async def fetch_targets(client: httpx.AsyncClient, limit: int) -> list[dict]:
    async def _do():
        resp = await client.get(
            f"{BACKEND_URL}/api/manufacturer/email-crawl-targets",
            params={"limit": limit},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()["targets"]

    return await _call_backend_with_retry(_do)


async def submit_results(client: httpx.AsyncClient, results: list[dict]) -> dict:
    payload = {
        "results": [
            {
                "manufacturer": r["manufacturer"],
                "factory": r["factory"],
                "country": r.get("country"),
                "email": r.get("email"),
            }
            for r in results
        ]
    }

    async def _do():
        resp = await client.post(
            f"{BACKEND_URL}/api/manufacturer/email-crawl-result",
            json=payload,
            timeout=60,
        )
        resp.raise_for_status()
        return resp.json()

    return await _call_backend_with_retry(_do)


async def main_async(limit: int, dry_run: bool):
    headers = {"User-Agent": USER_AGENT}
    async with httpx.AsyncClient(headers=headers) as client:
        log.info("크롤링 대상 조회 중 (limit=%d)", limit)
        targets = await fetch_targets(client, limit)
        log.info("대상 %d건", len(targets))

        if not targets:
            log.info("크롤링할 대상이 없습니다.")
            return

        sem = asyncio.Semaphore(CONCURRENCY)
        results = await asyncio.gather(*(crawl_one(client, sem, t) for t in targets))

        found = sum(1 for r in results if r.get("email"))
        log.info("크롤링 완료: %d건 중 %d건 이메일 발견", len(results), found)

        if dry_run:
            for r in results:
                if r.get("email"):
                    print(f"{r['manufacturer']} / {r['factory']} → {r['email']}")
            log.info("--dry-run: DB에는 반영하지 않았습니다.")
            return

        report = await submit_results(client, results)
        log.info("반영 완료: %s", report["message"])


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--limit", type=int, default=200, help="한 번에 처리할 최대 제조사 수")
    parser.add_argument("--dry-run", action="store_true", help="DB에 반영하지 않고 결과만 출력")
    args = parser.parse_args()

    asyncio.run(main_async(args.limit, args.dry_run))


if __name__ == "__main__":
    main()
