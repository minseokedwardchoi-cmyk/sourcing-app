#!/usr/bin/env python3
"""
scripts/upload_tariff_rates.py
관세청_품목번호별 관세율표(data.go.kr) Excel을 백엔드의 /api/upload-tariff-rates
엔드포인트로 업로드한다.

이 엔드포인트는 업로드 시마다 tariff_rate 테이블을 통째로 비우고 새로 채우므로,
관세율표가 갱신될 때마다(반기/연 단위) 이 스크립트만 다시 실행하면 된다.
품목별 소싱 테이블에 HS코드가 채워진 행들은 다음 조회부터 자동으로 추정원가가
갱신된다 (별도 재계산 작업 불필요).

사용법:
  python3 scripts/upload_tariff_rates.py 파일경로.xlsx
  python3 scripts/upload_tariff_rates.py 파일경로.xlsx --url https://sourcing-backend-ucp5.onrender.com
"""
import argparse
import sys
import urllib.error
import urllib.request

DEFAULT_URL = "https://sourcing-backend-ucp5.onrender.com"
DEFAULT_TIMEOUT = 600  # 관세율표는 HS코드 전체(수십만 행)라 파싱+적재에 1~2분 걸릴 수 있음


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("file", help="업로드할 .xlsx 파일 경로")
    parser.add_argument("--url", default=DEFAULT_URL, help="백엔드 base URL")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    args = parser.parse_args()

    with open(args.file, "rb") as f:
        file_bytes = f.read()

    boundary = "----TariffRateUpload"
    filename = args.file.split("/")[-1]
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet\r\n\r\n"
    ).encode() + file_bytes + f"\r\n--{boundary}--\r\n".encode()

    req = urllib.request.Request(
        f"{args.url}/api/upload-tariff-rates",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )

    print(f"업로드 중... ({len(file_bytes) / 1024 / 1024:.1f} MB → {args.url}, 시간이 좀 걸릴 수 있습니다)")
    try:
        with urllib.request.urlopen(req, timeout=args.timeout) as resp:
            print(resp.read().decode())
    except urllib.error.HTTPError as e:
        print(f"실패 ({e.code}): {e.read().decode()}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
