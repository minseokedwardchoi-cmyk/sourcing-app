import { waitForHumanIfChallenged } from "./human-check.js";

const clean = (s) => (s || "").replace(/\s+/g, " ").trim();

// 월마트 검색 정렬 옵션 → URL의 sort= 파라미터 매핑
const SORT_MAP = {
  featured: "best_match",
  "best-sellers": "best_seller",
  "price-asc": "price_low",
  "price-desc": "price_high",
  reviews: "rating_high",
  newest: "new",
};

const isChallenged = (text) =>
  /Robot or human|Verify you are a human|Press (&|and) Hold|are you a robot/i.test(text || "");

/**
 * 월마트 검색 결과에서 상위 limit개 "오가닉(광고 제외)" 제품정보를 긁어온다.
 * sort: "featured" | "best-sellers" | "price-asc" | "price-desc" | "reviews" | "newest"
 * interactive: true(=headed 모드)면 봇 확인 화면에서 페이지를 새로고침하지 않고 사람이
 *   직접 버튼을 누를 때까지 같은 화면에서 기다린다.
 * DOM 구조는 월마트가 수시로 바꾸므로, 실패 시 selector 조정이 필요할 수 있다.
 */
export async function scrapeWalmart(page, query, limit = 5, sort = "best-sellers", interactive = false) {
  const sortParam = SORT_MAP[sort] ?? SORT_MAP["best-sellers"];
  const url = `https://www.walmart.com/search?q=${encodeURIComponent(query)}&sort=${sortParam}`;
  await page.goto(url, { waitUntil: "domcontentloaded", timeout: 45000 });
  // 챌린지 스크립트가 렌더링될 시간을 조금 준다.
  await page.waitForTimeout(2000);

  const passed = await waitForHumanIfChallenged(
    page,
    async () => isChallenged(await page.textContent("body").catch(() => "")),
    "walmart",
    { interactive }
  );
  if (!passed) {
    throw new Error(
      "월마트 로봇 확인(캡차) 페이지가 떴습니다. --headed 옵션으로 직접 풀거나, 잠시 후 다시 시도하세요."
    );
  }

  await page.waitForSelector("[data-item-id]", { timeout: 20000 }).catch(() => {});

  const rawItems = await page.$$eval("[data-item-id]", (nodes) => {
    const clean = (s) => (s || "").replace(/\s+/g, " ").trim();

    const parseSize = (innerText, title) => {
      const combined = `${title} ${innerText}`;
      const m = combined.match(
        /(\d+(?:\.\d+)?\s?(?:fl\.?\s?oz|fluid ounces?|ml|milliliters?|\bL\b|liters?|litres?|qt|gal))/i
      );
      return m ? clean(m[0]) : null;
    };

    const parseBrand = (title) => {
      const stop = new Set([
        "Extra",
        "Virgin",
        "Olive",
        "Oil",
        "Oils",
        "Organic",
        "Cooking",
        "Smooth",
        "Robust",
        "Pure",
        "First",
        "Cold",
        "100%",
      ]);
      const words = title.split(/\s+/);
      const brandWords = [];
      for (const w of words) {
        const bare = w.replace(/[^A-Za-z%]/g, "");
        if (stop.has(bare)) break;
        brandWords.push(w);
        if (brandWords.length >= 3) break;
      }
      return brandWords.join(" ") || words[0] || null;
    };

    const out = [];
    for (const node of nodes) {
      const itemId = node.getAttribute("data-item-id");
      if (!itemId) continue;

      const titleEl = node.querySelector('span[data-automation-id="product-title"]');
      const title = clean(titleEl?.textContent);
      if (!title) continue;

      const linkEl = node.querySelector("a");
      const href = linkEl?.getAttribute("href");
      const productUrl = href
        ? href.startsWith("http")
          ? href.split("?")[0]
          : "https://www.walmart.com" + href.split("?")[0]
        : null;

      const priceEl = node.querySelector(
        '[data-automation-id="product-price"] span[aria-hidden="true"], [data-automation-id="product-price"]'
      );
      const price = clean(priceEl?.textContent);

      const ratingEl = node.querySelector(
        '[data-testid="product-ratings"], span[aria-label*="stars"], span[aria-label*="out of"]'
      );
      const rating = clean(ratingEl?.getAttribute("aria-label") || ratingEl?.textContent);

      const reviewEl = node.querySelector(
        '[data-testid="product-reviews-count"], [link-identifier="linkText"]'
      );
      const reviewCount = clean(reviewEl?.textContent);

      const badgeEl = node.querySelector('[data-testid="badge-text"]');
      const badge = clean(badgeEl?.textContent); // "Best seller", "Rollback" 등

      const imgEl = node.querySelector("img");
      const image = imgEl?.getAttribute("src") || null;

      const innerText = clean(node.innerText || "");
      const sponsored = /^Sponsored\b/i.test(innerText) || /\bSponsored\b/.test(
        clean(node.querySelector('[data-testid="sponsoredLabel"]')?.textContent || "")
      );

      const size = parseSize(innerText, title);
      const brand = parseBrand(title);

      out.push({
        itemId,
        brand,
        title,
        size,
        url: productUrl,
        price: price || null,
        rating: rating || null,
        reviewCount: reviewCount || null,
        badge: badge || null,
        image,
        sponsored,
        origin: null, // enrich 단계에서 채움
        acidity: null, // enrich 단계에서 채움
      });
    }
    return out;
  });

  // 광고 제외 + itemId 중복 제거 후 상위 limit개만 반환
  const seen = new Set();
  const organic = [];
  for (const item of rawItems) {
    if (item.sponsored) continue;
    if (seen.has(item.itemId)) continue;
    seen.add(item.itemId);
    organic.push(item);
    if (organic.length >= limit) break;
  }

  return organic;
}
