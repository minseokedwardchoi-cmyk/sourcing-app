#!/usr/bin/env python3
"""
scripts/bulk_upload.py
대용량 Excel/CSV 파일을 백엔드에 안전하게 업로드하는 스크립트.

/api/upload(파일 전체를 한 요청으로 처리)는 수십만 행 이상에서는 처리 시간이
길어져 플랫폼 요청 타임아웃에 걸릴 수 있음. 이 스크립트는 파일을 청크로 나눠
/api/upload-json에 순차 전송하고, 구체화 뷰(materialized view) 리프레시는
마지막 청크에서 딱 한 번만 트리거해서 대용량 파일도 안전하게 적재한다.

필요 패키지:
  pip install pandas openpyxl

사용법:
  python3 scripts/bulk_upload.py 파일경로.xlsx
  python3 scripts/bulk_upload.py 파일경로.xlsx --chunk-size 20000 --url https://sourcing-backend-ucp5.onrender.com
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

import pandas as pd

DEFAULT_URL = "https://sourcing-backend-ucp5.onrender.com"
DEFAULT_CHUNK = 20000
MAX_RETRIES = 3


def load_rows(path: str) -> list[dict]:
    if path.lower().endswith(".csv"):
        df = pd.read_csv(path)
    else:
        df = pd.read_excel(path)
    df = df.where(pd.notnull(df), None)
    return df.to_dict(orient="records")


def post_chunk(url: str, chunk: list[dict], refresh: bool, timeout: int) -> dict:
    body = json.dumps({"rows": chunk, "refresh": refresh}, default=str).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError) as e:
            last_err = e
            wait = 2 ** attempt
            print(f"      청크 전송 실패 ({attempt}/{MAX_RETRIES}), {wait}초 후 재시도: {e}")
            time.sleep(wait)
    raise RuntimeError(f"청크 전송 {MAX_RETRIES}회 재시도 후 실패: {last_err}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", help="업로드할 Excel(.xlsx/.xls) 또는 CSV 파일 경로")
    parser.add_argument("--url", default=DEFAULT_URL, help="백엔드 주소")
    parser.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK, help="청크당 행 수")
    args = parser.parse_args()

    print(f"[1/3] 파일 읽는 중: {args.file}")
    rows = load_rows(args.file)
    total = len(rows)
    print(f"      총 {total:,}행 로드 완료")
    if total == 0:
        print("업로드할 행이 없습니다.")
        sys.exit(0)

    endpoint = f"{args.url.rstrip('/')}/api/upload-json"
    chunks = [rows[i:i + args.chunk_size] for i in range(0, total, args.chunk_size)]
    print(f"[2/3] {len(chunks)}개 청크로 나눠서 업로드 시작 (청크당 최대 {args.chunk_size:,}행) → {endpoint}")

    total_inserted = 0
    total_skipped = 0
    start = time.time()

    for i, chunk in enumerate(chunks, 1):
        is_last = (i == len(chunks))
        data = post_chunk(endpoint, chunk, refresh=is_last, timeout=120)
        total_inserted += data.get("inserted", 0)
        total_skipped += data.get("skipped", 0)
        elapsed = time.time() - start
        print(f"      청크 {i}/{len(chunks)} 완료 — 누적 {total_inserted:,}건 적재, "
              f"{total_skipped:,}건 스킵 ({elapsed:.0f}초 경과)")

    print(f"[3/3] 업로드 완료: 총 {total_inserted:,}건 적재, {total_skipped:,}건 스킵")
    print("      마지막 청크에서 뷰 리프레시를 트리거했습니다. "
          "데이터 규모에 따라 대시보드에 반영되기까지 잠시 걸릴 수 있습니다.")


if __name__ == "__main__":
    main()
