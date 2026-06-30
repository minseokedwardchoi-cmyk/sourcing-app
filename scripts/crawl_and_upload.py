#!/usr/bin/env python3
"""
scripts/crawl_and_upload.py
수입식품정보마루 수입이력 수집 → OEM 마킹 → 백엔드 업로드 자동화

사용법:
  # 날짜 범위 지정 (초기 백필용)
  python3 scripts/crawl_and_upload.py --start 2026-06-01 --end 2026-06-29

  # 어제 하루치 (cron 기본 모드)
  python3 scripts/crawl_and_upload.py

환경변수:
  BACKEND_URL   백엔드 주소 (기본: http://localhost:8000)
  HEADLESS      0으로 설정하면 브라우저 화면 보임 (디버깅용)
"""

import argparse
import asyncio
import io
import logging
import os
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import httpx
import pandas as pd
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

# ── MC 매핑 로드 ──────────────────────────────────────────────────────────────
_MC_MAP_PATH = Path(__file__).parent / "mc_mapping.csv"

def load_mc_mapping() -> dict[str, str]:
    if not _MC_MAP_PATH.exists():
        log.warning("mc_mapping.csv 없음 — MC 컬럼이 모두 빈칸으로 처리됩니다")
        return {}
    df = pd.read_csv(_MC_MAP_PATH, encoding="utf-8-sig")
    mapping = dict(zip(df["품목(유형)"].str.strip(), df["MC"].str.strip()))
    log.info("MC 매핑 로드: %d개", len(mapping))
    return mapping

# ── 설정 ─────────────────────────────────────────────────────────────────────
TARGET_URL  = "https://impfood.mfds.go.kr/CFCCC01F01"
BACKEND_URL = os.environ.get("BACKEND_URL", "http://localhost:8000")
HEADLESS    = os.environ.get("HEADLESS", "1") != "0"
DOWNLOAD_DIR = Path("/tmp/impfood_downloads")
PAGE_DELAY  = 1.5   # 페이지 넘길 때 대기 (초) — 너무 빠르면 서버 부하

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)


# ── 날짜 유틸 ─────────────────────────────────────────────────────────────────
def yesterday() -> tuple[str, str]:
    d = date.today() - timedelta(days=1)
    s = d.strftime("%Y-%m-%d")
    return s, s


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default=None, help="시작일 (YYYY-MM-DD)")
    parser.add_argument("--end",   default=None, help="종료일 (YYYY-MM-DD)")
    args = parser.parse_args()

    if args.start and args.end:
        return args.start, args.end
    s, e = yesterday()
    return s, e


# ── 페이지 공통 초기화 ────────────────────────────────────────────────────────
async def open_search_page(page, start: str, end: str, oem: bool = False):
    """검색 조건 설정 후 검색 실행"""
    log.info("페이지 로드: %s (oem=%s)", TARGET_URL, oem)
    await page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=90_000)
    await asyncio.sleep(3)  # JS 초기화 대기

    # 날짜 입력 — JS로 직접 세팅 (jQuery datepicker가 있어서 value만 바꿔도 됨)
    await page.evaluate(f"""
        document.getElementById('srchStrtDt').value = '{start}';
        document.getElementById('srchEndDt').value = '{end}';
    """)

    # OEM 체크박스
    if oem:
        checked = await page.evaluate("document.getElementById('oemFoodYn').checked")
        if not checked:
            await page.click("#oemFoodYn")
        log.info("OEM 체크박스 선택 완료")

    # 검색 실행 — fnSearch(1) 직접 호출
    await page.evaluate("fnSearch(1)")
    await asyncio.sleep(5)  # 검색 결과 로딩 대기


# ── Step 1: 전체 엑셀 다운로드 ───────────────────────────────────────────────
async def download_full_excel(page, start: str, end: str) -> pd.DataFrame:
    """전체 수입이력 엑셀 다운로드 후 DataFrame 반환"""
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    await open_search_page(page, start, end, oem=False)

    log.info("전체 엑셀 다운로드 시작")
    async with page.expect_download(timeout=120_000) as dl_info:
        await page.click("button[onclick='fnGetExcelFile();']", timeout=15_000)
    download = await dl_info.value
    dest = DOWNLOAD_DIR / f"full_{start}_{end}.xlsx"
    await download.save_as(str(dest))
    log.info("다운로드 완료: %s", dest)

    df = pd.read_excel(dest, engine="openpyxl")
    log.info("전체 다운로드 행 수: %d", len(df))
    return df


# ── Step 2: OEM 크롤링 ────────────────────────────────────────────────────────
async def crawl_oem_data(page, start: str, end: str) -> pd.DataFrame:
    """OEM 필터 후 fnSearch(페이지번호) 로 전체 페이지 크롤링 (페이지당 50건)"""
    await open_search_page(page, start, end, oem=True)

    # 페이지당 50건으로 설정 — fnPageLimit(50) 호출
    await page.evaluate("fnPageLimit(50)")
    await asyncio.sleep(5)

    # 전체 건수 파싱 → 총 페이지 수 계산
    total_count = 0
    try:
        count_text = await page.inner_text("span.total, .total-count, #totalCount, .list-count")
        total_count = int("".join(filter(str.isdigit, count_text)))
    except Exception:
        try:
            # 페이지 텍스트에서 '총 N건' 패턴 찾기
            body = await page.inner_text("body")
            import re
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
        log.info("OEM 전체 %d건 → %d페이지 크롤링 시작", total_count, total_pages)

    COLS = ["구분", "수입업체", "제품명(한글)", "제품명(영문)",
            "품목(유형)", "제조/작업/수출업소", "처리일자", "소비기한", "제조국", "수출국"]

    rows_all = []

    for page_num in range(1, total_pages + 1):
        if page_num > 1:
            await page.evaluate(f"fnSearch({page_num})")
            await asyncio.sleep(PAGE_DELAY)

        log.info("OEM 크롤링 중: %d / %d 페이지", page_num, total_pages)

        tr_els = await page.query_selector_all("table tbody tr")
        if not tr_els:
            log.info("빈 페이지 — 크롤링 종료")
            break

        for tr in tr_els:
            tds = await tr.query_selector_all("td")
            values = [await td.inner_text() for td in tds]
            values = [v.strip() for v in values]
            if not any(values):
                continue
            # 컬럼 수 맞추기
            if len(values) < len(COLS):
                values += [""] * (len(COLS) - len(values))
            rows_all.append(values[:len(COLS)])

    if not rows_all:
        log.warning("OEM 크롤링 결과 없음")
        return pd.DataFrame()

    df = pd.DataFrame(rows_all, columns=COLS)
    log.info("OEM 크롤링 완료: %d 건", len(df))
    return df


# ── Step 3: OEM 마킹 + 업로드 포맷 변환 ──────────────────────────────────────
def normalize_str(s) -> str:
    if s is None or (isinstance(s, float) and pd.isna(s)):
        return ""
    return str(s).strip()


MATCH_KEYS_FULL = ["구분", "수입업체", "제품명(한글)", "제품명(영문)",
                   "품목(유형)", "해외제조업소", "처리일자", "소비기한", "제조국", "수출국"]
MATCH_KEYS_OEM  = ["구분", "수입업체", "제품명(한글)", "제품명(영문)",
                   "품목(유형)", "제조/작업/수출업소", "처리일자", "소비기한", "제조국", "수출국"]


def build_oem_set(oem_df: pd.DataFrame) -> set:
    """OEM 행의 비교 키 집합 생성"""
    result = set()
    for _, row in oem_df.iterrows():
        key = tuple(normalize_str(row.get(c, "")) for c in MATCH_KEYS_OEM)
        result.add(key)
    return result


def mark_and_transform(full_df: pd.DataFrame, oem_set: set, mc_map: dict) -> pd.DataFrame:
    """
    전체 다운로드 df에 OEM 마킹 + MC 변환 후 업로드 양식으로 변환
    업로드 컬럼: 구분, MC, 제품명(한글), 수입업체, OEM여부, 해외제조업소, 제조국, 이메일, 처리일자
    """
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
    oem_count = result_df["OEM여부"].notna().sum()
    mc_count  = result_df["MC"].notna().sum()
    log.info("OEM 마킹: %d / %d 건", oem_count, len(result_df))
    log.info("MC 변환: %d / %d 건", mc_count, len(result_df))
    if unmapped:
        log.warning("MC 매핑 없는 품목(유형) %d종: %s", len(unmapped), ", ".join(sorted(unmapped)[:20]))
    return result_df


# ── Step 4: 백엔드 업로드 ────────────────────────────────────────────────────
async def upload_to_backend(df: pd.DataFrame, start: str, end: str):
    """결과 DataFrame을 엑셀로 변환 후 /api/upload에 POST"""
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="시트1")
    buf.seek(0)

    filename = f"import_{start}_{end}.xlsx"
    log.info("백엔드 업로드 중: %s (%d건)", filename, len(df))

    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            f"{BACKEND_URL}/api/upload",
            files={"file": (filename, buf, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        )

    if resp.status_code == 200:
        result = resp.json()
        log.info("업로드 완료: %s", result)
    else:
        log.error("업로드 실패 (HTTP %d): %s", resp.status_code, resp.text)
        sys.exit(1)


# ── 메인 ─────────────────────────────────────────────────────────────────────
async def main():
    start, end = parse_args()
    log.info("=== 수입이력 자동화 시작: %s ~ %s ===", start, end)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=HEADLESS)
        context = await browser.new_context(
            accept_downloads=True,
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        )

        # Step 1: 전체 다운로드
        page = await context.new_page()
        full_df = await download_full_excel(page, start, end)

        # Step 2: OEM 크롤링 (새 탭)
        page2 = await context.new_page()
        oem_df = await crawl_oem_data(page2, start, end)

        await browser.close()

    # Step 3: 비교 + 마킹 + 포맷 변환
    if oem_df.empty:
        log.warning("OEM 데이터 없음 — OEM여부 전체 비워서 업로드")
        oem_set = set()
    else:
        oem_set = build_oem_set(oem_df)

    mc_map = load_mc_mapping()
    result_df = mark_and_transform(full_df, oem_set, mc_map)

    # Step 4: 백엔드 업로드
    await upload_to_backend(result_df, start, end)

    log.info("=== 완료 ===")


if __name__ == "__main__":
    asyncio.run(main())
