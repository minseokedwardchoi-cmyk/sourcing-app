"""
crawler.py — 수입식품정보마루 httpx 직접 호출 + OEM 마킹 + MC 변환
Playwright 없이 httpx만으로 동작 (메모리 ~50MB)
"""
from __future__ import annotations

import io
import logging
import re
from pathlib import Path

import httpx
import pandas as pd

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


async def download_excel(client: httpx.AsyncClient, start: str, end: str,
                         total_cnt: int, oem: bool = False) -> pd.DataFrame:
    """getExcelFile GET으로 Excel 다운로드 후 DataFrame 반환"""
    params = {"totalCnt": str(total_cnt), "page": "1", "limit": "10", **_base_params(start, end)}
    if oem:
        params["oemFoodYn"] = "Y"

    label = "OEM" if oem else "전체"
    log.info("%s 엑셀 다운로드 중 (totalCnt=%d)", label, total_cnt)
    resp = await client.get(f"{BASE_URL}/getExcelFile", params=params, timeout=120)
    resp.raise_for_status()

    df = pd.read_excel(io.BytesIO(resp.content), engine="openpyxl")
    log.info("%s 다운로드 완료: %d행", label, len(df))
    return df


# ── OEM 마킹 + MC 변환 ────────────────────────────────────────────────────────

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


# ── 메인 파이프라인 ───────────────────────────────────────────────────────────

async def run_crawl(start: str, end: str, backend_url: str) -> dict:
    log.info("=== 크롤링 시작: %s ~ %s ===", start, end)

    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True) as client:
        # 세션 쿠키 초기화
        await client.get(BASE_URL, timeout=30)

        # 전체 건수 파악
        full_count = await get_total_count(client, start, end, oem=False)
        log.info("전체 건수: %d", full_count)

        # OEM 건수 파악
        oem_count = await get_total_count(client, start, end, oem=True)
        log.info("OEM 건수: %d", oem_count)

        # Excel 다운로드
        full_df = await download_excel(client, start, end, full_count, oem=False)
        oem_df  = await download_excel(client, start, end, oem_count,  oem=True) if oem_count > 0 else pd.DataFrame()

    # OEM 마킹 + MC 변환
    oem_set = build_oem_set(oem_df) if not oem_df.empty else set()
    mc_map = load_mc_mapping()
    result_df = mark_and_transform(full_df, oem_set, mc_map)

    # 백엔드 업로드
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
