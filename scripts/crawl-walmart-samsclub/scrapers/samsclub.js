import { waitForHumanIfChallenged } from "./human-check.js";

const clean = (s) => (s || "").replace(/\s+/g, " ").trim();

const isChallenged = (text) =>
  /Robot or human|Verify you are a human|Press (&|and) Hold|are you a robot|Access Denied|no robots allowed/i.test(
    text || ""
  );

/**
 * 샘스클럽 검색 결과에서 상위 limit개 제품정보를 긁어온다.
 * 정렬 UI를 클릭해서 "Best Selling"을 시도하고, 안 되면 기본(관련도) 정렬로 진행한다.
 * interactive: true(=headed 모드)면 봇 확인 화면에서 페이지를 새로고침하지 않고 사람이
 *   직접 버튼을 누를 때까지 같은 화면에서 기다린다.
 * 실제 마크업을 본 적이 없는 추측 기반 구현이라, 실행 결과를 보면서 selector를 맞춰야 한다.
 */
export async function scrapeSamsClub(page, query, limit = 5, interactive = false) {
  const url = `https://www.samsclub.com/s/${encodeURIComponent(query)}`;
  await page.goto(url, { waitUntil: "domcontentloaded", timeout: 45000 });
  await page.waitForTimeout(2000);

  const passed = await waitForHumanIfChallenged(
    page,
    async () => isChallenged(await page.textContent("body").catch(() => "")),
    "samsclub",
    { interactive }
  );
  if (!passed) {
    throw new Error(
      "샘스클럽 로봇 확인 페이지가 떴습니다. --headed 옵션으로 직접 풀거나, 잠시 후 다시 시도하세요."
    );
  }

  // 정렬을 Best Selling으로 바꿔본다 (UI 클릭 방식 - URL 파라미터를 몰라도 동작)
  let sortApplied = false;
  try {
    const sortTrigger = page.getByRole("button", { name: /sort/i }).first();
    if (await sortTrigger.count()) {
      await sortTrigger.click({ timeout: 5000 });
      const option = page.getByText(/best.?sell(ing|ers)?|top.?sell(ing|ers)?/i).first();
      if (await option.count()) {
        await option.click({ timeout: 5000 });
        await page.waitForTimeout(2500);
        sortApplied = true;
      }
    }
  } catch {
    // 정렬 UI를 못 찾으면 기본(관련도) 정렬로 그냥 진행
  }
  if (!sortApplied) {
    console.warn(
      "[samsclub] Best Selling 정렬 UI를 못 찾음 — 기본(관련도) 정렬로 진행. selector 보강 필요할 수 있음."
    );
  }

  await page.waitForTimeout(1000);

  // 상품 상세페이지 링크(/p/ 포함)를 기준으로 카드를 역추적하는 범용 방식.
  // 정확한 data-testid를 모르기 때문에 이렇게 접근한다.
  const rawItems = await page.evaluate((limit) => {
    const clean = (s) => (s || "").replace(/\s+/g, " ").trim();
    const anchors = Array.from(document.querySelectorAll('a[href*="/p/"]'));
    const seenHref = new Set();
    const out = [];

    for (const a of anchors) {
      const href = a.getAttribute("href");
      if (!href || seenHref.has(href)) continue;
      seenHref.add(href);

      // 카드 컨테이너 추정: li/article 또는 카드로 보이는 클래스, 없으면 4단계 위 부모
      let container =
        a.closest("li") ||
        a.closest("article") ||
        a.closest('[class*="tile" i]') ||
        a.closest('[class*="card" i]');
      if (!container) {
        container = a;
        for (let i = 0; i < 4 && container.parentElement; i++) {
          container = container.parentElement;
        }
      }

      const containerText = clean(container.innerText || "");
      if (!containerText) continue;

      const priceMatch = containerText.match(/\$\s?\d[\d,]*\.?\d{0,2}/);
      const price = priceMatch ? clean(priceMatch[0]) : null;

      // 제목: 링크의 aria-label/title, 없으면 이미지 alt, 없으면 앵커 텍스트 중 가장 긴 텍스트
      let title =
        clean(a.getAttribute("aria-label")) ||
        clean(a.getAttribute("title")) ||
        clean(container.querySelector("img")?.getAttribute("alt")) ||
        clean(a.textContent);
      if (!title || title.length < 5) continue;

      const ratingMatch = containerText.match(/(\d(?:\.\d)?)\s*(?:out of 5|stars)/i);
      const rating = ratingMatch ? clean(ratingMatch[0]) : null;

      const reviewMatch = containerText.match(/\(([\d,]+)\)/);
      const reviewCount = reviewMatch ? reviewMatch[1] : null;

      const sponsored = /sponsored/i.test(containerText.slice(0, 60));

      const sizeMatch = (title + " " + containerText).match(
        /(\d+(?:\.\d+)?\s?(?:fl\.?\s?oz|fluid ounces?|ml|milliliters?|\bL\b|liters?|litres?|qt|gal|ct|count))/i
      );
      const size = sizeMatch ? clean(sizeMatch[0]) : null;

      const stop = new Set([
        "Extra", "Virgin", "Olive", "Oil", "Oils", "Organic", "Cooking",
        "Smooth", "Robust", "Pure", "First", "Cold", "Member's", "Mark",
      ]);
      const words = title.split(/\s+/);
      const brandWords = [];
      for (const w of words) {
        const bare = w.replace(/[^A-Za-z%']/g, "");
        if (stop.has(bare)) break;
        brandWords.push(w);
        if (brandWords.length >= 3) break;
      }
      const brand = brandWords.join(" ") || words[0] || null;

      const imgEl = container.querySelector("img");
      const image = imgEl?.getAttribute("src") || imgEl?.getAttribute("data-src") || null;

      out.push({
        id: href,
        brand,
        title,
        size,
        url: href.startsWith("http") ? href : "https://www.samsclub.com" + href,
        price,
        rating,
        reviewCount,
        badge: null,
        image,
        sponsored,
        origin: null,
        acidity: null,
      });
      if (out.length >= limit * 3) break; // 광고 제외 위해 넉넉히 모음
    }
    return out;
  }, limit);

  const organic = rawItems.filter((it) => !it.sponsored).slice(0, limit);
  return organic.length ? organic : rawItems.slice(0, limit);
}

/**
 * 상품 상세페이지를 방문해서 갤러리 사진(정면/후면/측면 등) URL을 여러 장 뽑는다.
 * walmart.js의 scrapeWalmartProductImages와 같은 목적(원산지판독용 후면 라벨 사진 확보).
 *
 * ⚠️ 이 파일 전체가 이미 "실제 마크업을 본 적이 없는 추측 기반 구현"이라고 명시돼 있는데,
 * 이 함수도 마찬가지로 미검증 — 후보 selector를 여러 개 순서대로 시도하는 방어적 구현.
 */
export async function scrapeSamsClubProductImages(page, productUrl, maxImages = 4) {
  await page.goto(productUrl, { waitUntil: "domcontentloaded", timeout: 45000 });
  await page.waitForTimeout(1500);

  const passed = await waitForHumanIfChallenged(
    page,
    async () => isChallenged(await page.textContent("body").catch(() => "")),
    "samsclub-detail",
    { interactive: true, maxWaitMs: 60000 }
  );
  if (!passed) return [];

  await page.waitForTimeout(500);

  const images = await page.evaluate((max) => {
    const candidateSelectors = [
      '[class*="thumbnail" i] img',
      '[class*="carousel" i] img',
      '[class*="gallery" i] img',
      '[data-testid*="image" i] img',
      'button[class*="thumb" i] img',
    ];
    const seen = new Set();
    const out = [];
    for (const sel of candidateSelectors) {
      const nodes = document.querySelectorAll(sel);
      for (const img of nodes) {
        const src = img.getAttribute("src") || img.getAttribute("data-src");
        if (!src || seen.has(src)) continue;
        seen.add(src);
        out.push(src);
        if (out.length >= max) return out;
      }
      if (out.length >= max) break;
    }
    return out;
  }, maxImages);

  return images;
}
