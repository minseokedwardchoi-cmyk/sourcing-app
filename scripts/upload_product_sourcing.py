#!/usr/bin/env python3
"""
scripts/upload_product_sourcing.py
품목별 유통사 인기상품/소싱 리스크 리서치 Excel(유형별카드 + 상품리스트(raw) 시트)을
백엔드의 /api/upload-product-sourcing 엔드포인트로 업로드한다.

이 엔드포인트는 업로드 시마다 product_sourcing_item 테이블을 통째로 비우고 새로
채우므로, 리서치 파일이 갱신될 때마다 이 스크립트만 다시 실행하면 된다.

사용법:
  python3 scripts/upload_product_sourcing.py 파일경로.xlsx
  python3 scripts/upload_product_sourcing.py 파일경로.xlsx --url https://sourcing-backend-ucp5.onrender.com
"""
import argparse
import sys
import urllib.error
import urllib.request

DEFAULT_URL = "https://sourcing-backend-ucp5.onrender.com"
DEFAULT_TIMEOUT = 300


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("file", help="업로드할 .xlsx 파일 경로")
    parser.add_argument("--url", default=DEFAULT_URL, help="백엔드 base URL")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    args = parser.parse_args()

    with open(args.file, "rb") as f:
        file_bytes = f.read()

    boundary = "----ProductSourcingUpload"
    filename = args.file.split("/")[-1]
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet\r\n\r\n"
    ).encode() + file_bytes + f"\r\n--{boundary}--\r\n".encode()

    req = urllib.request.Request(
        f"{args.url}/api/upload-product-sourcing",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )

    print(f"업로드 중... ({len(file_bytes) / 1024 / 1024:.1f} MB → {args.url})")
    try:
        with urllib.request.urlopen(req, timeout=args.timeout) as resp:
            print(resp.read().decode())
    except urllib.error.HTTPError as e:
        print(f"실패 ({e.code}): {e.read().decode()}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
