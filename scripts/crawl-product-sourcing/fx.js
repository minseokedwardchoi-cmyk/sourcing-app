/** JPY/MYR → USD 환율을 무료/무키 API로 조회한다. 실패하면 대략치(폴백)를 반환한다. */
export async function getCurrencyToUsdRates() {
  const fallback = { JPY: 1 / 150, MYR: 1 / 4.4 };
  try {
    const res = await fetch("https://open.er-api.com/v6/latest/USD", {
      signal: AbortSignal.timeout(8000),
    });
    if (res.ok) {
      const data = await res.json();
      const jpyPerUsd = data?.rates?.JPY;
      const myrPerUsd = data?.rates?.MYR;
      if (jpyPerUsd > 0 && myrPerUsd > 0) {
        return { JPY: 1 / jpyPerUsd, MYR: 1 / myrPerUsd };
      }
    }
  } catch {
    // 폴백 사용
  }
  console.warn("[fx] 실시간 환율 조회 실패 — 폴백 환율(약 150엔/달러, 4.4링깃/달러) 사용");
  return fallback;
}
