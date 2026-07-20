#!/usr/bin/env python3
"""
scripts/export_manufacturers_for_crawl.py
"산지맵"(국가별 제조사 랭킹, /api/countries/{country}/manufacturers)에서
실제 제조사 목록을 뽑아, 각 제조사의 홈페이지 URL(/api/manufacturer)까지
채워서 crawl_homepage_distributors.py가 바로 읽을 수 있는 CSV로 저장한다.

배포된 백엔드에 직접 API 호출을 하므로 인터넷 접속이 되는 곳(본인 PC)에서
실행해야 한다.

필요 패키지:
  pip install requests

사용법:
  python3 scripts/export_manufacturers_for_crawl.py --country 중국 --limit 100 --out china100.csv
  python3 scripts/export_manufacturers_for_crawl.py --country 중국 --country 미국 --country 일본 --limit 100 --out mixed100.csv

그 다음 이 CSV를 바로 크롤링 스크립트에 넘기면 된다:
  python3 scripts/crawl_homepage_distributors.py china100.csv --out china100_result.csv
"""

import argparse
import csv
import sys
import time

import requests

DEFAULT_URL = "https://sourcing-backend-ucp5.onrender.com"
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
}


def fetch_manufacturer_list(base_url: str, country: str, limit: int) -> list[dict]:
    resp = requests.get(
        f"{base_url}/api/countries/{country}/manufacturers",
        params={"page_size": limit, "page": 1, "sort_by": "total_import_count", "sort_order": "desc"},
        headers=_HEADERS, timeout=30,
    )
    resp.raise_for_status()
    return resp.json().get("data", [])


def fetch_homepage(base_url: str, manufacturer: str, factory: str) -> str:
    try:
        resp = requests.get(
            f"{base_url}/api/manufacturer",
            params={"manufacturer": manufacturer, "factory": factory or manufacturer},
            headers=_HEADERS, timeout=30,
        )
        if resp.status_code != 200:
            return ""
        return (resp.json().get("detail") or {}).get("homepage") or ""
    except requests.RequestException:
        return ""


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--country", action="append", required=True, help="국가명 (여러 번 지정 가능, 예: --country 중국 --country 미국)")
    parser.add_argument("--limit", type=int, default=100, help="전체 목표 건수 (국가 수에 맞춰 균등 배분)")
    parser.add_argument("--url", default=DEFAULT_URL, help="백엔드 주소")
    parser.add_argument("--out", default="manufacturers_for_crawl.csv", help="결과 저장 경로")
    parser.add_argument("--sleep", type=float, default=0.3, help="제조사 상세 조회 간 대기 시간(초)")
    args = parser.parse_args()

    base_url = args.url.rstrip("/")
    per_country = max(1, args.limit // len(args.country))

    all_rows = []
    for country in args.country:
        print(f"[1/2] {country} 제조사 목록 조회 중 (상위 {per_country}건)")
        try:
            rows = fetch_manufacturer_list(base_url, country, per_country)
        except requests.RequestException as e:
            print(f"      실패: {e}")
            continue
        print(f"      {len(rows)}건 조회됨")
        for r in rows:
            all_rows.append({"manufacturer": r["manufacturer"], "factory": r.get("factory") or r["manufacturer"],
                              "country": country})

    print(f"[2/2] 제조사 {len(all_rows)}건의 홈페이지 URL 조회 중...")
    with open(args.out, "w", newline="", encoding="utf-8-sig") as out_f:
        writer = csv.DictWriter(out_f, fieldnames=["제조사명", "국가", "홈페이지"])
        writer.writeheader()
        for i, row in enumerate(all_rows, 1):
            homepage = fetch_homepage(base_url, row["manufacturer"], row["factory"])
            writer.writerow({"제조사명": row["manufacturer"], "국가": row["country"], "홈페이지": homepage})
            out_f.flush()
            print(f"      [{i}/{len(all_rows)}] {row['manufacturer']} — {'홈페이지 있음' if homepage else '홈페이지 없음'}")
            if i < len(all_rows):
                time.sleep(args.sleep)

    print(f"완료 — {args.out}에 저장되었습니다. 이제 이걸 crawl_homepage_distributors.py에 넘기면 됩니다:")
    print(f"  python3 scripts/crawl_homepage_distributors.py {args.out} --out result.csv")


if __name__ == "__main__":
    main()
