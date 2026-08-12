# crawl-walmart-samsclub (Part B — 아직 B0 스파이크 단계)

월마트/샘스클럽은 PerimeterX "Press & Hold" 봇차단이 있어서 아마존/이온몰처럼 완전 무인
자동화가 불가능하다. 대신 **GitHub Actions 러너 안에서 headed 브라우저를 띄우고, 그 화면을
VNC로 공유해서 사람이 캡차만 직접 눌러주는** 반자동 방식을 시도한다.

## 지금 상태: B0 (실현가능성 스파이크)만 있음

`.github/workflows/_spike_vnc_walmart.yml`을 GitHub Actions 탭에서 수동 실행(workflow_dispatch)하면:

1. Xvfb(가상 화면) + headed Chromium + x11vnc + noVNC + cloudflared quick tunnel을 띄운다.
2. 워크플로 실행 로그의 **Job Summary**에 접속 링크(비밀번호 포함)가 뜬다.
3. 그 링크로 접속하면 GitHub Actions 러너 안에서 실행 중인 브라우저 화면이 그대로 보인다 —
   월마트 검색 결과 페이지가 열려 있을 것이다.
4. PerimeterX 캡차("Press & Hold")가 뜨는지, 뜬다면 화면을 직접 클릭해서 통과되는지 확인한다.

**이게 안 되면(캡차가 계속 안 뚫리면) Part B는 여기서 중단** — GitHub Actions 러너의 IP 자체가
데이터센터 IP로 차단당하는 것이므로, 사람이 눌러도 소용없다는 뜻이다. 이 경우 월마트/샘스클럽은
계속 로컬 PC(`D:\AI 프로젝트\유통사크롤러\` 또는 `oliveoil-scraper`)에서 수동으로 진행한다.

**이게 되면(캡차 통과 확인)** → Part B1로 진행: 백엔드에 trigger/session-callback/session-status
엔드포인트를 추가하고, 프론트에 "월마트/샘스클럽 크롤링 시작" 버튼을 붙이고, 이 폴더에
`oliveoil-scraper/scrapers/walmart.js` + `samsclub.js` + `human-check.js`를 이식해서 83개
유형을 순회하는 실제 크롤링 루프(`crawl.js`)를 완성한다. 결과는 Part A와 같은
`product_sourcing_crawl_snapshot_item` 이력 테이블에 `retailer=walmart/samsclub`로 쌓는다.

## 파일
```
vnc-spike.js   # B0 전용 — 실제 크롤링 없음. 브라우저를 띄우고 대기만 함(사람이 VNC로 확인하는 동안)
package.json   # playwright 의존성 (이 폴더만 — crawl-product-sourcing은 playwright 불필요)
```
