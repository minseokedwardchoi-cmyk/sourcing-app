/**
 * api.js — 백엔드 API 호출 모듈
 * BASE_URL은 .env 파일의 VITE_API_URL 환경변수로 관리
 */

const BASE_URL = "https://sourcing-backend-ucp5.onrender.com";

async function request(path, params = {}) {
  const url = new URL(`${BASE_URL}${path}`, window.location.origin);
  Object.entries(params).forEach(([k, v]) => {
    if (v !== null && v !== undefined && v !== "") {
      url.searchParams.set(k, String(v));
    }
  });
  const res = await fetch(url.toString());
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail || "API 오류");
  }
  return res.json();
}

/** 메인 대시보드: SKU 이력 집계 */
export function fetchSkuHistory({ search, competitor, sortBy, sortDir, page, pageSize }) {
  return request("/api/sku-history", {
    search,
    competitor,
    sort_by:   sortBy,
    sort_dir:  sortDir,
    page,
    page_size: pageSize,
  });
}

/** SKU 취급 제조사 목록 */
export function fetchSkuFactories(skuName, { search, countryFilter, hasContact, oemPossible, page, pageSize } = {}) {
  return request(`/api/sku/${encodeURIComponent(skuName)}/factories`, {
    search,
    country_filter: countryFilter,
    has_contact:    hasContact,
    oem_possible:   oemPossible,
    page,
    page_size: pageSize,
  });
}

/** 제조사 상세 정보 */
export function fetchManufacturerDetail(manufacturer, factory) {
  return request("/api/manufacturer", { manufacturer, factory });
}

/** DB 통계 */
export function fetchStats() {
  return request("/api/stats");
}

/** Excel 업로드 (프론트에서 JSON 변환 후 전송) */
export async function uploadExcel(file) {
  const XLSX = await import("xlsx");
  const buffer = await file.arrayBuffer();
  const wb = XLSX.read(buffer, { type: "array" });
  const ws = wb.Sheets[wb.SheetNames[0]];
  const rows = XLSX.utils.sheet_to_json(ws, { defval: null });

  const CHUNK = 2000;
  let totalInserted = 0;
  let totalSkipped = 0;

  for (let i = 0; i < rows.length; i += CHUNK) {
    const chunk = rows.slice(i, i + CHUNK);
    const res = await fetch(`${BASE_URL}/api/upload-json`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ rows: chunk }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      throw new Error(err.detail || "업로드 오류");
    }
    const data = await res.json();
    totalInserted += data.inserted || 0;
    totalSkipped += data.skipped || 0;
  }

  return { message: `${totalInserted}건 업로드 완료, ${totalSkipped}건 스킵`, inserted: totalInserted, skipped: totalSkipped };
}
/** 제조사 연락처 직접 수정 */
export async function updateManufacturerContact(payload) {
  const res = await fetch(`${BASE_URL}/api/manufacturer/contact`, {
    method: "PATCH",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify(payload),
  });

  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail || "연락처 저장 실패");
  }

  return res.json();
}
/** 제조사 연락처/인증서 Excel 일괄 보강 */
export async function uploadContacts(file, overwrite = false) {
  const form = new FormData();
  form.append("file", file);
  form.append("overwrite", overwrite ? "true" : "false");

  const res = await fetch(`${BASE_URL}/api/upload-contacts`, {
    method: "POST",
    body: form,
  });

  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail || "연락처 보강 업로드 실패");
  }

  return res.json();
}

/** 경쟁사별 해외제조업체 수 통계 */
export function fetchCompetitorStats() {
  return request("/api/competitor-stats");
}

