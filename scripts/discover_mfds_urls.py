"""
discover_mfds_urls.py — 수입식품정보마루 API URL 탐색 스크립트

로컬(브라우저 있는 환경)에서 실행해서 실제 XHR 엔드포인트 URL을 찾는다.
찾은 URL을 backend/stats_fetcher.py의 _TOP20_URL, _ITEMS_URL 상수에 붙여 넣으면 됨.

실행:
    pip install playwright
    playwright install chromium  (또는 PLAYWRIGHT_BROWSERS_PATH 환경변수 설정)
    python scripts/discover_mfds_urls.py
"""
from __future__ import annotations

import asyncio
import json
from playwright.async_api import async_playwright

ORIGIN = "https://impfood.mfds.go.kr"
MAIN_URL = f"{ORIGIN}/ifs/websquare/websquare.html?w2xPath=/ifs/ui/index.xml"

# 찾을 메뉴 텍스트 목록
TARGET_MENUS = [
    "국가별 수입 상위 20개국 현황",
    "국가별 주요 품목",
]


async def discover():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)  # 브라우저 창 열어서 확인
        ctx = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = await ctx.new_page()

        found: dict[str, dict] = {}   # url → {method, post_data, response_keys}

        async def on_response(resp):
            url = resp.url
            if ".action" not in url:
                return
            ct = resp.headers.get("content-type", "")
            if "json" not in ct and "javascript" not in ct:
                return
            try:
                body = await resp.json()
                if isinstance(body, dict):
                    req = resp.request
                    found[url] = {
                        "method":    req.method,
                        "post_data": req.post_data,
                        "keys":      list(body.keys())[:8],
                    }
                    print(f"\n[캡처] {url}")
                    print(f"  POST data: {req.post_data}")
                    print(f"  응답 키:   {list(body.keys())[:8]}")
            except Exception:
                pass

        page.on("response", on_response)

        print(f"메인 페이지 로드: {MAIN_URL}")
        await page.goto(MAIN_URL, timeout=30000)
        await page.wait_for_timeout(3000)

        # ① 국가별 수입 상위 20개국 현황 메뉴 클릭
        print("\n[클릭 시도] 국가별 수입 상위 20개국 현황")
        try:
            loc = page.get_by_text("국가별 수입 상위 20개국 현황", exact=False).first
            await loc.click(timeout=10000)
            await page.wait_for_timeout(3000)
        except Exception as e:
            print(f"  메뉴 클릭 실패: {e}")
            print("  브라우저 창에서 직접 메뉴를 클릭해주세요. 30초 대기...")
            await page.wait_for_timeout(30000)

        # ② 국가별 주요 품목 메뉴 클릭
        print("\n[클릭 시도] 국가별 주요 품목")
        try:
            loc = page.get_by_text("국가별 주요 품목", exact=False).first
            await loc.click(timeout=10000)
            await page.wait_for_timeout(3000)
        except Exception as e:
            print(f"  메뉴 클릭 실패: {e}")
            print("  브라우저 창에서 직접 메뉴를 클릭해주세요. 30초 대기...")
            await page.wait_for_timeout(30000)

        print("\n\n=== 탐색 결과 ===")
        print(f"캡처된 .action 엔드포인트: {len(found)}개\n")
        for url, info in found.items():
            print(f"URL: {url}")
            print(f"  방식: {info['method']}")
            print(f"  데이터: {info['post_data']}")
            print(f"  응답키: {info['keys']}")
            print()

        # stats_fetcher.py 수정 안내
        top20_candidates = [u for u in found if "top20" in u.lower() or "nation" in u.lower() or "ntn" in u.lower()]
        items_candidates = [u for u in found if "statistic" in u.lower() or "itmNm" in str(found[u]["keys"])]

        print("=== stats_fetcher.py 수정 가이드 ===")
        if top20_candidates:
            print(f'_TOP20_URL  = "{top20_candidates[0]}"')
        else:
            print("_TOP20_URL: 위 목록에서 상위 20개국 관련 URL을 찾아 입력하세요.")
        if items_candidates:
            print(f'_ITEMS_URL  = "{items_candidates[0]}"')
        else:
            print("_ITEMS_URL: 위 목록에서 품목 통계 관련 URL을 찾아 입력하세요.")

        # JSON으로 저장
        out = {"found": {k: {**v, "keys": v["keys"]} for k, v in found.items()}}
        with open("discovered_urls.json", "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        print("\n결과가 discovered_urls.json에 저장되었습니다.")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(discover())
