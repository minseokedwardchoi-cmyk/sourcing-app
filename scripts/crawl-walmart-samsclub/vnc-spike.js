import { chromium } from "playwright";

// Part B0 실현가능성 스파이크 전용 — 실제 크롤링 로직 없음. Xvfb 위에서 headed Chromium을
// 띄워 walmart.com 검색 페이지로 이동한 뒤, VNC로 접속한 사람이 PerimeterX "Press & Hold"
// 캡차를 실제로 통과시킬 수 있는지 확인하는 것이 유일한 목적. 통과되면 Part B1(본 구현)에서
// oliveoil-scraper의 scrapers/walmart.js + human-check.js를 그대로 이식해서 실제 크롤링
// 루프를 만든다.

const IDLE_MS = Number(process.env.SPIKE_IDLE_MINUTES || 10) * 60 * 1000; // 기본 10분
const VNC_CONNECT_URL = process.env.VNC_CONNECT_URL || "(설정 안 됨)";

// Job Summary는 잡(job) 전체가 끝나기 전까지 화면에 안 뜨는 것으로 실측 확인됨 — 세션이
// 이미 닫힌 뒤에나 보여서 이 용도로는 못 쓴다. 대신 실시간으로 스트리밍되는 일반 로그에
// 접속 링크를 반복 출력해서, 로그를 언제 열어보든(늦게 열어도) 놓치지 않게 한다.
function printConnectBanner() {
  console.log("==================================================================");
  console.log("  VNC 접속 링크 (전체 복사해서 브라우저에 붙여넣기)");
  console.log(VNC_CONNECT_URL);
  console.log("==================================================================");
}

async function main() {
  console.log("[spike] Chromium 실행 (headed, Xvfb 화면)...");
  const browser = await chromium.launch({ headless: false, args: ["--start-maximized"] });
  const context = await browser.newContext({ viewport: null });
  const page = await context.newPage();

  const query = "extra virgin olive oil";
  const url = `https://www.walmart.com/search?q=${encodeURIComponent(query)}&sort=best_seller`;
  console.log(`[spike] 이동: ${url}`);
  await page.goto(url, { waitUntil: "domcontentloaded", timeout: 45000 }).catch((e) => {
    console.warn(`[spike] goto 실패/타임아웃: ${e.message} — 페이지는 열려있으니 VNC로 직접 확인하세요.`);
  });

  console.log(
    `[spike] ${IDLE_MS / 1000}초 동안 브라우저를 열어둡니다. VNC로 접속해서 캡차가 뜨는지, 뜬다면 직접 눌러서 통과되는지 확인해주세요.`
  );
  printConnectBanner();

  const start = Date.now();
  let tick = 0;
  while (Date.now() - start < IDLE_MS) {
    await page.waitForTimeout(15000);
    tick += 1;
    const text = await page.textContent("body").catch(() => "");
    const challenged = /Robot or human|Verify you are a human|Press (&|and) Hold|are you a robot/i.test(text || "");
    const elapsedSec = Math.round((Date.now() - start) / 1000);
    console.log(`[spike] ${elapsedSec}s 경과 — 챌린지 화면: ${challenged ? "있음" : "없음"}`);
    // 20틱(약 5분)마다 링크를 다시 찍어서, 로그를 늦게 열어도 스크롤 몇 번이면 바로 보이게 한다.
    if (tick % 20 === 0) printConnectBanner();
  }

  console.log("[spike] 대기 시간 종료 — 브라우저를 닫습니다.");
  await browser.close();
}

main().catch((error) => {
  console.error("[spike] 오류:", error);
  process.exit(1);
});
