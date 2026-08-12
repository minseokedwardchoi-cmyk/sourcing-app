/**
 * 봇 확인 화면(Press & Hold 등)이 떴을 때, headed 모드라면 페이지를 다시 로드하지 않고
 * 같은 화면에서 사람이 직접 버튼을 누를 때까지 기다린다.
 * checkFn()은 "아직도 봇 확인 화면인가?"를 true/false로 반환해야 한다.
 */
export async function waitForHumanIfChallenged(page, checkFn, label, { interactive, maxWaitMs = 90000 } = {}) {
  let stillChallenged = await checkFn();
  if (!stillChallenged) return true;

  if (!interactive) {
    return false; // headless면 기다려봐야 소용없음 - 바로 실패 처리
  }

  console.log(
    `[${label}] 봇 확인 화면이 떴습니다. 브라우저 창에서 직접 버튼을 눌러 통과시켜주세요. (최대 ${Math.round(
      maxWaitMs / 1000
    )}초 대기)`
  );

  const start = Date.now();
  while (Date.now() - start < maxWaitMs) {
    await page.waitForTimeout(2000);
    stillChallenged = await checkFn();
    if (!stillChallenged) {
      console.log(`[${label}] 통과 확인됨 — 계속 진행합니다.`);
      return true;
    }
  }

  console.log(`[${label}] 시간 초과 — 여전히 봇 확인 화면입니다.`);
  return false;
}
