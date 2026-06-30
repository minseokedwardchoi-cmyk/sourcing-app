"""
crawler.py — 수입식품정보마루 크롤링 + OEM 마킹 + MC 변환
"""
from __future__ import annotations

import asyncio
import io
import logging
import re
from datetime import date, timedelta
from pathlib import Path

import httpx
import pandas as pd
from playwright.async_api import async_playwright

log = logging.getLogger(__name__)

TARGET_URL  = "https://impfood.mfds.go.kr/CFCCC01F01"
PAGE_DELAY  = 1.5
_MC_MAP_PATH = Path(__file__).parent / "mc_mapping.csv"


def load_mc_mapping() -> dict[str, str]:
    if not _MC_MAP_PATH.exists():
        log.warning("mc_mapping.csv 없음")
        return {}
    df = pd.read_csv(_MC_MAP_PATH, encoding="utf-8-sig")
    mapping = dict(zip(df["품목(유형)"].str.strip(), df["MC"].str.strip()))
    log.info("MC 매핑 로드: %d개", len(mapping))
    return mapping


async def open_search_page(page, start: str, end: str, oem: bool = False):
    await page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=90_000)
    await asyncio.sleep(3)
    await page.evaluate(f"""
        document.getElementById('srchStrtDt').value = '{start}';
        document.getElementById('srchEndDt').value = '{end}';
    """)
    if oem:
        checked = await page.evaluate("document.getElementById('oemFoodYn').checked")
        if not checked:
            await page.evaluate("document.getElementById('oemFoodYn').click()")
    await page.evaluate("fnSearch(1)")
    await asyncio.sleep(5)


async def download_full_excel(page, start: str, end: str) -> pd.DataFrame:
    await open_search_page(page, start, end, oem=False)
    log.info("전체 엑셀 다운로드 시작")
    import tempfile, os
    dl_dir = Path(tempfile.mkdtemp())
    async with page.expect_download(timeout=120_000) as dl_info:
        await page.click("button[onclick='fnGetExcelFile();']", timeout=15_000, no_wait_after=True)
    download = await dl_info.value
    dest = dl_dir / f"full_{start}_{end}.xlsx"
    await download.save_as(str(dest))
    df = pd.read_excel(dest, engine="openpyxl")
    log.info("전체 다운로드 행 수: %d", len(df))
    return df


async def crawl_oem_data(page, start: str, end: str) -> pd.DataFrame:
    await open_search_page(page, start, end, oem=True)
    await page.evaluate("fnPageLimit(50)")
    await asyncio.sleep(5)
    try:
        await page.wait_for_selector("table tbody tr", timeout=15_000)
    except Exception:
        pass

    total_count = 0
    try:
        count_text = await page.inner_text("span.total, .total-count, #totalCount, .list-count")
        total_count = int("".join(filter(str.isdigit, count_text)))
    except Exception:
        try:
            body = await page.inner_text("body")
            m = re.search(r"총\s*([\d,]+)\s*건", body)
            if m:
                total_count = int(m.group(1).replace(",", ""))
        except Exception:
            pass

    if total_count == 0:
        log.warning("전체 건수 파악 실패 — 페이지 끝날 때까지 크롤링")
        total_pages = 9999
    else:
        total_pages = (total_count + 49) // 50
        log.info("OEM 전체 %d건 → %d페이지", total_count, total_pages)

    COLS = ["구분", "수입업체", "제품명(한글)", "제품명(영문)",
            "품목(유형)", "제조/작업/수출업소", "처리일자", "소비기한", "제조국", "수출국"]
    rows_all = []

    for page_num in range(1, total_pages + 1):
        if page_num > 1:
            await page.evaluate(f"fnSearch({page_num})")
            await asyncio.sleep(PAGE_DELAY)

        log.info("OEM 크롤링 중: %d / %d 페이지", page_num, total_pages)
        try:
            await page.wait_for_selector("table tbody tr", timeout=15_000)
        except Exception:
            log.info("테이블 없음 — 종료")
            break
        tr_els = await page.query_selector_all("table tbody tr")
        if not tr_els:
            log.info("빈 페이지 — 종료")
            break

        for tr in tr_els:
            tds = await tr.query_selector_all("td")
            values = [await td.inner_text() for td in tds]
            values = [v.strip() for v in values]
            if not any(values):
                continue
            if len(values) < len(COLS):
                values += [""] * (len(COLS) - len(values))
            rows_all.append(values[:len(COLS)])

    if not rows_all:
        return pd.DataFrame()
    df = pd.DataFrame(rows_all, columns=COLS)
    log.info("OEM 크롤링 완료: %d건", len(df))
    return df


def normalize_str(s) -> str:
    if s is None or (isinstance(s, float) and pd.isna(s)):
        return ""
    return str(s).strip()


MATCH_KEYS_FULL = ["구분", "수입업체", "제품명(한글)", "제품명(영문)",
                   "품목(유형)", "해외제조업소", "처리일자", "소비기한", "제조국", "수출국"]
MATCH_KEYS_OEM  = ["구분", "수입업체", "제품명(한글)", "제품명(영문)",
                   "품목(유형)", "제조/작업/수출업소", "처리일자", "소비기한", "제조국", "수출국"]


def build_oem_set(oem_df: pd.DataFrame) -> set:
    result = set()
    for _, row in oem_df.iterrows():
        key = tuple(normalize_str(row.get(c, "")) for c in MATCH_KEYS_OEM)
        result.add(key)
    return result


def mark_and_transform(full_df: pd.DataFrame, oem_set: set, mc_map: dict) -> pd.DataFrame:
    unmapped = set()
    records = []
    for _, row in full_df.iterrows():
        key = tuple(normalize_str(row.get(c, "")) for c in MATCH_KEYS_FULL)
        is_oem = key in oem_set
        품목 = normalize_str(row.get("품목(유형)"))
        mc = mc_map.get(품목)
        if mc is None and 품목:
            unmapped.add(품목)
        records.append({
            "구분":       normalize_str(row.get("구분")),
            "MC":        mc,
            "제품명(한글)": normalize_str(row.get("제품명(한글)")),
            "수입업체":    normalize_str(row.get("수입업체")),
            "OEM여부":    "O" if is_oem else None,
            "해외제조업소": normalize_str(row.get("해외제조업소")),
            "제조국":      normalize_str(row.get("제조국")),
            "이메일":      None,
            "처리일자":    normalize_str(row.get("처리일자")),
        })
    result_df = pd.DataFrame(records, columns=[
        "구분", "MC", "제품명(한글)", "수입업체", "OEM여부", "해외제조업소", "제조국", "이메일", "처리일자"
    ])
    log.info("OEM 마킹: %d / %d건", result_df["OEM여부"].notna().sum(), len(result_df))
    log.info("MC 변환: %d / %d건", result_df["MC"].notna().sum(), len(result_df))
    if unmapped:
        log.warning("MC 매핑 없는 품목 %d종: %s", len(unmapped), ", ".join(sorted(unmapped)[:20]))
    return result_df


async def run_crawl(start: str, end: str, backend_url: str) -> dict:
    """크롤링 전체 파이프라인 실행 후 결과 반환"""
    log.info("=== 크롤링 시작: %s ~ %s ===", start, end)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            accept_downloads=True,
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        )
        page1 = await context.new_page()
        full_df = await download_full_excel(page1, start, end)

        page2 = await context.new_page()
        oem_df = await crawl_oem_data(page2, start, end)
        await browser.close()

    oem_set = build_oem_set(oem_df) if not oem_df.empty else set()
    mc_map = load_mc_mapping()
    result_df = mark_and_transform(full_df, oem_set, mc_map)

    # 업로드
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        result_df.to_excel(writer, index=False, sheet_name="시트1")
    buf.seek(0)
    filename = f"import_{start}_{end}.xlsx"

    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            f"{backend_url}/api/upload",
            files={"file": (filename, buf, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        )

    if resp.status_code != 200:
        raise RuntimeError(f"업로드 실패 HTTP {resp.status_code}: {resp.text}")

    log.info("=== 완료: %d건 업로드 ===", len(result_df))
    return {"rows": len(result_df), "start": start, "end": end, **resp.json()}
