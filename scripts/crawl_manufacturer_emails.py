#!/usr/bin/env python3
"""
scripts/crawl_manufacturer_emails.py
제조사 홈페이지에서 "회사 대표 이메일"(info@, sales@, contact@ 등)만 찾아서
DB에 채워 넣는 스크립트. 담당자 개인 이메일은 절대 수집하지 않는다.

수집 대상을 회사 대표 이메일로만 한정하는 이유:
  - 개인 이메일은 개인정보보호법상 개인정보라 별도 동의 없이 수집·재배포하면
    법적 리스크가 있음. 반면 info@company.com 같은 법인 대표 연락처는
    회사 자체의 채널이라 문제가 없음.
  - 그래서 다음 두 조건을 모두 만족하는 이메일만 채택한다:
    ① 이메일 도메인이 그 제조사 홈페이지 도메인과 일치
    ② 로컬파트가 info/contact/sales 등 통상적인 대표 계정 패턴과 일치

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
from urllib.parse import urljoin, urlparse

import httpx
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


async def _fetch(client: httpx.AsyncClient, url: str) -> str | None:
    try:
        if not await _allowed(client, url):
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


def _find_contact_links(html: str, base_url: str) -> list[str]:
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
        if urlparse(full_url).netloc != base_host:
            continue  # 같은 도메인만
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


def _pick_company_email(emails: set[str], homepage: str) -> str | None:
    home_domain = registered_domain(urlparse(homepage).netloc or urlparse(f"//{homepage}").netloc)
    if not home_domain:
        return None

    matched: list[str] = []
    for email in emails:
        if "@" not in email:
            continue
        local, _, domain = email.partition("@")
        if registered_domain(domain) != home_domain:
            continue  # 도메인 불일치 → 이 회사 소유가 아닌 이메일로 보고 제외
        local_key = re.sub(r"[0-9]+$", "", local.lower())  # sales1@ 같은 변형 허용
        if local_key in GENERIC_LOCAL_PARTS:
            matched.append(email)

    if not matched:
        return None

    def priority(email: str) -> int:
        local = re.sub(r"[0-9]+$", "", email.split("@")[0].lower())
        try:
            return GENERIC_LOCAL_PARTS_PRIORITY.index(local)
        except ValueError:
            return len(GENERIC_LOCAL_PARTS_PRIORITY)

    return sorted(matched, key=priority)[0]


async def crawl_one(client: httpx.AsyncClient, sem: asyncio.Semaphore, target: dict) -> dict:
    homepage = target["homepage"].strip()
    if not re.match(r"^https?://", homepage, re.IGNORECASE):
        homepage = f"http://{homepage}"

    async with sem:
        html = await _fetch(client, homepage)
        if html is None:
            return {**target, "email": None}

        all_emails = _extract_emails(html)
        contact_urls = _find_contact_links(html, homepage)
        for url in contact_urls:
            sub_html = await _fetch(client, url)
            if sub_html:
                all_emails.update(_extract_emails(sub_html))

        email = _pick_company_email(all_emails, homepage)
        if email:
            log.info("찾음: %s (%s) → %s", target["manufacturer"], target["factory"], email)
        else:
            log.info("못 찾음: %s (%s)", target["manufacturer"], target["factory"])
        return {**target, "email": email}


async def fetch_targets(client: httpx.AsyncClient, limit: int) -> list[dict]:
    resp = await client.get(
        f"{BACKEND_URL}/api/manufacturer/email-crawl-targets",
        params={"limit": limit},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["targets"]


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
    resp = await client.post(
        f"{BACKEND_URL}/api/manufacturer/email-crawl-result",
        json=payload,
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()


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
