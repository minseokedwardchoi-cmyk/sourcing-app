"""
discover_mfds_urls.py — 수입식품정보마루 API URL 탐색 스크립트

브라우저가 열리면 아래 두 메뉴를 직접 클릭하세요:
  ① 수입식품 → 일반현황 → 국가별 수입 상위 20개국 현황
  ② 종합통계 → 국가별 주요 품목  (미국 선택 후 조회)

3분 동안 기다리며 XHR을 캡처합니다.
"""
from __future__ import annotations

import asyncio
import json
from playwright.async_api import async_playwright

ORIGIN   = "https://impfood.mfds.go.kr"
MAIN_URL = f"{ORIGIN}/ifs/websquare/websquare.html?w2xPath=/ifs/ui/index.xml"
WAIT_SEC = 180   # 직접 클릭할 시간 (초)


async def discover():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        ctx  = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1400, "height": 900},
        )
        page = await ctx.new_page()
        found: dict[str, dict] = {}

        async def on_response(resp):
            url = resp.url
            if ".action" not in url:
                return
            # 세션/로그인 관련은 제외
            if any(x in url for x in ["logout", "login", "getEnviron", "getMenu", "initMain"]):
                return
            ct = resp.headers.get("content-type", "")
            if "json" not in ct and "javascript" not in ct:
                return
            try:
                body = await resp.json()
                if not isinstance(body, dict):
                    return
                req = resp.request
                found[url] = {
                    "method":    req.method,
                    "post_data": req.post_data,
                    "keys":      list(body.keys())[:10],
                    "body":      body,
                }
                print(f"\n[캡처] {url}")
                print(f"  데이터: {(req.post_data or '')[:200]}")
                print(f"  응답키: {list(body.keys())[:10]}")
            except Exception:
                pass

        page.on("response", on_response)

        print(f"브라우저 열림 → {MAIN_URL}")
        print(f"\n{'='*60}")
        print(f"지금부터 {WAIT_SEC}초 동안 브라우저에서 직접 클릭하세요:")
        print(f"  ① 수입식품 → 일반현황 → 국가별 수입 상위 20개국 현황")
        print(f"     (연도 선택 후 검색 버튼 클릭)")
        print(f"  ② 종합통계 → 국가별 주요 품목")
        print(f"     (국가 드롭다운에서 미국 선택 후 조회)")
        print(f"{'='*60}\n")

        await page.goto(MAIN_URL, timeout=30000)

        # 카운트다운 표시하며 대기
        for remaining in range(WAIT_SEC, 0, -10):
            await asyncio.sleep(10)
            print(f"  ... {remaining}초 남음 (캡처된 URL: {len(found)}개)")

        print("\n\n=== 탐색 결과 ===")
        print(f"캡처된 .action 엔드포인트: {len(found)}개\n")
        for url, info in found.items():
            print(f"URL: {url}")
            print(f"  데이터: {(info['post_data'] or '')[:300]}")
            print(f"  응답키: {info['keys']}")
            print()

        # discovered_urls.json 저장 (body 제외 — 너무 큼)
        out = {k: {"method": v["method"], "post_data": v["post_data"], "keys": v["keys"]}
               for k, v in found.items()}
        with open("discovered_urls.json", "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        print("결과가 discovered_urls.json에 저장되었습니다.")

        # 안내 출력
        top20 = [u for u in found
                 if any(x in u.lower() for x in ["top20", "nation", "ntn20", "ntnrank"])]
        items = [u for u in found
                 if any(x in u.lower() for x in ["statistic", "itmstat", "topitem"])
                 or any("itmNm" in str(found[u]["keys"]))]

        print("\n=== stats_fetcher.py 수정값 ===")
        print(f"_TOP20_URL = \"{top20[0]}\"" if top20 else "_TOP20_URL: 목록에서 20개국 관련 URL 찾아서 입력")
        print(f"_ITEMS_URL = \"{items[0]}\"" if items else "_ITEMS_URL: 목록에서 품목 통계 관련 URL 찾아서 입력")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(discover())
