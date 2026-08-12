import fs from "node:fs";
import path from "node:path";
import { getCurrencyToUsdRates } from "./fx.js";

// results.jsonl(crawl.js 결과)을 읽어 백엔드 이력 테이블에 한 번의 배치로 업로드한다.
// product_sourcing_item(메인페이지 실제 데이터)은 절대 건드리지 않는다 — 새 테이블
// product_sourcing_crawl_run / product_sourcing_crawl_snapshot_item에만 append.

function parseArgs(argv) {
  const args = { results: "results.jsonl", note: "" };
  for (const raw of argv) {
    const m = raw.match(/^--([a-zA-Z]+)=(.*)$/);
    if (m) args[m[1]] = m[2];
  }
  return args;
}

function parseAmazonPrice(priceText) {
  const m = String(priceText || "").match(/[\d,]+(?:\.\d+)?/);
  return m ? Number(m[0].replace(/,/g, "")) : null;
}

function parseRating(ratingText) {
  const m = String(ratingText || "").match(/^([0-5](?:\.\d)?)/);
  return m ? Number(m[1]) : null;
}

function parseReviewCount(text) {
  const m = String(text || "").match(/^([\d,]+(?:\.\d+)?)\s*([KM])?$/i);
  if (!m) return null;
  const base = Number(m[1].replace(/,/g, ""));
  if (!Number.isFinite(base)) return null;
  const mult = m[2]?.toUpperCase() === "K" ? 1000 : m[2]?.toUpperCase() === "M" ? 1000000 : 1;
  return Math.round(base * mult);
}

function priceToUsd(item, retailer, rates) {
  if (retailer === "amazon") return parseAmazonPrice(item.price);
  if (item.source === "aeon-jp") {
    const yen = Number(String(item.price || "").replace(/[^\d.]/g, ""));
    return Number.isFinite(yen) ? Number((yen * rates.JPY).toFixed(2)) : null;
  }
  if (item.source === "aeon-my") {
    const myr = Number(String(item.price || "").replace(/[^\d.]/g, ""));
    return Number.isFinite(myr) ? Number((myr * rates.MYR).toFixed(2)) : null;
  }
  return null;
}

function buildRows(records, rates) {
  const rows = [];
  for (const record of records) {
    if (record.status !== "ok") continue;
    const rankCounters = {};
    for (const item of record.items) {
      const sourceSite = record.retailer === "aeon" ? item.source || "aeon" : null;
      const rankKey = sourceSite || record.retailer;
      rankCounters[rankKey] = (rankCounters[rankKey] || 0) + 1;
      rows.push({
        category: record.category || null,
        product_type: record.productType,
        query_used: record.query,
        retailer: record.retailer,
        source_site: sourceSite,
        rank: rankCounters[rankKey],
        brand: item.brand || null,
        product_name_en: item.title || null,
        price_usd: priceToUsd(item, record.retailer, rates),
        rating: record.retailer === "amazon" ? parseRating(item.rating) : null,
        review_count: record.retailer === "amazon" ? parseReviewCount(item.reviewCount) : null,
        url: item.url || null,
        image_url: item.image || null,
      });
    }
  }
  return rows;
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const resultsPath = path.resolve(args.results);
  if (!fs.existsSync(resultsPath)) {
    throw new Error(`결과 파일이 없습니다: ${resultsPath}`);
  }

  const records = fs
    .readFileSync(resultsPath, "utf-8")
    .split("\n")
    .filter(Boolean)
    .map((line) => JSON.parse(line));

  const okCount = records.filter((r) => r.status === "ok").length;
  const errorCount = records.filter((r) => r.status === "error").length;
  const siteScope = [...new Set(records.map((r) => r.retailer))].sort().join(",");

  const rates = await getCurrencyToUsdRates();
  const rows = buildRows(records, rates);

  console.log(
    `[upload] 회차 요약: 성공 ${okCount}건 / 실패 ${errorCount}건, 총 상품 행 ${rows.length}개 (${siteScope})`
  );

  const backendUrl = process.env.BACKEND_URL;
  if (!backendUrl) throw new Error("BACKEND_URL 환경변수가 필요합니다.");

  const body = {
    site_scope: siteScope,
    row_count: rows.length,
    note: args.note || `crawl.js 자동 업로드 (성공 ${okCount}, 실패 ${errorCount})`,
    rows,
  };

  const res = await fetch(`${backendUrl.replace(/\/$/, "")}/api/product-sourcing/crawl-snapshot`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal: AbortSignal.timeout(120000),
  });

  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`업로드 실패: HTTP ${res.status} ${text.slice(0, 500)}`);
  }

  const result = await res.json();
  console.log("[upload] 완료:", JSON.stringify(result));
}

main().catch((error) => {
  console.error("[upload] 오류:", error.message);
  process.exit(1);
});
