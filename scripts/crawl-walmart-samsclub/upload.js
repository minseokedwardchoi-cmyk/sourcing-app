import fs from "node:fs";
import path from "node:path";

// results.jsonl(crawl.js 결과)을 읽어 백엔드 이력 테이블에 업로드한다. 아마존/이온몰과
// 같은 엔드포인트(POST /api/product-sourcing/crawl-snapshot)를 그대로 재사용 — 스키마가
// 이미 retailer를 자유 문자열로 받아서 walmart/samsclub도 그대로 들어간다.
// product_sourcing_item(메인페이지 실제 데이터)은 건드리지 않는다.

function parseArgs(argv) {
  const args = { results: "results.jsonl", note: "" };
  for (const raw of argv) {
    const m = raw.match(/^--([a-zA-Z]+)=(.*)$/);
    if (m) args[m[1]] = m[2];
  }
  return args;
}

function parsePrice(priceText) {
  const m = String(priceText || "").match(/[\d,]+(?:\.\d+)?/);
  return m ? Number(m[0].replace(/,/g, "")) : null;
}

function parseRating(ratingText) {
  const m = String(ratingText || "").match(/([0-5](?:\.\d)?)/);
  return m ? Number(m[1]) : null;
}

function parseReviewCount(text) {
  const m = String(text || "").match(/([\d,]+)/);
  if (!m) return null;
  const value = Number(m[1].replace(/,/g, ""));
  return Number.isFinite(value) ? value : null;
}

function buildRows(records) {
  const rows = [];
  for (const record of records) {
    if (record.status !== "ok") continue;
    let rank = 0;
    for (const item of record.items) {
      rank += 1;
      rows.push({
        category: record.category || null,
        product_type: record.productType,
        query_used: record.query,
        retailer: record.retailer,
        source_site: null,
        rank,
        brand: item.brand || null,
        product_name_en: item.title || null,
        price_usd: parsePrice(item.price),
        rating: parseRating(item.rating),
        review_count: parseReviewCount(item.reviewCount),
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
  const rows = buildRows(records);

  console.log(
    `[upload] 회차 요약: 성공 ${okCount}건 / 실패 ${errorCount}건, 총 상품 행 ${rows.length}개 (${siteScope})`
  );

  const backendUrl = process.env.BACKEND_URL;
  if (!backendUrl) throw new Error("BACKEND_URL 환경변수가 필요합니다.");

  const body = {
    site_scope: siteScope,
    row_count: rows.length,
    note: args.note || `crawl.js(월마트/샘스클럽) 자동 업로드 (성공 ${okCount}, 실패 ${errorCount})`,
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
