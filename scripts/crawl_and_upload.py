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
    log.info("페이지 로드: %s", TARGET_URL)
    await page.goto(TARGET_URL, wait_until="networkidle", timeout=60_000)

    # 처리일자 시작 — 여러 selector 시도
    for sel in ["#strtDt", "input[name='strtDt']", "input[placeholder='시작일']"]:
        try:
            await page.fill(sel, start, timeout=3_000)
            break
        except Exception:
            pass

    # 처리일자 종료
    for sel in ["#endDt", "input[name='endDt']", "input[placeholder='종료일']"]:
        try:
            await page.fill(sel, end, timeout=3_000)
            break
        except Exception:
            pass

    # 날짜 입력이 안 됐을 경우 JS로 강제 세팅
    await page.evaluate(f"""
        (() => {{
            const inputs = document.querySelectorAll('input[type="text"]');
            // 처리일자 관련 input을 순서대로 찾아서 설정
            let dateInputs = Array.from(inputs).filter(el =>
                el.value && el.value.match(/\\d{{4}}-\\d{{2}}-\\d{{2}}/)
            );
            if (dateInputs.length >= 2) {{
                dateInputs[0].value = '{start}';
                dateInputs[1].value = '{end}';
                dateInputs.forEach(el => el.dispatchEvent(new Event('change', {{bubbles: true}})));
            }}
        }})()
    """)

    # OEM 체크박스
    if oem:
        oem_checked = False
        for sel in [
            "input[type='checkbox'][id*='oem' i]",
            "input[type='checkbox'][name*='oem' i]",
            "input[type='checkbox']",
        ]:
            try:
                boxes = await page.query_selector_all(sel)
                for box in boxes:
                    label = await box.evaluate("el => el.labels?.[0]?.textContent || el.closest('label')?.textContent || ''")
                    if "OEM" in label or "주문자" in label:
                        if not await box.is_checked():
                            await box.check()
                        oem_checked = True
                        break
                if oem_checked:
                    break
            except Exception:
                pass
        if not oem_checked:
            log.warning("OEM 체크박스를 찾지 못했습니다 — 수동 확인 필요")

    # 검색 버튼 클릭
    await page.click("button:has-text('검색')", timeout=10_000)
    await page.wait_for_load_state("networkidle", timeout=30_000)


# ── Step 1: 전체 엑셀 다운로드 ───────────────────────────────────────────────
async def download_full_excel(page, start: str, end: str) -> pd.DataFrame:
    """전체 수입이력 엑셀 다운로드 후 DataFrame 반환"""
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    await open_search_page(page, start, end, oem=False)

    log.info("전체 엑셀 다운로드 시작")
    async with page.expect_download(timeout=120_000) as dl_info:
        # 엑셀다운로드 버튼 — 텍스트 또는 이미지 버튼
        await page.click("button:has-text('엑셀'), a:has-text('엑셀'), "
                         "button:has-text('Excel'), a:has-text('Excel'), "
                         "img[alt*='엑셀'], input[value*='엑셀']",
                         timeout=15_000)
    download = await dl_info.value
    dest = DOWNLOAD_DIR / f"full_{start}_{end}.xlsx"
    await download.save_as(str(dest))
    log.info("다운로드 완료: %s", dest)

    df = pd.read_excel(dest, engine="openpyxl")
    log.info("전체 다운로드 행 수: %d", len(df))
    return df


# ── Step 2: OEM 크롤링 ────────────────────────────────────────────────────────
async def crawl_oem_data(page, start: str, end: str) -> pd.DataFrame:
    """OEM 필터 후 페이지네이션 전체 크롤링"""
    await open_search_page(page, start, end, oem=True)

    # 페이지당 건수를 100으로 늘리기 (드롭다운)
    try:
        await page.select_option("select", "100", timeout=5_000)
        await page.wait_for_load_state("networkidle", timeout=20_000)
    except Exception:
        try:
            # 일부 사이트는 드롭다운이 여러 개 있음 — 마지막 것이 보통 페이지수
            dropdowns = await page.query_selector_all("select")
            if dropdowns:
                await dropdowns[-1].select_option("100")
                await page.wait_for_load_state("networkidle", timeout=20_000)
        except Exception:
            log.info("페이지당 건수 변경 실패 — 기본값(10) 사용")

    rows_all = []
    page_num = 1

    while True:
        log.info("OEM 크롤링 중: %d 페이지", page_num)

        # 테이블 헤더 읽기
        headers = []
        try:
            th_els = await page.query_selector_all("table thead th, table tr:first-child th")
            headers = [await th.inner_text() for th in th_els]
            headers = [h.strip() for h in headers if h.strip()]
        except Exception:
            pass

        # 테이블 데이터 행 읽기
        try:
            rows = await page.query_selector_all("table tbody tr")
            for row in rows:
                cells = await row.query_selector_all("td")
                values = [await c.inner_text() for c in cells]
                values = [v.strip() for v in values]
                if values and any(values):
                    rows_all.append(values)
        except Exception as e:
            log.warning("테이블 읽기 실패 (p%d): %s", page_num, e)

        # 다음 페이지 버튼
        next_btn = None
        for sel in [
            "a.next", "button.next",
            "a[title='다음']", "button[title='다음']",
            "a:has-text('다음')", "button:has-text('다음')",
            ".pagination .next",
        ]:
            try:
                btn = page.locator(sel).first
                if await btn.count() > 0 and await btn.is_visible():
                    next_btn = btn
                    break
            except Exception:
                pass

        if next_btn is None:
            # 페이지 번호 버튼으로 시도
            try:
                next_page_btn = page.locator(f"a:has-text('{page_num + 1}'), button:has-text('{page_num + 1}')").first
                if await next_page_btn.count() > 0:
                    next_btn = next_page_btn
            except Exception:
                pass

        if next_btn is None:
            log.info("마지막 페이지 도달 (총 %d 페이지)", page_num)
            break

        await next_btn.click()
        await page.wait_for_load_state("networkidle", timeout=30_000)
        await asyncio.sleep(PAGE_DELAY)
        page_num += 1

    if not rows_all:
        log.warning("OEM 크롤링 결과 없음")
        return pd.DataFrame()

    # 컬럼명 결정
    expected_cols = ["구분", "수입업체", "제품명(한글)", "제품명(영문)",
                     "품목(유형)", "제조/작업/수출업소", "처리일자", "소비기한", "제조국", "수출국"]

    if headers and len(headers) >= len(expected_cols):
        col_names = headers[:len(expected_cols)]
    else:
        col_names = expected_cols

    # 행 길이 맞추기
    normalized = []
    for r in rows_all:
        if len(r) < len(col_names):
            r = r + [""] * (len(col_names) - len(r))
        normalized.append(r[:len(col_names)])

    df = pd.DataFrame(normalized, columns=col_names)
    df["OEM여부"] = "Y"
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


def mark_and_transform(full_df: pd.DataFrame, oem_set: set) -> pd.DataFrame:
    """
    전체 다운로드 df에 OEM 마킹 후 업로드 양식으로 변환
    업로드 컬럼: 구분, MC, 제품명(한글), 수입업체, OEM여부, 해외제조업소, 제조국, 이메일, 처리일자
    """
    records = []
    for _, row in full_df.iterrows():
        key = tuple(normalize_str(row.get(c, "")) for c in MATCH_KEYS_FULL)
        is_oem = key in oem_set

        records.append({
            "구분":       normalize_str(row.get("구분")),
            "MC":        None,                              # 정부 데이터에 없는 필드
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
    log.info("OEM 마킹: %d / %d 건", oem_count, len(result_df))
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
        context = await browser.new_context(accept_downloads=True)

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

    result_df = mark_and_transform(full_df, oem_set)

    # Step 4: 백엔드 업로드
    await upload_to_backend(result_df, start, end)

    log.info("=== 완료 ===")


if __name__ == "__main__":
    asyncio.run(main())
