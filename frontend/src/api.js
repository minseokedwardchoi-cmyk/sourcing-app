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

// 컬럼 필터 드롭다운은 같은 조건으로 여러 번 열릴 수 있어(재오픈, 여러 드롭다운 간
// 공유 컨텍스트 등) 매번 새로 조회하지 않도록 짧은 TTL로 응답을 캐싱 + 동시 요청을 dedupe.
const _columnValuesCache = new Map(); // key(URL) -> { ts, promise }
const COLUMN_VALUES_TTL_MS = 30000;

function normalizeMonthRange(dateFrom, dateTo) {
  const from = dateFrom && /^\d{4}-\d{2}$/.test(dateFrom) ? `${dateFrom}-01` : dateFrom;
  let to = dateTo;
  if (dateTo && /^\d{4}-\d{2}$/.test(dateTo)) {
    const [year, month] = dateTo.split("-").map(Number);
    const lastDay = new Date(year, month, 0).getDate();
    to = `${dateTo}-${String(lastDay).padStart(2, "0")}`;
  }
  return { from, to };
}

/** 컬럼별 고유값 목록 (contextParams로 현재 필터 컨텍스트 전달) */
export function fetchColumnValues(col, contextParams = {}) {
  const url = new URL(`${BASE_URL}/api/column-values`, window.location.origin);
  url.searchParams.set("col", col);
  const { search, competitor, dateFrom, dateTo, colFilters } = contextParams;
  if (search) url.searchParams.set("search", search);
  if (competitor && competitor !== "전체") url.searchParams.set("competitor", competitor);
  const { from, to } = normalizeMonthRange(dateFrom, dateTo);
  if (from) url.searchParams.set("date_from", from);
  if (to)   url.searchParams.set("date_to", to);
  const colMap = {
    category: "filter_category", mc: "filter_mc", import_type: "filter_import_type",
    importer: "filter_importer", country: "filter_country", factory: "filter_factory",
    email: "filter_email", sku_name: "filter_sku_name",
  };
  if (colFilters) {
    Object.entries(colFilters).forEach(([k, vals]) => {
      if (vals && vals.length > 0 && colMap[k]) {
        vals.forEach(v => url.searchParams.append(colMap[k], v));
      }
    });
  }

  const key = url.toString();
  const cached = _columnValuesCache.get(key);
  if (cached && Date.now() - cached.ts < COLUMN_VALUES_TTL_MS) {
    return cached.promise;
  }

  const promise = fetch(key)
    .then(res => {
      if (!res.ok) throw new Error("컬럼값 로드 실패");
      return res.json();
    })
    .catch(err => {
      _columnValuesCache.delete(key); // 실패한 요청은 캐싱하지 않음
      throw err;
    });

  _columnValuesCache.set(key, { ts: Date.now(), promise });
  return promise;
}

/** 메인 대시보드: SKU 이력 집계 */
export async function fetchSkuHistory({ search, competitor, sortBy, sortDir, page, pageSize, colFilters = {}, dateFrom, dateTo }) {
  const url = new URL(`${BASE_URL}/api/sku-history`, window.location.origin);
  const params = { search, competitor, sort_by: sortBy, sort_dir: sortDir, page, page_size: pageSize, date_from: dateFrom, date_to: dateTo };
  Object.entries(params).forEach(([k, v]) => {
    if (v !== null && v !== undefined && v !== "") url.searchParams.set(k, String(v));
  });
  // 컬럼별 체크박스 필터 (multi-value)
  const colMap = {
    category: "filter_category", mc: "filter_mc", import_type: "filter_import_type",
    importer: "filter_importer", country: "filter_country", factory: "filter_factory",
    email: "filter_email", sku_name: "filter_sku_name",
  };
  Object.entries(colFilters).forEach(([col, values]) => {
    if (values && values.length > 0 && colMap[col]) {
      values.forEach(v => url.searchParams.append(colMap[col], v));
    }
  });
  const res = await fetch(url.toString());
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail || "API 오류");
  }
  return res.json();
}

/** 행(그룹)별 월별 수입횟수 */
export async function fetchMonthlyImportCounts(row, dateFrom, dateTo) {
  const cols = ["category", "mc", "sku_name", "import_type", "importer", "manufacturer", "factory", "country"];
  const url = new URL(`${BASE_URL}/api/sku-history/monthly`, window.location.origin);
  cols.forEach(col => {
    const v = row[col];
    // null/undefined만 "값 없음"으로 취급. 빈 문자열("")은 실제 DB 값일 수 있으므로 그대로 전달.
    if (v !== null && v !== undefined) {
      url.searchParams.set(col, String(v));
    }
  });
  const { from, to } = normalizeMonthRange(dateFrom, dateTo);
  if (from) url.searchParams.set("date_from", from);
  if (to)   url.searchParams.set("date_to", to);
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 20000);
  try {
    const res = await fetch(url.toString(), { signal: controller.signal });
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      throw new Error(err.detail || "API 오류");
    }
    return res.json();
  } catch (e) {
    if (e.name === "AbortError") throw new Error("서버 응답 시간 초과 (20초). 잠시 후 다시 시도해주세요.");
    throw e;
  } finally {
    clearTimeout(timer);
  }
}

/** SKU 취급 제조사 목록 */
export function fetchSkuFactories(skuName, { search, countryFilter, hasContact, oemPossible, dateFrom, dateTo, page, pageSize } = {}) {
  return request(`/api/sku/${encodeURIComponent(skuName)}/factories`, {
    search,
    country_filter: countryFilter,
    has_contact:    hasContact,
    oem_possible:   oemPossible,
    date_from:      dateFrom,
    date_to:        dateTo,
    page,
    page_size: pageSize,
  });
}

/** 제조사 상세 정보 */
export function fetchManufacturerDetail(manufacturer, factory, { skuSearch, dateFrom, dateTo } = {}) {
  return request("/api/manufacturer", {
    manufacturer, factory,
    sku_search: skuSearch, date_from: dateFrom, date_to: dateTo,
  });
}

/** 제조사 전체 국내수입 연도별/월별 추이 */
export async function fetchManufacturerMonthlyImportCounts(manufacturer, factory, dateFrom, dateTo) {
  const url = new URL(BASE_URL + "/api/manufacturer/monthly", window.location.origin);
  url.searchParams.set("manufacturer", manufacturer);
  url.searchParams.set("factory", factory);
  const { from, to } = normalizeMonthRange(dateFrom, dateTo);
  if (from) url.searchParams.set("date_from", from);
  if (to)   url.searchParams.set("date_to", to);
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 20000);
  try {
    const res = await fetch(url.toString(), { signal: controller.signal });
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      throw new Error(err.detail || "API 오류");
    }
    return res.json();
  } catch (e) {
    if (e.name === "AbortError") throw new Error("서버 응답 시간 초과 (20초). 잠시 후 다시 시도해주세요.");
    throw e;
  } finally {
    clearTimeout(timer);
  }
}

/** DB 통계 */
export function fetchStats() {
  return request("/api/stats");
}

/** Excel 업로드 (파일을 그대로 서버에 전송, 서버에서 파싱/적재) */
export async function uploadExcel(file) {
  const form = new FormData();
  form.append("file", file);

  const res = await fetch(`${BASE_URL}/api/upload`, {
    method: "POST",
    body: form,
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail || "업로드 오류");
  }
  return res.json();
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

/** 전체 데이터 삭제 (복구 불가) */
export async function clearAllData() {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 120000);
  try {
    const res = await fetch(`${BASE_URL}/api/data`, {
      method: "DELETE",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ confirm: "DELETE" }),
      signal: controller.signal,
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      throw new Error(err.detail || "삭제 실패");
    }
    return res.json();
  } catch (e) {
    if (e.name === "AbortError") throw new Error("서버 응답 시간 초과 (120초). 잠시 후 다시 시도해주세요.");
    throw e;
  } finally {
    clearTimeout(timer);
  }
}

/** 경쟁사별 해외제조업체 수 통계 */
export function fetchCompetitorStats() {
  return request("/api/competitor-stats");
}

/** 국가 상세: 요약 (수입금액 순위/비중) */
export function fetchCountrySummary(country) {
  return request(`/api/countries/${encodeURIComponent(country)}/summary`);
}

/** 전체 수입금액 기준 국가별 비중 (파이차트용) */
export function fetchCountryAmountShare(topN = 8) {
  return request("/api/countries/amount-share", { top_n: topN });
}

/** 국가 상세: 주요 수입품목 TOP10 */
export function fetchCountryTopItems(country) {
  return request(`/api/countries/${encodeURIComponent(country)}/top-items`);
}

/** 국가 상세: 제조사 목록 */
export function fetchCountryManufacturers(country, { mc, query, sortBy, sortOrder, page, pageSize, dateFrom, dateTo } = {}) {
  return request(`/api/countries/${encodeURIComponent(country)}/manufacturers`, {
    mc, query, sort_by: sortBy, sort_order: sortOrder, page, page_size: pageSize,
    date_from: dateFrom, date_to: dateTo,
  });
}

/** 공장별 보기: SKU 이력 집계 (importer 제외 그룹핑) */
export async function fetchFactoryView({ search, competitor, sortBy, sortDir, page, pageSize, colFilters = {}, dateFrom, dateTo }) {
  const url = new URL(`${BASE_URL}/api/factory-view`, window.location.origin);
  const params = { search, competitor, sort_by: sortBy, sort_dir: sortDir, page, page_size: pageSize, date_from: dateFrom, date_to: dateTo };
  Object.entries(params).forEach(([k, v]) => {
    if (v !== null && v !== undefined && v !== "") url.searchParams.set(k, String(v));
  });
  const colMap = {
    category: "filter_category", mc: "filter_mc", import_type: "filter_import_type",
    importer: "filter_importer", country: "filter_country", factory: "filter_factory",
    email: "filter_email", sku_name: "filter_sku_name",
  };
  Object.entries(colFilters).forEach(([col, values]) => {
    if (values && values.length > 0 && colMap[col]) {
      values.forEach(v => url.searchParams.append(colMap[col], v));
    }
  });
  const res = await fetch(url.toString());
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail || "API 오류");
  }
  return res.json();
}

/** 공장별 보기: 행별 월별 수입횟수 (importer 미포함) */
export async function fetchFactoryViewMonthly(row, dateFrom, dateTo) {
  const cols = ["category", "mc", "sku_name", "import_type", "manufacturer", "factory", "country"];
  const url = new URL(`${BASE_URL}/api/factory-view/monthly`, window.location.origin);
  cols.forEach(col => {
    const v = row[col];
    if (v !== null && v !== undefined) {
      url.searchParams.set(col, String(v));
    }
  });
  const { from, to } = normalizeMonthRange(dateFrom, dateTo);
  if (from) url.searchParams.set("date_from", from);
  if (to)   url.searchParams.set("date_to", to);
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 20000);
  try {
    const res = await fetch(url.toString(), { signal: controller.signal });
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      throw new Error(err.detail || "API 오류");
    }
    return res.json();
  } catch (e) {
    if (e.name === "AbortError") throw new Error("서버 응답 시간 초과 (20초). 잠시 후 다시 시도해주세요.");
    throw e;
  } finally {
    clearTimeout(timer);
  }
}

