"""
crawler.py — 수입식품정보마루 httpx 직접 호출 + OEM 마킹 + MC 변환
Playwright 없이 httpx만으로 동작 (메모리 ~50MB)
"""
from __future__ import annotations

import asyncio
import io
import logging
import re
from pathlib import Path

import httpx
import pandas as pd
from bs4 import BeautifulSoup
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)

BASE_URL = "https://impfood.mfds.go.kr/CFCCC01F01"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": f"{BASE_URL}/getList",
    "Origin": "https://impfood.mfds.go.kr",
}

_MC_MAP_PATH = Path(__file__).parent / "mc_mapping.csv"


def load_mc_mapping() -> dict[str, str]:
    if not _MC_MAP_PATH.exists():
        log.warning("mc_mapping.csv 없음")
        return {}
    df = pd.read_csv(_MC_MAP_PATH, encoding="utf-8-sig")
    mapping = dict(zip(df["품목(유형)"].str.strip(), df["MC"].str.strip()))
    log.info("MC 매핑 로드: %d개", len(mapping))
    return mapping


def _base_params(start: str, end: str) -> dict:
    return {
        "dclPrductSeCd": "", "prductNm": "", "srchNtncd": "", "srchHistNo": "",
        "rpsntItmNm": "", "rpsntItmCd": "", "bsshNm": "", "ovsmnfstNm": "",
        "srchStrtDt": start, "srchEndDt": end,
        "expirdeBeginDtm": "", "expirdeEndDtm": "",
        "returnChk": "", "sameSearch": "",
    }


async def get_total_count(client: httpx.AsyncClient, start: str, end: str, oem: bool = False) -> int:
    """getList POST로 totalCnt 파악"""
    data = {"page": "1", "limit": "1", "totalCnt": "", **_base_params(start, end)}
    if oem:
        data["oemFoodYn"] = "Y"
    resp = await client.post(f"{BASE_URL}/getList", data=data, timeout=30)
    resp.raise_for_status()

    # HTML에서 totalCnt 파싱
    m = re.search(r'name=["\']totalCnt["\'][^>]*value=["\'](\d+)["\']', resp.text)
    if not m:
        m = re.search(r'totalCnt["\s]*[:=]["\s]*["\']?(\d+)', resp.text)
    if m:
        return int(m.group(1))

    # hidden input에서 못 찾으면 JS 변수에서 시도
    m = re.search(r'var\s+totalCnt\s*=\s*["\']?(\d+)', resp.text)
    if m:
        return int(m.group(1))

    raise RuntimeError("totalCnt 파싱 실패 — 사이트 응답 구조가 바뀌었을 수 있습니다")


async def download_full_excel(client: httpx.AsyncClient, start: str, end: str,
                              total_cnt: int) -> pd.DataFrame:
    """전체 수입이력 Excel 다운로드"""
    params = {"totalCnt": str(total_cnt), "page": "1", "limit": "10", **_base_params(start, end)}
    log.info("전체 엑셀 다운로드 중 (totalCnt=%d)", total_cnt)
    resp = await client.get(f"{BASE_URL}/getExcelFile", params=params, timeout=120)
    resp.raise_for_status()
    df = pd.read_excel(io.BytesIO(resp.content), engine="openpyxl")
    log.info("전체 다운로드 완료: %d행", len(df))
    return df


async def crawl_oem_pages(client: httpx.AsyncClient, start: str, end: str,
                          total_cnt: int) -> pd.DataFrame:
    """getList HTML 파싱으로 OEM 행 수집 (페이지당 50건)"""
    LIMIT = 50
    total_pages = (total_cnt + LIMIT - 1) // LIMIT
    print(f"OEM 페이지 크롤링 시작: {total_cnt}건 → {total_pages}페이지", flush=True)

    COLS = ["구분", "수입업체", "제품명(한글)", "제품명(영문)",
            "품목(유형)", "해외제조업소", "처리일자", "소비기한", "제조국", "수출국"]
    rows_all = []

    for page_num in range(1, total_pages + 1):
        data = {
            "page": str(page_num), "limit": str(LIMIT),
            "totalCnt": str(total_cnt),
            "oemFoodYn": "Y",
            **_base_params(start, end),
        }
        resp = await client.post(f"{BASE_URL}/getList", data=data, timeout=30)
        resp.raise_for_status()
        print(f"OEM 페이지 {page_num} 응답: HTTP {resp.status_code}, {len(resp.text)}자", flush=True)

        soup = BeautifulSoup(resp.text, "html.parser")
        # <tbody> 가 없는 경우 대비해 table에서 직접 tr 검색
        tbody = soup.select_one("table tbody") or soup.select_one("table")
        if not tbody:
            print(f"OEM 페이지 {page_num}: 테이블 없음 — 응답 앞부분: {resp.text[:300]}", flush=True)
            break

        trs = tbody.find_all("tr")
        if not trs:
            print(f"OEM 페이지 {page_num}: tr 없음", flush=True)
            break

        page_rows = 0
        for tr in trs:
            tds = tr.find_all("td")
            values = [td.get_text(strip=True) for td in tds]
            if not any(values):
                continue
            if len(values) < len(COLS):
                values += [""] * (len(COLS) - len(values))
            rows_all.append(values[:len(COLS)])
            page_rows += 1

        print(f"OEM 페이지 {page_num}: {page_rows}행 수집 (누적 {len(rows_all)}건)", flush=True)

    if not rows_all:
        print("OEM 크롤링 결과 없음", flush=True)
        return pd.DataFrame()

    df = pd.DataFrame(rows_all, columns=COLS)
    print(f"OEM 크롤링 완료: {len(df)}건", flush=True)
    print("OEM 샘플:\n", df[["수입업체", "제품명(한글)", "처리일자", "제조국"]].head(3).to_string(), flush=True)
    return df


# ── OEM 마킹 + MC 변환 ────────────────────────────────────────────────────────

def normalize_str(s) -> str:
    if s is None or (isinstance(s, float) and pd.isna(s)):
        return ""
    return str(s).strip()


MATCH_KEYS = ["구분", "수입업체", "제품명(한글)", "제품명(영문)",
              "품목(유형)", "해외제조업소", "처리일자", "소비기한", "제조국", "수출국"]


def build_oem_set(oem_df: pd.DataFrame) -> set:
    result = set()
    for _, row in oem_df.iterrows():
        key = tuple(normalize_str(row.get(c, "")) for c in MATCH_KEYS)
        result.add(key)
    print(f"OEM 키 샘플: {list(result)[:3]}", flush=True)
    return result


def mark_and_transform(full_df: pd.DataFrame, oem_set: set, mc_map: dict) -> pd.DataFrame:
    unmapped = set()
    records = []
    for _, row in full_df.iterrows():
        key = tuple(normalize_str(row.get(c, "")) for c in MATCH_KEYS)
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
    print(f"OEM 마킹: {oem_count} / {len(result_df)}건", flush=True)
    print(f"MC 변환: {result_df['MC'].notna().sum()} / {len(result_df)}건", flush=True)
    if unmapped:
        print(f"MC 매핑 없는 품목 {len(unmapped)}종: {', '.join(sorted(unmapped)[:20])}", flush=True)
    return result_df


# ── 메인 파이프라인 ───────────────────────────────────────────────────────────

async def run_crawl(start: str, end: str, db: AsyncSession) -> dict:
    print(f"=== 크롤링 시작: {start} ~ {end} ===", flush=True)

    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True) as client:
        # 세션 쿠키 초기화
        await client.get(BASE_URL, timeout=30)

        # 전체/OEM 건수 파악
        full_count = await get_total_count(client, start, end, oem=False)
        print(f"전체 건수: {full_count}", flush=True)
        oem_count = await get_total_count(client, start, end, oem=True)
        print(f"OEM 건수: {oem_count}", flush=True)

        # 전체 Excel 다운로드 + OEM 페이지 크롤링
        full_df = await download_full_excel(client, start, end, full_count)
        print(f"전체 Excel 다운로드 완료: {len(full_df)}행", flush=True)
        oem_df  = await crawl_oem_pages(client, start, end, oem_count) if oem_count > 0 else pd.DataFrame()
        print(f"OEM 크롤링 완료: {len(oem_df)}행", flush=True)

    # CPU bound 작업 → 스레드풀로 분리해 이벤트 루프 블락 방지
    def _transform():
        oem_set = build_oem_set(oem_df) if not oem_df.empty else set()
        if full_df is not None and not full_df.empty:
            sample_keys = [tuple(normalize_str(full_df.iloc[i].get(c, "")) for c in MATCH_KEYS) for i in range(min(3, len(full_df)))]
            print(f"FULL_DF 키 샘플: {sample_keys}", flush=True)
        mc_map = load_mc_mapping()
        return mark_and_transform(full_df, oem_set, mc_map)

    result_df = await asyncio.to_thread(_transform)

    # Excel 직렬화 → 스레드풀
    def _to_excel_bytes():
        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as writer:
            result_df.to_excel(writer, index=False, sheet_name="시트1")
        buf.seek(0)
        return buf.read()

    file_bytes = await asyncio.to_thread(_to_excel_bytes)

    # DB에 직접 적재 (HTTP 왕복 없음)
    from importer import import_excel
    result = await import_excel(file_bytes, db)

    log.info("=== 완료: %d건 업로드 ===", len(result_df))
    return {"rows": len(result_df), "start": start, "end": end, **result}
