#!/usr/bin/env python3
"""
scripts/bulk_upload.py
대용량 Excel/CSV 파일을 백엔드에 안전하게 업로드하는 스크립트.

/api/upload(파일 전체를 한 요청으로 처리)는 수십만 행 이상에서는 처리 시간이
길어져 플랫폼 요청 타임아웃에 걸릴 수 있음. 이 스크립트는 파일을 청크로 나눠
/api/upload-json에 순차 전송하고, 구체화 뷰(materialized view) 리프레시는
마지막 청크에서 딱 한 번만 트리거해서 대용량 파일도 안전하게 적재한다.

타임아웃 발생 시 무조건 재전송하지 않고, /api/stats로 전체 행 수가 이미
늘어났는지 확인한 뒤 재전송 여부를 결정한다 (서버는 응답만 늦었을 뿐 실제로는
이미 반영됐을 수 있어서, 그냥 재전송하면 같은 행이 두 번 들어갈 수 있음).

스크립트가 도중에 완전히 실패하면 --start-chunk로 마지막 성공한 청크
다음부터 이어서 실행할 수 있다.

필요 패키지:
  pip install pandas openpyxl

사용법:
  python3 scripts/bulk_upload.py 파일경로.xlsx
  python3 scripts/bulk_upload.py 파일1.xlsx 파일2.xlsx 파일3.xlsx
  python3 scripts/bulk_upload.py 파일경로.xlsx --chunk-size 2000 --url https://sourcing-backend-ucp5.onrender.com
  python3 scripts/bulk_upload.py 파일1.xlsx 파일2.xlsx --start-chunk 42   # 42번째 청크부터 이어서 시작

여러 파일을 동시에 넘기면 순서대로 이어붙여서 업로드하고, 전체 업로드가 끝난
마지막 청크에서만 뷰 리프레시를 트리거한다 (파일마다 따로 실행하면 파일당
한 번씩 리프레시됨).
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

import pandas as pd

DEFAULT_URL = "https://sourcing-backend-ucp5.onrender.com"
DEFAULT_CHUNK = 2000
DEFAULT_TIMEOUT = 120
MAX_RETRIES = 3
VERIFY_WAIT = 5  # 타임아웃 후 서버에 실제 반영됐는지 확인하기 전 대기 시간(초)


# 백엔드 importer.py의 FIELD_MAP 키(헤더로 쓰이는 한글/영문 컬럼명들). 첫 행이
# 이 중 하나라도 포함하면 "헤더 있는 파일"로 판단한다. 없으면 헤더가 없는
# 파일로 보고 컬럼 수에 따라 위치 기준으로 이름을 붙인다 (헤더가 없는데도
# header=0으로 읽으면 첫 데이터 행이 컬럼명이 되어, 그 값이 날짜 등일 경우
# json 직렬화 시 "keys must be str..." 에러가 남).
_FIELD_MAP_KEYS = {
    "구분", "분류", "MC", "자사MC", "자사 MC", "카테고리",
    "제품명(한글)", "제품명", "상품명", "SKU명", "SKU", "품목명",
    "수입업체", "수입사", "수입/OEM업체", "거래한 유통사",
    "OEM여부", "OEM 여부", "OEM/수입", "수입/OEM",
    "해외제조업소", "해외 제조업소", "해외제조업체", "제조업소", "제조사", "제조업체",
    "제조국", "제조국가", "국가",
    "이메일", "연락처", "email", "Email",
    "수입처리일자", "처리일자", "처리 일자", "수입일자", "수입 일자",
}
_HEADERLESS_COLS = ["category", "mc", "sku_name", "importer", "import_type", "factory", "country"]


def load_excel(path: str) -> list[dict]:
    df_raw = pd.read_excel(path, engine="openpyxl", header=None)
    df_raw = df_raw.dropna(how="all").dropna(axis=1, how="all")
    if df_raw.empty:
        return []

    first_row_values = {
        str(v).strip() for v in df_raw.iloc[0].tolist() if pd.notna(v) and str(v).strip()
    }
    has_header = bool(first_row_values & _FIELD_MAP_KEYS)

    if has_header:
        headers = [str(v).strip() if pd.notna(v) else "" for v in df_raw.iloc[0].tolist()]
        df = df_raw.iloc[1:].copy()
        df.columns = headers
    else:
        df = df_raw.copy()
        n_cols = len(df.columns)
        if n_cols == 6:
            df.columns = ["category", "mc", "sku_name", "importer", "factory", "country"]
        elif n_cols == 7:
            df.columns = _HEADERLESS_COLS
        elif n_cols == 8:
            df.columns = _HEADERLESS_COLS + ["email"]
        else:
            base_cols = _HEADERLESS_COLS.copy()
            if n_cols <= len(base_cols):
                df.columns = base_cols[:n_cols]
            else:
                df.columns = base_cols + [f"extra_{i}" for i in range(n_cols - len(base_cols))]

    df = df.where(pd.notnull(df), None)
    return df.to_dict(orient="records")


def load_rows(path: str) -> list[dict]:
    if path.lower().endswith(".csv"):
        df = pd.read_csv(path)
        df = df.where(pd.notnull(df), None)
        return df.to_dict(orient="records")
    return load_excel(path)


def fetch_total_count(base_url: str, timeout: int = 30) -> int | None:
    """현재 import_history 전체 행 수. 조회 자체가 실패하면 None (판단 불가)."""
    try:
        req = urllib.request.Request(f"{base_url}/api/stats", method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("importHistoryCount")
    except Exception:
        return None


def post_chunk(endpoint: str, base_url: str, chunk: list[dict], refresh: bool, timeout: int,
               expected_before: int | None) -> dict:
    """expected_before: 이 청크를 보내기 직전, import_history에 있어야 할 것으로 예상되는 총 행 수
    (스크립트가 지금까지 성공시킨 것만 누적한 값 — /api/stats를 매번 부르지 않기 위함).
    타임아웃이 실제로 발생했을 때만 /api/stats를 호출해서 서버 반영 여부를 확인한다."""
    body = json.dumps({"rows": chunk, "refresh": refresh}, default=str).encode("utf-8")

    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        req = urllib.request.Request(
            endpoint, data=body, method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError) as e:
            last_err = e
            print(f"      청크 전송 실패 ({attempt}/{MAX_RETRIES}): {e}")

            # 타임아웃 = 응답을 못 받은 것일 뿐, 서버에는 이미 반영됐을 수 있음.
            # 무조건 재전송하면 같은 행이 중복 삽입될 위험이 있어서 먼저 확인한다.
            if expected_before is not None:
                time.sleep(VERIFY_WAIT)
                after_count = fetch_total_count(base_url)
                if after_count is not None:
                    landed = after_count - expected_before
                    if landed >= len(chunk) * 0.5:
                        print(f"      → 타임아웃났지만 서버에는 이미 반영된 것으로 보임 "
                              f"({landed:,}건 증가) — 재전송하지 않고 통과 처리")
                        return {"inserted": landed, "skipped": max(len(chunk) - landed, 0)}

            if attempt < MAX_RETRIES:
                wait = 10 * attempt
                print(f"      {wait}초 후 재시도")
                time.sleep(wait)

    raise RuntimeError(
        f"청크 전송 {MAX_RETRIES}회 재시도 후에도 실패했고, 서버 반영 여부도 확인되지 않음: {last_err}\n"
        f"  → /api/stats 로 현재 총 행 수를 직접 확인한 뒤, 이미 늘어났으면 이 청크는 건너뛰고\n"
        f"    --start-chunk 옵션으로 다음 청크부터 이어서 실행하세요."
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", nargs="+", help="업로드할 Excel(.xlsx/.xls) 또는 CSV 파일 경로 (여러 개 가능)")
    parser.add_argument("--url", default=DEFAULT_URL, help="백엔드 주소")
    parser.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK, help="청크당 행 수")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="청크당 요청 타임아웃(초)")
    parser.add_argument("--start-chunk", type=int, default=1,
                         help="이 번호의 청크부터 시작 (1부터 시작; 이전 실행이 도중에 실패했을 때 이어서 실행하는 용도)")
    args = parser.parse_args()

    print(f"[1/3] 파일 {len(args.files)}개 읽는 중")
    rows: list[dict] = []
    for path in args.files:
        file_rows = load_rows(path)
        print(f"      {path}: {len(file_rows):,}행")
        rows.extend(file_rows)
    total = len(rows)
    print(f"      총 {total:,}행 로드 완료")
    if total == 0:
        print("업로드할 행이 없습니다.")
        sys.exit(0)

    base_url = args.url.rstrip("/")
    endpoint = f"{base_url}/api/upload-json"
    chunks = [rows[i:i + args.chunk_size] for i in range(0, total, args.chunk_size)]
    print(f"[2/3] {len(chunks)}개 청크로 나눠서 업로드 시작 (청크당 최대 {args.chunk_size:,}행) → {endpoint}")
    if args.start_chunk > 1:
        print(f"      --start-chunk {args.start_chunk} 지정됨 — 앞의 {args.start_chunk - 1}개 청크는 건너뜁니다.")

    total_inserted = 0
    total_skipped = 0
    start = time.time()

    # 매 청크마다 /api/stats를 부르면 느려지니, 시작 시점에 한 번만 기준값을 받아오고
    # 이후로는 스크립트가 누적한 inserted 값으로 예상치를 직접 계산한다.
    expected_count = fetch_total_count(base_url)
    if expected_count is None:
        print("      경고: /api/stats 조회 실패 — 타임아웃 시 중복 삽입 방지 확인이 비활성화됩니다.")

    for i, chunk in enumerate(chunks, 1):
        if i < args.start_chunk:
            continue
        is_last = (i == len(chunks))
        data = post_chunk(endpoint, base_url, chunk, refresh=is_last, timeout=args.timeout,
                           expected_before=expected_count)
        inserted = data.get("inserted", 0)
        total_inserted += inserted
        total_skipped += data.get("skipped", 0)
        if expected_count is not None:
            expected_count += inserted
        elapsed = time.time() - start
        print(f"      청크 {i}/{len(chunks)} 완료 — 누적 {total_inserted:,}건 적재, "
              f"{total_skipped:,}건 스킵 ({elapsed:.0f}초 경과)")

    print(f"[3/3] 업로드 완료: 총 {total_inserted:,}건 적재, {total_skipped:,}건 스킵")
    print("      마지막 청크에서 뷰 리프레시를 트리거했습니다. "
          "데이터 규모에 따라 대시보드에 반영되기까지 잠시 걸릴 수 있습니다.")


if __name__ == "__main__":
    main()
