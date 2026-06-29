import React, { useState, useMemo, useEffect, useLayoutEffect, useRef } from "react";
import { ComposableMap, Geographies, Geography } from "react-simple-maps";
import {
  fetchSkuHistory, fetchSkuFactories,
  fetchManufacturerDetail, uploadExcel,
  updateManufacturerContact, uploadContacts, clearAllData,
  fetchColumnValues, fetchMonthlyImportCounts,
  fetchCountrySummary, fetchCountryTopItems, fetchCountryManufacturers, fetchCountryAmountShare,
} from "./api.js";
import { getKoreanName } from "./countryGeo.js";
import worldGeoData from "world-atlas/countries-110m.json";

// ─── 경쟁사 필터 목록 ────────────────────────────────────────────────────────
const COMPETITORS = ["전체", "홈플러스", "이마트", "롯데마트", "쿠팡", "코스트코"];
const CARD_THEMES = {

  전체:    { bg: "#E8F5E9", active: "#2E7D32" },   // 초록
  코스트코: { bg: "#E3EAF6", active: "#1A3A6B" },   // 네이비
  이마트:  { bg: "#FFFDE7", active: "#F9A800" },   // 노랑
  롯데마트: { bg: "#FDECEA", active: "#C8001E" },   // 롯데 빨강
  홈플러스: { bg: "#E8F4FD", active: "#7B2FBE" },   // 보라
  쿠팡:   { bg: "#FFF3E0", active: "#FF6000" },   // 쿠팡 오렌지
};
// ─── 공통 CSS ────────────────────────────────────────────────────────────────
const styles = `
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: system-ui, -apple-system, sans-serif; background: #f5f6f8; color: #1a1a2e; font-size: 14px; }
  .app { min-height: 100vh; }
  .banner { background: #fff; border-bottom: 1px solid #e8eaed; padding: 12px 24px; position: sticky; top: 0; z-index: 50; }
  .banner-inner { max-width: 1400px; margin: 0 auto; display: flex; align-items: center; justify-content: space-between; gap: 16px; flex-wrap: wrap; }
  .banner-left h1 { font-size: 16px; font-weight: 600; color: #1a1a2e; }
  .banner-left p  { font-size: 11px; color: #6b7280; margin-top: 1px; }
  .banner-stats   { display: flex; gap: 20px; flex-wrap: wrap; }
  .stat-item      { text-align: center; }
  .stat-num       { font-size: 14px; font-weight: 600; color: #166534; }
  .stat-label     { font-size: 10px; color: #6b7280; margin-top: 1px; }
  .desc-bar { background: #f0fdf4; border-bottom: 1px solid #bbf7d0; padding: 6px 24px; }
  .desc-bar p { max-width: 1400px; margin: 0 auto; font-size: 11px; color: #15803d; }
  .page { max-width: 1400px; margin: 0 auto; padding: 16px 24px; }
  .back-btn { display: inline-flex; align-items: center; gap: 5px; font-size: 13px; color: #6b7280; background: none; border: none; cursor: pointer; padding: 4px 0; margin-bottom: 14px; }
  .back-btn:hover { color: #1a1a2e; }
  .page-header { margin-bottom: 14px; }
  .page-header h2 { font-size: 17px; font-weight: 600; }
  .page-header .sub { font-size: 12px; color: #6b7280; margin-top: 2px; }
  .sku-card { background: #fff; border: 1px solid #e8eaed; border-radius: 8px; padding: 12px 18px; margin-bottom: 14px; display: flex; gap: 28px; flex-wrap: wrap; }
  .sku-field-label { font-size: 10px; color: #6b7280; margin-bottom: 2px; text-transform: uppercase; letter-spacing: 0.4px; }
  .sku-field-value { font-size: 13px; font-weight: 500; }
  .card { background: #fff; border: 1px solid #e8eaed; border-radius: 8px; margin-bottom: 12px; }
  .card-header { padding: 11px 16px; border-bottom: 1px solid #e8eaed; display: flex; align-items: center; justify-content: space-between; }
  .sticky-panel-header { position: sticky; top: 0; z-index: 35; background: #fff; border-radius: 8px 8px 0 0; }
  .card-title { font-size: 13px; font-weight: 600; }
  .card-body { padding: 14px 16px; }
  .toolbar { padding: 9px 14px; border-bottom: 1px solid #e8eaed; display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
  .search-wrap { position: relative; flex: 1; min-width: 200px; }
  .search-wrap input { width: 100%; padding: 6px 10px 6px 30px; border: 1px solid #d1d5db; border-radius: 6px; font-size: 13px; background: #f9fafb; outline: none; color: #1a1a2e; }
  .search-wrap input:focus { border-color: #16a34a; background: #fff; }
  .search-icon { position: absolute; left: 8px; top: 50%; transform: translateY(-50%); font-size: 13px; color: #9ca3af; pointer-events: none; }
  .count-label { font-size: 12px; color: #6b7280; white-space: nowrap; }
  .icon-btn { display: inline-flex; align-items: center; gap: 4px; padding: 6px 10px; border: 1px solid #d1d5db; border-radius: 6px; font-size: 12px; background: #f9fafb; color: #374151; cursor: pointer; white-space: nowrap; }
  .icon-btn:hover { background: #f3f4f6; }
  .filter-bar { padding: 7px 14px; border-bottom: 1px solid #e8eaed; display: flex; gap: 5px; flex-wrap: wrap; align-items: center; }
  .filter-label { font-size: 11px; color: #6b7280; margin-right: 2px; }
  .pill { padding: 3px 11px; border-radius: 20px; font-size: 12px; border: 1px solid #d1d5db; background: #f9fafb; color: #374151; cursor: pointer; transition: all .12s; }
  .pill:hover { border-color: #16a34a; color: #15803d; }
  .pill.active { background: #16a34a; border-color: #16a34a; color: #fff; }
  .select-f { padding: 4px 8px; border: 1px solid #d1d5db; border-radius: 6px; font-size: 12px; background: #f9fafb; color: #1a1a2e; outline: none; cursor: pointer; }
  .table-wrap { overflow-x: auto; }
  table { border-collapse: collapse; font-size: 13px; table-layout: fixed; width: 100%; min-width: 1306px; }
  thead tr { background: #f8fafc; }
  th { padding: 8px 12px; text-align: left; font-size: 11px; font-weight: 600; color: #6b7280; border-bottom: 1px solid #e8eaed; white-space: nowrap; cursor: pointer; user-select: none; text-transform: uppercase; letter-spacing: 0.3px; background: #f8fafc; }
  th:hover { color: #1a1a2e; }
  th.col-highlight, td.col-highlight { background: #e0f2fe; }
  .sort-icon { margin-left: 3px; opacity: 0.4; }
  th.sorted .sort-icon { opacity: 1; color: #16a34a; }
  td { padding: 8px 12px; border-bottom: 1px solid #f1f3f5; color: #1a1a2e; vertical-align: middle; max-width: 260px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  tr:last-child td { border-bottom: none; }
  tbody tr:hover { background: #f0fdf4; }
  .link-cell { color: #1d4ed8; cursor: pointer; text-decoration: underline; text-underline-offset: 2px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; display: inline-block; max-width: 100%; }
  .link-cell:hover { color: #1e3a8a; }
  .email-cell { color: #6b7280; font-size: 12px; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 11px; font-weight: 500; margin: 1px; white-space: nowrap; }
  .b-green  { background: #dcfce7; color: #15803d; }
  .b-blue   { background: #dbeafe; color: #1d4ed8; }
  .b-gray   { background: #f3f4f6; color: #374151; border: 1px solid #e5e7eb; }
  .b-orange { background: #ffedd5; color: #c2410c; }
  .b-red    { background: #fee2e2; color: #b91c1c; }
  .b-teal   { background: #ccfbf1; color: #0f766e; }
  .b-count  { background: none; color: #1a1a2e; font-weight: 600; }
  .b-mc     { background: none; color: #1a1a2e; }
  .b-cat    { background: none; color: #1a1a2e; }
  .b-grade  { display: inline-flex; align-items: center; justify-content: center; width: 20px; height: 20px; border-radius: 50%; font-size: 11px; font-weight: 700; }
  .b-grade-a { background: #dcfce7; color: #15803d; }
  .b-grade-b { background: #dbeafe; color: #1d4ed8; }
  .b-grade-c { background: #fee2e2; color: #b91c1c; }
  .score-cell { font-weight: 600; color: #1a1a2e; }
  .grade-evidence { font-size: 11px; color: #6b7280; white-space: nowrap; }
  .pagination { padding: 9px 14px; display: flex; align-items: center; justify-content: space-between; border-top: 1px solid #e8eaed; flex-wrap: wrap; gap: 6px; }
  .page-btns { display: flex; gap: 3px; }
  .page-btn { padding: 4px 8px; border: 1px solid #d1d5db; border-radius: 5px; font-size: 12px; cursor: pointer; background: #f9fafb; color: #374151; }
  .page-btn:hover { background: #f3f4f6; }
  .page-btn.active { background: #16a34a; border-color: #16a34a; color: #fff; }
  .page-btn:disabled { opacity: 0.35; cursor: not-allowed; }
  .mfr-header { background: #fff; border: 1px solid #e8eaed; border-radius: 8px; padding: 18px 22px; margin-bottom: 12px; }
  .mfr-country { font-size: 11px; color: #6b7280; margin-bottom: 3px; }
  .mfr-name    { font-size: 20px; font-weight: 600; margin-bottom: 2px; word-break: break-word; }
  .mfr-factory { font-size: 13px; color: #6b7280; margin-bottom: 4px; word-break: break-word; }
  .badge-row   { display: flex; gap: 5px; flex-wrap: wrap; margin-top: 10px; }
  .detail-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
  @media (max-width: 720px) { .detail-grid { grid-template-columns: 1fr; } }
  .detail-row { display: flex; padding: 6px 0; border-bottom: 1px solid #f1f3f5; gap: 10px; }
  .detail-row:last-child { border-bottom: none; }
  .dk { font-size: 11px; color: #6b7280; width: 100px; flex-shrink: 0; padding-top: 2px; }
  .dv { font-size: 13px; flex: 1; word-break: break-word; }
  .detail-link { color: #1d4ed8; text-decoration: underline; text-underline-offset: 2px; }
  .acc-toggle { width: 100%; background: none; border: none; cursor: pointer; display: flex; align-items: center; justify-content: space-between; padding: 11px 16px; font-size: 13px; font-weight: 600; color: #1a1a2e; }
  .acc-toggle:hover { background: #f8fafc; }
  .skeleton { background: linear-gradient(90deg,#f0f0f0 25%,#e0e0e0 50%,#f0f0f0 75%); background-size: 200% 100%; animation: shimmer 1.4s infinite; border-radius: 4px; height: 13px; margin: 5px 0; }
  @keyframes shimmer { 0%{background-position:200% 0}100%{background-position:-200% 0} }
  .empty-state { padding: 40px; text-align: center; color: #9ca3af; font-size: 13px; }
  .error-box { padding: 10px 14px; background: #fee2e2; border: 1px solid #fecaca; border-radius: 6px; color: #b91c1c; font-size: 13px; margin: 10px 14px; }
  .upload-btn { display: inline-flex; align-items: center; gap: 5px; padding: 7px 14px; background: #16a34a; color: #fff; border: none; border-radius: 6px; font-size: 13px; font-weight: 500; cursor: pointer; }
  .upload-btn:hover { background: #15803d; }
  .upload-btn:disabled { opacity: 0.5; cursor: not-allowed; }
  .contact-edit-box { margin-top: 12px; padding-top: 12px; border-top: 1px solid #f1f3f5; }
  .contact-edit-title { font-size: 12px; font-weight: 600; margin-bottom: 8px; color: #374151; }
  .contact-edit-grid { display: grid; grid-template-columns: 1fr; gap: 8px; }
  .contact-edit-field label { display: block; font-size: 11px; color: #6b7280; margin-bottom: 4px; }
  .contact-edit-field input { width: 100%; padding: 7px 9px; border: 1px solid #d1d5db; border-radius: 6px; font-size: 13px; outline: none; background: #f9fafb; color: #1a1a2e; }
  .contact-edit-field input:focus { border-color: #16a34a; background: #fff; }
  .contact-msg { margin-top: 8px; font-size: 12px; }
  .contact-msg.ok { color: #15803d; }
  .contact-msg.fail { color: #b91c1c; }
  .col-wrap { position: relative; }
  .col-dropdown { position: absolute; right: 0; top: calc(100% + 4px); background: #fff; border: 1px solid #d1d5db; border-radius: 8px; padding: 6px; z-index: 100; min-width: 150px; box-shadow: 0 4px 16px rgba(0,0,0,.08); }
  .col-item { display: flex; align-items: center; gap: 7px; padding: 4px 8px; cursor: pointer; border-radius: 4px; font-size: 12px; }
  .col-item:hover { background: #f3f4f6; }
  .th-inner { display:flex; align-items:center; justify-content:space-between; gap:2px; }
  .th-label { white-space:nowrap; flex:1; overflow:hidden; text-overflow:ellipsis; min-width:0; }
  .filter-icon-btn { background:none; border:none; cursor:pointer; padding:2px 2px; border-radius:2px; font-size:13px; color:#9ca3af; line-height:1; flex-shrink:0; }
  .filter-icon-btn:hover { background:#e5e7eb; color:#374151; }
  .filter-icon-btn.active { color:#16a34a; background:#dcfce7; }
  .filter-dropdown { position:absolute; top:calc(100% + 2px); left:0; z-index:200; background:#fff; border:1px solid #bdc3c7; border-radius:4px; box-shadow:0 4px 16px rgba(0,0,0,.18); min-width:220px; max-width:300px; }
  .filter-sort-section { padding:4px 0; border-bottom:1px solid #e8eaed; }
  .filter-sort-btn { display:flex; align-items:center; gap:8px; width:100%; background:none; border:none; cursor:pointer; padding:6px 12px; font-size:12px; color:#1a1a2e; text-align:left; }
  .filter-sort-btn:hover { background:#f0f0f0; }
  .filter-sort-icon { font-size:11px; color:#6b7280; }
  .filter-divider { height:1px; background:#e8eaed; margin:2px 0; }
  .filter-search-section { padding:6px 8px; border-bottom:1px solid #e8eaed; }
  .filter-search { width:100%; padding:5px 8px; border:1px solid #bdc3c7; border-radius:3px; font-size:12px; outline:none; }
  .filter-search:focus { border-color:#16a34a; }
  .filter-list { max-height:200px; overflow-y:auto; padding:2px 0; }
  .filter-item { display:flex; align-items:center; gap:7px; padding:4px 12px; cursor:pointer; font-size:12px; }
  .filter-item:hover { background:#f0f0f0; }
  .filter-item input[type=checkbox] { flex-shrink:0; cursor:pointer; accent-color:#16a34a; }
  .filter-item span { white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .filter-actions { display:flex; gap:6px; padding:7px 10px; border-top:1px solid #e8eaed; justify-content:flex-end; background:#f9fafb; border-radius:0 0 4px 4px; }
  .filter-ok-btn { padding:4px 16px; background:#16a34a; color:#fff; border:none; border-radius:3px; font-size:12px; cursor:pointer; font-weight:500; }
  .filter-ok-btn:hover { background:#15803d; }
  .filter-cancel-btn { padding:4px 12px; background:#fff; color:#374151; border:1px solid #d1d5db; border-radius:3px; font-size:12px; cursor:pointer; }
  .filter-cancel-btn:hover { background:#f3f4f6; }
  .monthly-row td { padding:0; background:#dde1e6; }
  .monthly-panel { padding:10px 14px; overflow-x:auto; animation: monthlySlide .15s ease-out; }
  @keyframes monthlySlide { from { opacity:0; transform: translateY(-6px); } to { opacity:1; transform: translateY(0); } }
  .monthly-table { width:auto; border-collapse: collapse; font-size:12px; table-layout:fixed; }
  .monthly-table td { padding:5px 10px; border:1px solid #e8eaed; text-align:center; white-space:nowrap; background:#fff; width:54px; max-width:54px; }
  .monthly-table td.monthly-table-label { font-weight:600; color:#6b7280; background:#f1f3f5 !important; position:sticky; left:0; width:70px; max-width:70px; }
  .date-range-wrap { display:flex; align-items:center; gap:5px; }
  .date-range-input { padding:6px 8px; border:1px solid #d1d5db; border-radius:6px; font-size:12px; background:#f9fafb; color:#1a1a2e; outline:none; }
  .date-range-input:focus { border-color:#16a34a; background:#fff; }
  .date-range-sep { font-size:12px; color:#9ca3af; }
  .date-range-clear { border:none; background:#f3f4f6; color:#6b7280; border-radius:50%; width:20px; height:20px; cursor:pointer; font-size:11px; line-height:1; }
  .date-range-clear:hover { background:#e5e7eb; color:#374151; }
  .hero { background: #f0fdf4; border-bottom: 1px solid #bbf7d0; padding: 32px 0 28px; }
  .hero-inner { max-width: 1400px; margin: 0 auto; padding: 0 32px; }
  .hero-title { font-size: 32px; font-weight: 700; color: #0f172a; letter-spacing: -0.5px; margin-bottom: 8px; }
  .hero-desc  { font-size: 16px; color: #475569; margin-bottom: 28px; line-height: 1.6; }
  .hero-kpi   { display: flex; gap: 0; border-top: 1px solid #e8eaed; padding-top: 24px; }
  .hero-kpi-item { flex: 1; padding: 0 28px 0 0; }
  .hero-kpi-item + .hero-kpi-item { padding-left: 28px; border-left: 1px solid #e8eaed; }
  .hero-kpi-label { font-size: 13px; font-weight: 600; color: #64748b; letter-spacing: 0.3px; margin-bottom: 6px; }
  .hero-kpi-num   { font-size: 28px; font-weight: 700; color: #0f172a; line-height: 1; }
  .hero-kpi-unit  { font-size: 13px; font-weight: 400; color: #64748b; margin-left: 3px; }
  .notice { padding: 8px 14px; margin: 0 0 10px; background: #fffbeb; border: 1px solid #fde68a; border-radius: 6px; font-size: 12px; color: #92400e; }
  .competitor-cards { display: grid; grid-template-columns: repeat(6, 1fr); gap: 8px; margin-bottom: 4px; }
  .comp-card { display: flex; flex-direction: column; gap: 4px; padding: 10px 12px; border-radius: 10px; border: 1.5px solid #e2e8f0; background: #f9fafb; cursor: pointer; transition: all .15s; text-align: left; }
  .comp-card:hover { border-color: #16a34a; box-shadow: 0 2px 8px rgba(22,163,74,.12); }
  .comp-card.active { color: #fff; border-color: transparent; box-shadow: 0 2px 10px rgba(0,0,0,.15); }
  .comp-card-name { font-size: 11px; font-weight: 600; }
  .comp-card-num  { font-size: 20px; font-weight: 700; line-height: 1; }
  .comp-card-label { font-size: 10px; opacity: .7; margin-top: -2px; }
  .country-header-card { padding: 16px 18px; }
  .country-header-row { display: flex; align-items: center; justify-content: space-between; gap: 20px; flex-wrap: wrap; }
  .country-title-block { flex: 1; min-width: 220px; }
  .country-title { font-size: 20px; font-weight: 700; margin-bottom: 6px; }
  .country-rank-line { font-size: 13px; color: #374151; }
  .country-rank-line.muted { color: #9ca3af; }
  .country-stat-row { display: flex; gap: 6px; margin-top: 8px; flex-wrap: wrap; }
  .country-pie-block { display: flex; align-items: center; gap: 12px; }
  .country-pie-legend { font-size: 12px; color: #374151; display: flex; flex-direction: column; gap: 4px; }
  .country-amount-share-grid { display: grid; grid-template-columns: repeat(2, auto); gap: 4px 24px; font-size: 12px; color: #374151; }
  .country-amount-share-item { display: flex; align-items: center; gap: 4px; white-space: nowrap; }
  .legend-dot { display: inline-block; width: 9px; height: 9px; border-radius: 50%; margin-right: 6px; }
  .country-top-items { display: flex; align-items: flex-start; gap: 20px; flex-wrap: wrap; }
  .country-top-legend { flex: 1; min-width: 220px; display: grid; grid-template-columns: 1fr 1fr; gap: 4px 14px; }
  .sku-cell { display: flex; align-items: center; gap: 4px; }
  .sku-cell-text { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 1; min-width: 0; display: inline-block; }
  .sku-expand-btn { flex: 0 0 auto; border: none; background: none; cursor: pointer; font-size: 9px; color: #9ca3af; padding: 2px; line-height: 1; }
  .sku-expand-btn:hover { color: #374151; }
  .count-cell-wrap { display: inline-flex; align-items: center; gap: 3px; }
  .modal-overlay { position: fixed; inset: 0; background: rgba(0,0,0,.4); display: flex; align-items: center; justify-content: center; z-index: 100; }
  .modal-box { background: #fff; border-radius: 10px; max-width: 90vw; max-height: 80vh; overflow: auto; min-width: 360px; box-shadow: 0 10px 40px rgba(0,0,0,.2); }
  .modal-header { display: flex; align-items: center; justify-content: space-between; padding: 12px 16px; border-bottom: 1px solid #e8eaed; position: sticky; top: 0; background: #fff; }
  .modal-title { font-size: 13px; font-weight: 600; }
  .modal-close { border: none; background: none; cursor: pointer; font-size: 14px; color: #6b7280; }
  .modal-close:hover { color: #1a1a2e; }
  .modal-body { padding: 14px 16px; }
  .modal-section-title { font-size: 12px; font-weight: 600; color: #374151; margin-bottom: 6px; }
`;

// ─── 컬럼 필터 컴포넌트 (엑셀 스타일) ───────────────────────────────────────
function ColumnFilter({ colKey, isNumeric, activeValues, activeSortCol, activeSortDir, onApply, onSort }) {
  const [open, setOpen]         = useState(false);
  const [values, setValues]     = useState([]);
  const [search, setSearch]     = useState("");
  const [selected, setSelected] = useState(new Set(activeValues || []));
  const [loading, setLoading]   = useState(false);
  const ref = useRef(null);

  // 드롭다운 열릴 때 값 로드
  useEffect(() => {
    if (!open || !colKey) return;
    setLoading(true);
    fetchColumnValues(colKey).then(setValues).finally(() => setLoading(false));
  }, [open, colKey]);

  // 드롭다운 닫힐 때 선택값 원래대로 복원 (취소 효과)
  useEffect(() => {
    if (!open) { setSelected(new Set(activeValues || [])); setSearch(""); }
  }, [open, activeValues]);

  // 외부 클릭 시 닫기
  useEffect(() => {
    const h = e => { if (ref.current && !ref.current.contains(e.target)) setOpen(false); };
    document.addEventListener("mousedown", h);
    return () => document.removeEventListener("mousedown", h);
  }, []);

  const filtered = values.filter(v => !search || String(v).toLowerCase().includes(search.toLowerCase()));
  const allSelected = filtered.length > 0 && filtered.every(v => selected.has(String(v)));

  function toggleAll() {
    setSelected(prev => {
      const s = new Set(prev);
      if (allSelected) filtered.forEach(v => s.delete(String(v)));
      else filtered.forEach(v => s.add(String(v)));
      return s;
    });
  }

  function toggle(v) {
    setSelected(prev => { const s = new Set(prev); s.has(v) ? s.delete(v) : s.add(v); return s; });
  }

  function handleSort(dir) {
    onSort(dir);
    setOpen(false);
  }

  function handleOk() {
    const sel = [...selected];
    onApply(sel.length === 0 || sel.length === values.length ? null : sel);
    setOpen(false);
  }

  function handleCancel() {
    setOpen(false); // 선택값 복원은 위 useEffect에서 처리
  }

  const isActive = (activeValues && activeValues.length > 0) || activeSortCol;
  const sortAscLabel  = isNumeric ? "숫자 오름차순 정렬" : "텍스트 오름차순 정렬 (ㄱ→ㅎ)";
  const sortDescLabel = isNumeric ? "숫자 내림차순 정렬" : "텍스트 내림차순 정렬 (ㅎ→ㄱ)";
  const sortAscIcon   = isNumeric ? "1→9" : "ㄱ→ㅎ";
  const sortDescIcon  = isNumeric ? "9→1" : "ㅎ→ㄱ";

  return (
    <div ref={ref} style={{ position:"relative", display:"inline-block" }} onClick={e => e.stopPropagation()}>
      <button
        className={`filter-icon-btn${isActive ? " active" : ""}`}
        onClick={() => setOpen(v => !v)}
        title="필터"
      >▾</button>

      {open && (
        <div className="filter-dropdown">
          {/* 정렬 섹션 */}
          <div className="filter-sort-section">
            <button className="filter-sort-btn" onClick={() => handleSort("asc")}>
              <span className="filter-sort-icon">{sortAscIcon}</span>
              {sortAscLabel}
              {activeSortCol && activeSortDir === "asc" && <span style={{marginLeft:"auto",color:"#16a34a"}}>✓</span>}
            </button>
            <button className="filter-sort-btn" onClick={() => handleSort("desc")}>
              <span className="filter-sort-icon">{sortDescIcon}</span>
              {sortDescLabel}
              {activeSortCol && activeSortDir === "desc" && <span style={{marginLeft:"auto",color:"#16a34a"}}>✓</span>}
            </button>
          </div>

          {/* 검색 + 체크박스 (텍스트 컬럼만) */}
          {colKey && (
            <>
              <div className="filter-search-section">
                <input
                  className="filter-search"
                  placeholder="검색..."
                  value={search}
                  onChange={e => setSearch(e.target.value)}
                />
              </div>
              <div className="filter-list">
                {loading
                  ? <div style={{ padding:"10px", fontSize:12, color:"#9ca3af", textAlign:"center" }}>로딩 중...</div>
                  : <>
                    <label className="filter-item">
                      <input type="checkbox" checked={allSelected} onChange={toggleAll} />
                      <span style={{ fontWeight:600 }}>(모두 선택)</span>
                    </label>
                    {filtered.map((v, i) => (
                      <label key={i} className="filter-item">
                        <input type="checkbox" checked={selected.has(String(v))} onChange={() => toggle(String(v))} />
                        <span title={String(v)}>{v || "(비어있음)"}</span>
                      </label>
                    ))}
                    {filtered.length === 0 && <div style={{ padding:"8px 12px", fontSize:12, color:"#9ca3af" }}>결과 없음</div>}
                  </>
                }
              </div>
              <div className="filter-actions">
                <button className="filter-ok-btn" onClick={handleOk}>확인</button>
                <button className="filter-cancel-btn" onClick={handleCancel}>취소</button>
              </div>
            </>
          )}

          {/* 숫자 컬럼은 정렬만 */}
          {!colKey && (
            <div style={{ padding:"6px 10px 8px", borderTop:"1px solid #e8eaed" }}>
              <button className="filter-cancel-btn" style={{width:"100%"}} onClick={handleCancel}>닫기</button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ─── 유틸 ─────────────────────────────────────────────────────────────────────
function OemBadge({ value }) {
  if (!value) return <span className="badge b-gray">-</span>;
  if (value === "OEM" || (value.includes("가능") && !value.includes("문의"))) return <span className="badge b-green">{value}</span>;
  if (value.includes("문의")) return <span className="badge b-orange">{value}</span>;
  if (value === "수입" || value.includes("불가")) return <span className="badge b-gray">{value}</span>;
  return <span className="badge b-gray">{value}</span>;
}

function GradeBadge({ grade }) {
  if (!grade) return <span style={{color:"#9ca3af",fontSize:12}}>-</span>;
  const cls = grade === "A" ? "b-grade-a" : grade === "B" ? "b-grade-b" : "b-grade-c";
  return <span className={`b-grade ${cls}`} title={`등급 ${grade}`}>{grade}</span>;
  return <span className="badge b-gray">{value}</span>;
}

function SortIcon({ col, sortCol, sortDir }) {
  if (sortCol !== col) return <span className="sort-icon">↕</span>;
  return <span className="sort-icon">{sortDir === "asc" ? "↑" : "↓"}</span>;
}

function SkeletonRows({ cols = 9, rows = 10 }) {
  return Array.from({ length: rows }).map((_, i) => (
    <tr key={i}>{Array.from({ length: cols }).map((__, j) => (
      <td key={j}><div className="skeleton" style={{ width: `${55 + Math.random() * 40}%` }} /></td>
    ))}</tr>
  ));
}

function Pagination({ meta, page, setPage }) {
  if (!meta || meta.total_pages <= 1) return null;
  const start = Math.max(1, Math.min(meta.total_pages - 4, page - 2));
  const pages = Array.from({ length: Math.min(5, meta.total_pages) }, (_, i) => start + i);
  return (
    <div className="pagination">
      <span className="count-label">총 {meta.total.toLocaleString()}건 | 페이지 {page}/{meta.total_pages}</span>
      <div className="page-btns">
        <button className="page-btn" disabled={page===1} onClick={()=>setPage(1)}>«</button>
        <button className="page-btn" disabled={page===1} onClick={()=>setPage(p=>p-1)}>‹</button>
        {pages.map(p=><button key={p} className={`page-btn${page===p?" active":""}`} onClick={()=>setPage(p)}>{p}</button>)}
        <button className="page-btn" disabled={page===meta.total_pages} onClick={()=>setPage(p=>p+1)}>›</button>
        <button className="page-btn" disabled={page===meta.total_pages} onClick={()=>setPage(meta.total_pages)}>»</button>
      </div>
    </div>
  );
}

function downloadCSV(data, filename) {
  if (!data?.length) return;
  const keys = Object.keys(data[0]);
  const rows = [keys.join(","), ...data.map(r=>keys.map(k=>`"${(r[k]??"").toString().replace(/"/g,'""')}"`).join(","))];
  const blob = new Blob(["\uFEFF"+rows.join("\n")], {type:"text/csv;charset=utf-8;"});
  const a = document.createElement("a"); a.href=URL.createObjectURL(blob); a.download=filename; a.click();
}

// ─── 컬럼 정의 (실제 DB 필드 기준) ──────────────────────────────────────────
// 연도별 컬럼 레이블 (base_year는 첫 데이터 행에서 읽음, 없으면 현재연도 사용)
function yearLabel(offset, baseYear) {
  const y = (baseYear || new Date().getFullYear()) - offset;
  return `${y}년`;
}

const ALL_COLS = [
  { key:"category",     label:"구분",           w:118, filterKey:"category"               },
  { key:"mc",           label:"MC",             w:105, filterKey:"mc",      isMc:true      },
  { key:"sku_name",     label:"제품명",         w:200, filterKey:"sku_name", clickable:"sku" },
  { key:"import_type",  label:"OEM/수입",       w:92,  filterKey:"import_type"             },
  { key:"importer",     label:"수입업체",       w:118, filterKey:"importer"                },
  { key:"factory",      label:"해외제조업소",   w:110, filterKey:"factory", clickable:"mfr" },
  { key:"country",      label:"제조국",         w:105, filterKey:"country", clickable:"country" },
  { key:"import_count", label:"수입횟수(전체)", w:100, isNumeric:true                      },
  { key:"count_year3",  label:"",               w:78,  isYearCount:3                      },
  { key:"count_year2",  label:"",               w:78,  isYearCount:2                      },
  { key:"count_year1",  label:"",               w:78,  isYearCount:1                      },
  { key:"email",        label:"이메일",         w:160, filterKey:"email"                   },
];

// ═══════════════════════════════════════════════════════════════════════════════
// PAGE 1: 메인 대시보드
// ═══════════════════════════════════════════════════════════════════════════════
function MainDashboard({ navigate }) {
  const [data,        setData]        = useState([]);
  const [meta,        setMeta]        = useState(null);
  const [loading,     setLoading]     = useState(true);
  const [error,       setError]       = useState(null);
  const [search,      setSearch]      = useState("");
  const [debSearch,   setDebSearch]   = useState("");
  const [dateFrom,    setDateFrom]    = useState("");
  const [dateTo,      setDateTo]      = useState("");
  const [competitor,  setCompetitor]  = useState("전체");
  const [sortBy,      setSortBy]      = useState("import_count");
  const [sortDir,     setSortDir]     = useState("desc");
  const [page,        setPage]        = useState(1);
  const [visibleCols, setVisibleCols] = useState(ALL_COLS.map(c=>c.key));
  const [showColMenu, setShowColMenu] = useState(false);
  const [uploading,   setUploading]   = useState(false);
  const [uploadMsg,   setUploadMsg]   = useState(null);
  const [colFilters,  setColFilters]  = useState({});
  const [expandedOverflow, setExpandedOverflow] = useState(()=>new Set());  // 픽셀 오버플로우 기반 펼침 상태 (`${colKey}:${i}`)
  const [overflowCells,    setOverflowCells]    = useState(()=>new Set());  // 잘려서 펼치기 화살표가 필요한 셀 (`${colKey}:${i}`)
  const [expandedCells,   setExpandedCells]   = useState(()=>new Set());  // 구분/MC/수입업체/제조국 글자수 제한 펼침 상태 (`${colKey}:${i}`)
  const [monthlyModal, setMonthlyModal] = useState(null);   // { row, loading, error, yearly, monthly }
  const colMenuRef = useRef(null);
  const fileRef    = useRef(null);

  function cellKey(colKey, i) { return `${colKey}:${i}`; }

  function toggleOverflowExpand(colKey, i) {
    const key = cellKey(colKey, i);
    setExpandedOverflow(prev => {
      const next = new Set(prev);
      next.has(key) ? next.delete(key) : next.add(key);
      return next;
    });
  }

  function overflowTextRef(el, colKey, i) {
    if (!el) return;
    const key = cellKey(colKey, i);
    const isOverflowing = el.scrollWidth > el.clientWidth + 1;
    setOverflowCells(prev => {
      const has = prev.has(key);
      if (isOverflowing === has) return prev;
      const next = new Set(prev);
      isOverflowing ? next.add(key) : next.delete(key);
      return next;
    });
  }

  function toggleCell(colKey, i) {
    setExpandedCells(prev => {
      const k = cellKey(colKey, i);
      const next = new Set(prev);
      next.has(k) ? next.delete(k) : next.add(k);
      return next;
    });
  }

  function renderTrunc(colKey, value, limit, i, { badgeClass, navTo } = {}) {
    if (!value) return badgeClass ? <span className={`badge ${badgeClass}`}>-</span> : "-";
    const key = cellKey(colKey, i);
    const expanded = expandedCells.has(key);
    const over = value.length > limit;
    const text = expanded || !over ? value : value.slice(0, limit) + "…";
    const textStyle = expanded ? { whiteSpace:"normal", wordBreak:"break-all" } : undefined;
    const inner = badgeClass
      ? <span className={`badge ${badgeClass}`} style={textStyle}>{text}</span>
      : navTo
      ? <span className="link-cell" style={textStyle} onClick={navTo}>{text}</span>
      : <span style={textStyle}>{text}</span>;
    return (
      <div className="sku-cell">
        {inner}
        {over && (
          <button className="sku-expand-btn" onClick={(e)=>{e.stopPropagation();toggleCell(colKey,i);}} title={expanded?"접기":"펼치기"}>
            {expanded?"▲":"▼"}
          </button>
        )}
      </div>
    );
  }

  function openMonthlyModal(row) {
    setMonthlyModal({ row, loading: true, error: null, yearly: [], monthly: [] });
    fetchMonthlyImportCounts(row, dateFrom, dateTo)
      .then(res => setMonthlyModal(m => (m && m.row===row) ? { ...m, loading:false, yearly: res.yearly||[], monthly: res.data||[] } : m))
      .catch(e => setMonthlyModal(m => (m && m.row===row) ? { ...m, loading:false, error: e.message || "조회 실패" } : m));
  }

  // 히어로(KPI) 영역은 페이지와 함께 스크롤되어 사라지고, 패널 헤더(경쟁사카드+툴바)가
  // 화면 상단에 고정되면, 그 아래 테이블 영역이 남은 뷰포트 높이를 모두 차지하며
  // 자체적으로(가로/세로) 스크롤되어 테이블 헤더가 항상 보이도록 높이를 계산
  const stickyHeaderRef = useRef(null);
  const paginationRef   = useRef(null);
  const [stickyHeaderHeight, setStickyHeaderHeight] = useState(0);
  const [tableMaxHeight,     setTableMaxHeight]     = useState(null);
  useLayoutEffect(() => {
    const headerEl = stickyHeaderRef.current;
    if (!headerEl) return;
    const update = () => {
      const headerHeight = headerEl.offsetHeight;
      const paginationHeight = paginationRef.current ? paginationRef.current.offsetHeight : 0;
      setStickyHeaderHeight(headerHeight);
      setTableMaxHeight(Math.max(200, window.innerHeight - headerHeight - paginationHeight - 16));
    };
    update();
    window.addEventListener("resize", update);
    const ro = new ResizeObserver(update);
    ro.observe(headerEl);
    if (paginationRef.current) ro.observe(paginationRef.current);
    return () => { window.removeEventListener("resize", update); ro.disconnect(); };
  }, [showColMenu]);

  // 검색 디바운스 500ms
  useEffect(()=>{ const t=setTimeout(()=>{setDebSearch(search);setPage(1);},500); return()=>clearTimeout(t); },[search]);

  // 기간 필터 변경 시 1페이지로
  useEffect(()=>{ setPage(1); },[dateFrom,dateTo]);

  // 데이터
  useEffect(()=>{
    setLoading(true); setError(null);
    fetchSkuHistory({search:debSearch,competitor,sortBy,sortDir,page,pageSize:50,colFilters,dateFrom,dateTo})
      .then(r=>{setData(r.data);setMeta(r.meta);})
      .catch(e=>setError(e.message))
      .finally(()=>setLoading(false));
  },[debSearch,competitor,sortBy,sortDir,page,colFilters,dateFrom,dateTo]);

  useEffect(()=>{
    const h=e=>{if(colMenuRef.current&&!colMenuRef.current.contains(e.target))setShowColMenu(false);};
    document.addEventListener("mousedown",h); return()=>document.removeEventListener("mousedown",h);
  },[]);

  function handleSort(col){
    if(sortBy===col) setSortDir(d=>d==="asc"?"desc":"asc");
    else{setSortBy(col);setSortDir("asc");}
    setPage(1);
  }

  async function handleUpload(e){
    const file=e.target.files?.[0]; if(!file)return;
    setUploading(true); setUploadMsg(null);
    try{
      const res=await uploadExcel(file);
      setUploadMsg({ok:true,text:res.message});
      const r2=await fetchSkuHistory({search:debSearch,competitor,sortBy,sortDir,page,pageSize:50});
      setData(r2.data); setMeta(r2.meta);
    }catch(err){setUploadMsg({ok:false,text:err.message});}
    finally{setUploading(false); e.target.value="";}
  }
  async function handleClearAllData(){
    if(!window.confirm("정말 모든 데이터를 삭제하시겠습니까? 이 작업은 되돌릴 수 없습니다."))return;
    if(window.prompt("삭제를 진행하려면 'DELETE'를 입력하세요.") !== "DELETE")return;
    setUploading(true); setUploadMsg(null);
    try{
      const res=await clearAllData();
      setUploadMsg({ok:true,text:res.message});
      const r2=await fetchSkuHistory({search:debSearch,competitor,sortBy,sortDir,page,pageSize:50});
      setData(r2.data); setMeta(r2.meta);
    }catch(err){setUploadMsg({ok:false,text:err.message});}
    finally{setUploading(false);}
  }
  async function handleContactExcelUpload(e) {
  const file = e.target.files?.[0];
  if (!file) return;

  setUploading(true);
  setUploadMsg(null);

  try {
    const res = await uploadContacts(file, true);

    setUploadMsg({
      ok: true,
      text: res.message,
    });

    const refreshed = await fetchSkuHistory({
      search: debSearch,
      competitor,
      sortBy,
      sortDir,
      page,
      pageSize: 50,
    });

    setData(refreshed.data);
    setMeta(refreshed.meta);
    setError(null);
  } catch (err) {
    setUploadMsg({
      ok: false,
      text: err.message || "연락처 보강 업로드 실패",
    });
  } finally {
    setUploading(false);
    e.target.value = "";
  }
}

  const baseYear = data[0]?.base_year || new Date().getFullYear();
  const cols = ALL_COLS
    .filter(c=>visibleCols.includes(c.key))
    .map(c => c.isYearCount ? { ...c, label: yearLabel(c.isYearCount, baseYear) } : c);

  return (
    <div className="app">
      <style>{styles}</style>
      {/* Hero */}
      <div className="hero">
        <div className="hero-inner">
          <div className="hero-title">해외 제조업체 발굴 대시보드</div>
          <div className="hero-desc">국내 식품 수입 이력을 기반으로 해외 제조업체와 관련 상품 정보를 한눈에 확인하는 대시보드입니다.</div>
          <div className="hero-kpi">
            {[
              { label: "해외 제조업체", val: 40658,    unit: "개" },
              { label: "OEM 업체",      val: 2249,     unit: "개" },
              { label: "제조국",         val: 162,      unit: "개" },
              { label: "식품 SKU",       val: 172178,   unit: "개" },
              { label: "식품 수입 이력", val: 1045692,  unit: "건" },
            ].map(({ label, val, unit }) => (
              <div key={label} className="hero-kpi-item">
                <div className="hero-kpi-label">{label}</div>
                <div className="hero-kpi-num">
                  {Number(val).toLocaleString()}
                  <span className="hero-kpi-unit">{unit}</span>
                </div>
              </div>
            ))}
          </div>
        </div>
      </div>
      <div className="page">
        {uploadMsg && (
          <div className={uploadMsg.ok?"notice":"error-box"} style={{marginBottom:10}}>
            {uploadMsg.text}
          </div>
        )}

        <div className="card">
          <div className="sticky-panel-header" ref={stickyHeaderRef}>
          <div className="card-header">
            <div style={{display:"flex",alignItems:"center",gap:8}}>
              <span className="card-title">수입/OEM SKU 이력</span>
              <button
                className="icon-btn"
                onClick={()=>navigate("country-map")}
              >
                🌍 국가별로 보기
              </button>
            </div>
            <div style={{display:"flex",gap:8}}>
              <input
                type="file"
                accept=".xlsx,.xls"
                ref={fileRef}
                style={{display:"none"}}
                onChange={handleUpload}
              />

              <button
                className="upload-btn"
                disabled={uploading}
                onClick={()=>fileRef.current?.click()}
              >
                {uploading ? "업로드 중..." : "📤 Excel 업로드"}
              </button>

              <label
                className="upload-btn"
                style={{
                  background: "#0f766e",
                  cursor: uploading ? "not-allowed" : "pointer",
                  opacity: uploading ? 0.5 : 1,
                }}
              >
                {uploading ? "업로드 중..." : "📇 연락처 보강 업로드"}
                <input
                  type="file"
                  accept=".xlsx,.xls"
                  onChange={handleContactExcelUpload}
                  disabled={uploading}
                  style={{display:"none"}}
                />
              </label>

              <button
                className="upload-btn"
                style={{ background: "#dc2626" }}
                disabled={uploading}
                onClick={handleClearAllData}
              >
                🗑️ 전체 데이터 삭제
              </button>
            </div>
          </div>

          {/* 경쟁사 카드 */}
          <div style={{padding:"12px 14px 0"}}>
            <div className="competitor-cards">
              {["전체","코스트코","이마트","롯데마트","홈플러스","쿠팡"].map(name => {
                const theme = CARD_THEMES[name];
                const isActive = competitor === name;
                const FIXED_COUNTS = { "전체": 40658, "코스트코": 420, "이마트": 575, "롯데마트": 353, "홈플러스": 74, "쿠팡": 375 };
                const count = FIXED_COUNTS[name];
                return (
                  <button
                    key={name}
                    className={`comp-card${isActive ? " active" : ""}`}
                    style={{ background: isActive ? theme.active : theme.bg, borderColor: isActive ? theme.active : "#e2e8f0" }}
                    onClick={() => { setCompetitor(name); setPage(1); }}
                  >
                    <span className="comp-card-name" style={{color: isActive ? "#fff" : "#374151", fontSize:"15px"}}>{name}</span>
                    <span className="comp-card-num"  style={{color: isActive ? "#fff" : "#1a1a2e"}}>
                      {typeof count === "number" ? count.toLocaleString() : count}
                    </span>
                    <span className="comp-card-label">해외제조업체</span>
                  </button>
                );
              })}
            </div>
          </div>

          {/* 검색 툴바 */}
          <div className="toolbar">
            <div className="search-wrap">
              <span className="search-icon">🔍</span>
              <input placeholder="제품명, 해외제조업소, MC, 수입업체, 제조국 검색..." value={search} onChange={e=>setSearch(e.target.value)}/>
            </div>
            <div className="date-range-wrap">
              <input type="date" className="date-range-input" value={dateFrom} max={dateTo||undefined} onChange={e=>setDateFrom(e.target.value)}/>
              <span className="date-range-sep">~</span>
              <input type="date" className="date-range-input" value={dateTo} min={dateFrom||undefined} onChange={e=>setDateTo(e.target.value)}/>
              {(dateFrom||dateTo) && (
                <button className="date-range-clear" onClick={()=>{setDateFrom("");setDateTo("");}} title="기간 필터 해제">✕</button>
              )}
            </div>
            <span className="count-label">{meta?`총 ${meta.total.toLocaleString()}건 중 표시`:""}</span>
            <button className="icon-btn" onClick={()=>downloadCSV(data,"sku_history.csv")}>⬇ CSV</button>
            <div className="col-wrap" ref={colMenuRef}>
              <button className="icon-btn" onClick={()=>setShowColMenu(v=>!v)}>⚙ 열 설정</button>
              {showColMenu&&(
                <div className="col-dropdown">
                  {ALL_COLS.map(c=>(
                    <label key={c.key} className="col-item">
                      <input type="checkbox" checked={visibleCols.includes(c.key)}
                        onChange={e=>setVisibleCols(prev=>e.target.checked?[...prev,c.key]:prev.filter(k=>k!==c.key))}/>
                      {c.label}
                    </label>
                  ))}
                </div>
              )}
            </div>
          </div>
          </div>

          {error&&<div className="error-box">오류: {error}</div>}

          {/* 테이블: 패널 헤더 아래 남은 뷰포트 영역에서 자체적으로 상하/좌우 스크롤 */}
          <div className="table-wrap" style={{overflow:"auto", maxHeight: tableMaxHeight ? `${tableMaxHeight}px` : undefined}}>
            <table>
              <colgroup>
                {cols.map(c=><col key={c.key} style={c.key==="email" ? undefined : {width:c.w}}/>)}
              </colgroup>
              <thead>
                <tr>
                  {cols.map(c=>(
                    <th key={c.key} className={["import_count","count_year3","count_year2","count_year1"].includes(c.key) ? "col-highlight" : undefined} style={{position:"sticky", top:0, zIndex:30}}>
                      <div className="th-inner">
                        <span className="th-label">{c.label}</span>
                        <ColumnFilter
                          colKey={c.filterKey || null}
                          isNumeric={!!c.isNumeric}
                          activeValues={c.filterKey ? (colFilters[c.filterKey] || null) : null}
                          activeSortCol={sortBy === c.key}
                          activeSortDir={sortDir}
                          onSort={dir => { setSortBy(c.key); setSortDir(dir); setPage(1); }}
                          onApply={vals => {
                            if (c.filterKey) {
                              setColFilters(prev => ({ ...prev, [c.filterKey]: vals }));
                              setPage(1);
                            }
                          }}
                        />
                      </div>
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {loading ? <SkeletonRows cols={cols.length}/>
                : data.length===0
                  ? <tr><td colSpan={cols.length}><div className="empty-state">조건에 맞는 SKU 이력이 없습니다.</div></td></tr>
                  : data.map((row,i)=>(
                    <tr key={i}>
                      {cols.map(c=>(
                        <td key={c.key} title={c.key==="factory"?undefined:row[c.key]}
                          className={["import_count","count_year3","count_year2","count_year1"].includes(c.key) ? "col-highlight" : undefined}
                          style={
                            c.key==="factory" ? {maxWidth:"none", overflow:"visible"}
                            : c.key==="import_type" ? {padding:"8px 4px", textAlign:"center"}
                            : c.key==="email" ? {maxWidth:"none"}
                            : c.key==="sku_name"
                            ? {maxWidth:"none", overflow:"visible", whiteSpace: expandedOverflow.has(cellKey("sku_name",i)) ? "normal" : "nowrap"}
                            : ["category","mc","importer","country"].includes(c.key)
                            ? {maxWidth:"none", overflow:"visible", whiteSpace: expandedCells.has(cellKey(c.key,i)) ? "normal" : "nowrap"}
                            : undefined
                          }>

                          {c.clickable==="sku"
                            ? (
                              <div className="sku-cell">
                                <span
                                  ref={el=>overflowTextRef(el,"sku_name",i)}
                                  className="link-cell sku-cell-text"
                                  style={expandedOverflow.has(cellKey("sku_name",i)) ? {whiteSpace:"normal",wordBreak:"break-all"} : undefined}
                                  onClick={()=>navigate("sku",{row})}
                                  title={row[c.key]}
                                >
                                  {row[c.key]}
                                </span>
                                {(overflowCells.has(cellKey("sku_name",i)) || expandedOverflow.has(cellKey("sku_name",i))) && (
                                  <button
                                    className="sku-expand-btn"
                                    onClick={(e)=>{e.stopPropagation();toggleOverflowExpand("sku_name",i);}}
                                    title={expandedOverflow.has(cellKey("sku_name",i))?"접기":"펼치기"}
                                  >
                                    {expandedOverflow.has(cellKey("sku_name",i))?"▲":"▼"}
                                  </button>
                                )}
                              </div>
                            )
                            : c.clickable==="mfr"
                            ? (
                              <div className="sku-cell">
                                <span
                                  ref={el=>overflowTextRef(el,"factory",i)}
                                  className="link-cell sku-cell-text"
                                  style={expandedOverflow.has(cellKey("factory",i)) ? {whiteSpace:"normal",wordBreak:"break-all"} : undefined}
                                  onClick={()=>navigate("mfr",{row,from:"main"})}
                                  title={row[c.key]}
                                >
                                  {row[c.key]}
                                </span>
                                {(overflowCells.has(cellKey("factory",i)) || expandedOverflow.has(cellKey("factory",i))) && (
                                  <button
                                    className="sku-expand-btn"
                                    onClick={(e)=>{e.stopPropagation();toggleOverflowExpand("factory",i);}}
                                    title={expandedOverflow.has(cellKey("factory",i))?"접기":"펼치기"}
                                  >
                                    {expandedOverflow.has(cellKey("factory",i))?"▲":"▼"}
                                  </button>
                                )}
                              </div>
                            )
                            : c.clickable==="country"
                            ? renderTrunc("country", row[c.key], 5, i, { navTo: ()=>navigate("country",{country:row[c.key]}) })
                            : c.key==="import_count"
                            ? (
                              <span className="count-cell-wrap">
                                <span className="badge b-count">{row[c.key]}</span>
                                <button className="sku-expand-btn" onClick={(e)=>{e.stopPropagation();openMonthlyModal(row);}} title="연도별/월별 보기">▼</button>
                              </span>
                            )
                            : c.isYearCount
                            ? <span style={{color: row[c.key]>0?"#15803d":"#9ca3af", fontWeight: row[c.key]>0?600:400}}>
                                {row[c.key]>0 ? row[c.key] : "-"}
                              </span>
                            : c.isMc
                            ? renderTrunc("mc", row[c.key], 5, i, { badgeClass:"b-mc" })
                            : c.key==="email"
                            ? <span className="email-cell">{row[c.key]||"-"}</span>
                            : c.key==="import_type"
                            ? <OemBadge value={row[c.key]}/>
                            : c.key==="category"
                            ? renderTrunc("category", row[c.key], 6, i, { badgeClass:"b-cat" })
                            : c.key==="importer"
                            ? renderTrunc("importer", row[c.key], 6, i)
                            : row[c.key]||"-"}
                        </td>
                      ))}
                    </tr>
                  ))}
              </tbody>
            </table>
          </div>
          <div ref={paginationRef}>
            <Pagination meta={meta} page={page} setPage={setPage}/>
          </div>
        </div>
      </div>

      {monthlyModal && (
        <div className="modal-overlay" onClick={()=>setMonthlyModal(null)}>
          <div className="modal-box" onClick={e=>e.stopPropagation()}>
            <div className="modal-header">
              <span className="modal-title">{monthlyModal.row.sku_name} — 연도별 / 월별 수입횟수</span>
              <button className="modal-close" onClick={()=>setMonthlyModal(null)}>✕</button>
            </div>
            <div className="modal-body">
              {monthlyModal.loading ? <div className="empty-state">불러오는 중...</div>
              : monthlyModal.error ? <div className="error-box">오류: {monthlyModal.error}</div>
              : (
                <>
                  <div className="modal-section-title">연도별 수입횟수</div>
                  {!monthlyModal.yearly.length ? <div className="empty-state">이력 없음</div> : (
                    <table className="monthly-table">
                      <tbody>
                        <tr>
                          <td className="monthly-table-label">연도</td>
                          {monthlyModal.yearly.map(y=><td key={y.year}>{y.year}</td>)}
                        </tr>
                        <tr>
                          <td className="monthly-table-label">수입횟수</td>
                          {monthlyModal.yearly.map(y=>
                            <td key={y.year} style={{color: y.count>0?"#15803d":"#9ca3af", fontWeight: y.count>0?600:400}}>{y.count}</td>
                          )}
                        </tr>
                      </tbody>
                    </table>
                  )}
                  <div className="modal-section-title" style={{marginTop:14}}>월별 수입횟수</div>
                  {!monthlyModal.monthly.length ? <div className="empty-state">이력 없음</div> : (
                    <div style={{overflowX:"auto"}}>
                      <table className="monthly-table">
                        <tbody>
                          <tr>
                            <td className="monthly-table-label">년/월</td>
                            {monthlyModal.monthly.map(mo=><td key={mo.month}>{mo.month}</td>)}
                          </tr>
                          <tr>
                            <td className="monthly-table-label">수입횟수</td>
                            {monthlyModal.monthly.map(mo=>
                              <td key={mo.month} style={{color: mo.count>0?"#15803d":"#9ca3af", fontWeight: mo.count>0?600:400}}>{mo.count}</td>
                            )}
                          </tr>
                        </tbody>
                      </table>
                    </div>
                  )}
                </>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════════════════
// PAGE 2: SKU 취급 제조사
// ═══════════════════════════════════════════════════════════════════════════════
function SkuManufacturers({ navigate, state }) {
  const { row } = state;
  const skuName = row.sku_name;
  const [res,          setRes]         = useState(null);
  const [loading,      setLoading]     = useState(true);
  const [error,        setError]       = useState(null);
  const [search,       setSearch]      = useState("");
  const [debSearch,    setDebSearch]   = useState("");
  const [countryF,     setCountryF]    = useState("");
  const [contactF,     setContactF]    = useState("");
  const [oemF,         setOemF]        = useState("");
  const [page,         setPage]        = useState(1);
  const [skuSort, setSkuSort] = useState("ranking_score");
  const [skuDir,  setSkuDir]  = useState("desc");

  function handleSkuSort(col) {
    if (skuSort === col) setSkuDir(d => d === "asc" ? "desc" : "asc");
    else { setSkuSort(col); setSkuDir("asc"); }
  }

  const sortedRows = useMemo(() => {
    if (!res?.data) return [];
    const numericCols = ["ranking_score"];
    return [...res.data].sort((a, b) => {
      if (numericCols.includes(skuSort)) {
        const av = a[skuSort] ?? 0, bv = b[skuSort] ?? 0;
        return skuDir === "asc" ? av - bv : bv - av;
      }
      let av, bv;
      if (skuSort === "sku_name")  { av = a.skus?.[0]||""; bv = b.skus?.[0]||""; }
      else if (skuSort === "factory")   { av = a.factory||""; bv = b.factory||""; }
      else if (skuSort === "country")   { av = a.country||""; bv = b.country||""; }
      else if (skuSort === "email")     { av = a.email||""; bv = b.email||""; }
      else if (skuSort === "oem")       { av = a.import_types?.join()||""; bv = b.import_types?.join()||""; }
      else if (skuSort === "importers") { av = a.importers?.[0]||""; bv = b.importers?.[0]||""; }
      else { av = ""; bv = ""; }
      return skuDir === "asc" ? av.localeCompare(bv) : bv.localeCompare(av);
    });
  }, [res, skuSort, skuDir]);

  useEffect(()=>{const t=setTimeout(()=>{setDebSearch(search);setPage(1);},400);return()=>clearTimeout(t);},[search]);

  useEffect(()=>{
    setLoading(true); setError(null);
    fetchSkuFactories(skuName,{
      search:debSearch,
      countryFilter:countryF||undefined,
      hasContact:contactF==="있음"?true:contactF==="없음"?false:undefined,
      oemPossible:oemF==="가능"?true:undefined,
      page,pageSize:50,
    }).then(setRes).catch(e=>setError(e.message)).finally(()=>setLoading(false));
  },[skuName,debSearch,countryF,contactF,oemF,page]);

  const countries = useMemo(()=>{
    if(!res)return[];
    return Array.from(new Set(res.data.map(r=>r.country).filter(Boolean))).sort();
  },[res]);

  const info = res?.sku_info;

  return (
    <div className="app">
      <style>{styles}</style>
      <div className="banner">
        <div className="banner-inner">
          <div className="banner-left">
            <h1>🌐 Global Factory Sourcing Database</h1>
            <p>수입식품정보 기반으로 구축한 해외 제조업체 · SKU · 수입/OEM 이력 통합 DB</p>
          </div>
        </div>
      </div>
      <div className="page">
        <button className="back-btn" onClick={()=>navigate("main")}>← 수입/OEM SKU 이력으로 돌아가기</button>
        <div className="page-header">
          <h2>선택 SKU 취급 제조사</h2>
          <div className="sub">해당 SKU를 취급한 해외 제조업체 후보를 비교합니다.</div>
        </div>
        <div className="sku-card">
          <div><div className="sku-field-label">제품명</div><div className="sku-field-value">{skuName}</div></div>
          <div><div className="sku-field-label">MC (카테고리)</div><div className="sku-field-value"><span className="badge b-mc">{info?.mc||row.mc||"-"}</span></div></div>
          <div><div className="sku-field-label">구분</div><div className="sku-field-value"><span className="badge b-cat">{info?.category||row.category||"-"}</span></div></div>
          <div style={{flex:1}}><div className="sku-field-label">수입업체</div><div className="sku-field-value" style={{fontSize:12}}>
            {(() => {
              const BIG5 = ["이마트","홈플러스","롯데마트","쿠팡","코스트코"];
              const all = info?.importers || (row.importer ? [row.importer] : []);
              const big = all.filter(i => BIG5.some(b => i.includes(b)));
              const rest = all.filter(i => !BIG5.some(b => i.includes(b)));
              return <>{big.map((i,j)=><span key={j} className="badge b-gray" style={{marginRight:3}}>{i}</span>)}{rest.length>0&&<span className="badge b-gray">외 {rest.length}개</span>}</>;
            })()}
          </div></div>
        </div>
        <div className="card">
          <div className="card-header"><span className="card-title">해당 SKU 취급 제조사 목록</span></div>
          <div className="toolbar">
            <div className="search-wrap">
              <span className="search-icon">🔍</span>
              <input placeholder="해외제조업소, 국가, 수입업체 검색..." value={search} onChange={e=>setSearch(e.target.value)}/>
            </div>
            <span className="count-label">{res?`${res.meta.total}개 제조사`:""}</span>
          </div>
          {error&&<div className="error-box">오류: {error}</div>}
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th style={{minWidth:200}} onClick={()=>handleSkuSort("sku_name")}>SKU <SortIcon col="sku_name" sortCol={skuSort} sortDir={skuDir}/></th>
                  <th style={{minWidth:160}} onClick={()=>handleSkuSort("importers")}>수입업체 <SortIcon col="importers" sortCol={skuSort} sortDir={skuDir}/></th>
                  <th style={{minWidth:90}}  onClick={()=>handleSkuSort("oem")}>OEM 여부 <SortIcon col="oem" sortCol={skuSort} sortDir={skuDir}/></th>
                  <th style={{minWidth:220}} onClick={()=>handleSkuSort("factory")}>제조업체 <SortIcon col="factory" sortCol={skuSort} sortDir={skuDir}/></th>
                  <th style={{minWidth:80}}  onClick={()=>handleSkuSort("country")}>제조국 <SortIcon col="country" sortCol={skuSort} sortDir={skuDir}/></th>
                  <th style={{minWidth:90}}  onClick={()=>handleSkuSort("ranking_score")}>종합점수 <SortIcon col="ranking_score" sortCol={skuSort} sortDir={skuDir}/></th>
                  <th style={{minWidth:220}}>탑5 유통사 거래 다양성</th>
                  <th style={{minWidth:120}}>국내 수입횟수</th>
                  <th style={{minWidth:220}}>최근 3개년 성장추세</th>
                  <th style={{minWidth:160}} onClick={()=>handleSkuSort("email")}>연락처 <SortIcon col="email" sortCol={skuSort} sortDir={skuDir}/></th>
                </tr>
              </thead>
              <tbody>
                {loading ? <SkeletonRows cols={10}/>
                : !sortedRows?.length
                  ? <tr><td colSpan={10}><div className="empty-state">선택한 SKU와 연결된 제조사 정보가 없습니다.</div></td></tr>
                  : sortedRows.map((g,i)=>(
                    <tr key={i}>
                      <td title={g.skus?.[0]}><span style={{fontSize:12}}>{g.skus?.[0]||"-"}</span></td>
                      <td>
                        <div style={{display:"flex",gap:3,flexWrap:"wrap"}}>
                          {(() => {
                            const BIG5 = ["이마트","홈플러스","롯데마트","쿠팡","코스트코"];
                            const big = g.importers?.filter(i => BIG5.some(b => i.includes(b))) || [];
                            const rest = g.importers?.filter(i => !BIG5.some(b => i.includes(b))) || [];
                            return <>
                              {big.map((imp,j)=><span key={j} className="badge b-gray">{imp}</span>)}
                              {rest.length>0&&<span className="badge b-gray">외 {rest.length}개</span>}
                            </>;
                          })()}
                        </div>
                      </td>
                      <td><OemBadge value={g.import_types?.join(", ")}/></td>
                      <td title={g.factory}>
                        <span className="link-cell" onClick={()=>navigate("mfr",{row:{manufacturer:g.manufacturer,factory:g.factory,...g},from:"sku",skuState:state})}>
                          {g.factory}
                        </span>
                      </td>
                      <td>{g.country||"-"}</td>
                      <td><span className="score-cell">{g.ranking_score!=null?`${g.ranking_score.toFixed(1)}점`:"-"}</span></td>
                      <td style={{maxWidth:"none",overflow:"visible"}}>
                        <div style={{display:"flex",alignItems:"center",gap:5}}>
                          <GradeBadge grade={g.top5_retailer_grade}/>
                          <span className="grade-evidence">
                            {g.top5_retailers_matched?.length ? g.top5_retailers_matched.join(", ") : "거래 없음"}
                          </span>
                        </div>
                      </td>
                      <td style={{maxWidth:"none",overflow:"visible"}}>
                        <div style={{display:"flex",alignItems:"center",gap:5}}>
                          <GradeBadge grade={g.import_count_grade}/>
                          <span className="grade-evidence">{g.total_import_count ?? 0}건</span>
                        </div>
                      </td>
                      <td style={{maxWidth:"none",overflow:"visible"}}>
                        <div style={{display:"flex",alignItems:"center",gap:5}}>
                          <GradeBadge grade={g.growth_trend_grade}/>
                          <span className="grade-evidence">
                            {g.growth_yearly?.length
                              ? g.growth_yearly.map(y=>y.count).join(" → ")
                              : "-"}
                          </span>
                        </div>
                      </td>
                      <td>
                        {g.email ? <a href={`mailto:${g.email}`} style={{color:"#1d4ed8",fontSize:12}}>{g.email}</a>
                          : g.homepage ? <a href={g.homepage} target="_blank" rel="noopener noreferrer" style={{color:"#1d4ed8",fontSize:12}}>{g.homepage}</a>
                          : <span style={{color:"#9ca3af",fontSize:12}}>-</span>}
                      </td>
                    </tr>
                  ))}
              </tbody>
            </table>
          </div>
          <Pagination meta={res?.meta} page={page} setPage={setPage}/>
        </div>
      </div>
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════════════════
// PAGE 3: 제조사 상세
// ═══════════════════════════════════════════════════════════════════════════════
function ManufacturerDetail({ navigate, state }) {
  const { row, from, skuState, countryState } = state;
  const manufacturer = row.manufacturer||row.factory||"";
  const factory      = row.factory||"";
  const [res,     setRes]     = useState(null);
  const [loading, setLoading] = useState(true);
  const [error,   setError]   = useState(null);
  const [skuOpen, setSkuOpen] = useState(true);
  const [contactForm, setContactForm] = useState({
    email: "",
    homepage: "",
    certificates: "",
  });
  const [contactSaving, setContactSaving] = useState(false);
  const [contactMsg, setContactMsg] = useState(null);

  useEffect(()=>{
    setLoading(true); setError(null);
    fetchManufacturerDetail(manufacturer,factory)
      .then(setRes).catch(e=>setError(e.message)).finally(()=>setLoading(false));
  },[manufacturer,factory]);

  const d = res?.detail;

  useEffect(() => {
    if (!d) return;

    setContactForm({
      email: d.emails?.[0] || "",
      homepage: d.homepage || "",
      certificates: Array.isArray(d.certificates) ? d.certificates.join(", ") : "",
    });
    setContactMsg(null);
  }, [d]);

  const statusBadges = d ? [
    d.emails?.length       && {label:"연락 가능",       cls:"b-green"},
    d.oem_status==="OEM"   && {label:"OEM 이력 있음",   cls:"b-teal"},
    d.export_count>0       && {label:"한국 수출이력 있음",cls:"b-blue"},
    d.manager_mc           && {label:`담당: ${d.manager_mc}`, cls:"b-gray"},
  ].filter(Boolean) : [];

    async function handleSaveContact() {
    if (!d) {
      setContactMsg({ ok: false, text: "제조사 상세 정보를 먼저 불러와야 합니다." });
      return;
    }

    setContactSaving(true);
    setContactMsg(null);

    try {
      const saveRes = await updateManufacturerContact({
        factory,
        manufacturer,
        country: d.country || undefined,
        email: contactForm.email,
        homepage: contactForm.homepage,
        certificates: contactForm.certificates,
      });

      const refreshed = await fetchManufacturerDetail(manufacturer, factory);
      setRes(refreshed);

      setContactMsg({
        ok: true,
        text: saveRes.message || "연락처 저장 완료",
      });
    } catch (err) {
      setContactMsg({
        ok: false,
        text: err.message || "연락처 저장 실패",
      });
    } finally {
      setContactSaving(false);
    }
  }
  return (
    <div className="app">
      <style>{styles}</style>
      <div className="banner">
        <div className="banner-inner">
          <div className="banner-left">
            <h1>🌐 Global Factory Sourcing Database</h1>
            <p>수입식품정보 기반으로 구축한 해외 제조업체 · SKU · 수입/OEM 이력 통합 DB</p>
          </div>
        </div>
      </div>
      <div className="page">
        <button className="back-btn" onClick={()=>from==="sku"?navigate("sku",skuState):from==="country"?navigate("country",countryState):navigate("main")}>
          ← {from==="sku"?"선택 SKU 취급 제조사로 돌아가기":from==="country"?"국가별 상세로 돌아가기":"수입/OEM SKU 이력으로 돌아가기"}
        </button>

        {error&&<div className="error-box">오류: {error}</div>}

        <div className="mfr-header">
          {loading ? (
            <>
              <div className="skeleton" style={{width:"30%",height:11,marginBottom:8}}/>
              <div className="skeleton" style={{width:"75%",height:22,marginBottom:6}}/>
              <div className="skeleton" style={{width:"55%",height:13}}/>
            </>
          ) : d ? (
            <>
              <div className="mfr-country">📍 {d.country||"-"}</div>
              <div className="mfr-name">{factory}</div>
              <div className="mfr-factory">{d.location||""}</div>
              <div className="badge-row">{statusBadges.map((b,i)=><span key={i} className={`badge ${b.cls}`}>{b.label}</span>)}</div>
            </>
          ):null}
        </div>

        {d&&(
          <>
            <div className="detail-grid">
              {/* 연락처 */}
              <div className="card">
                <div className="card-header"><span className="card-title">📧 연락처 정보</span></div>
                <div className="card-body">
                  <div className="detail-row">
                    <div className="dk">이메일</div>
                    <div className="dv">
                      {d.emails?.length
                        ? d.emails.map((e,i)=><div key={i}><a href={`mailto:${e}`} className="detail-link">{e}</a></div>)
                        : <span style={{color:"#9ca3af"}}>-</span>}
                    </div>
                  </div>
                  <div className="detail-row">
                    <div className="dk">홈페이지</div>
                    <div className="dv">
                      {d.homepage ? <a href={d.homepage} target="_blank" rel="noopener noreferrer" className="detail-link">{d.homepage}</a> : "-"}
                    </div>
                  </div>
                  <div className="detail-row">
                    <div className="dk">연락 상태</div>
                    <div className="dv">{d.emails?.length ? <span className="badge b-green">연락처 확보</span> : <span className="badge b-gray">미확보</span>}</div>
                  </div>
                      <div className="contact-edit-box">
      <div className="contact-edit-title">연락처 직접 입력</div>

      <div className="contact-edit-grid">
        <div className="contact-edit-field">
          <label>이메일</label>
          <input
            type="text"
            value={contactForm.email}
            onChange={(e) =>
              setContactForm((prev) => ({
                ...prev,
                email: e.target.value,
              }))
            }
            placeholder="예: contact@company.com"
          />
        </div>

        <div className="contact-edit-field">
          <label>홈페이지</label>
          <input
            type="text"
            value={contactForm.homepage}
            onChange={(e) =>
              setContactForm((prev) => ({
                ...prev,
                homepage: e.target.value,
              }))
            }
            placeholder="예: https://www.company.com"
          />
        </div>

        <div className="contact-edit-field">
          <label>인증서</label>
          <input
            type="text"
            value={contactForm.certificates}
            onChange={(e) =>
              setContactForm((prev) => ({
                ...prev,
                certificates: e.target.value,
              }))
            }
            placeholder="예: HACCP, BRC, ISO22000"
          />
        </div>
      </div>

      <button
        type="button"
        className="upload-btn"
        onClick={handleSaveContact}
        disabled={contactSaving}
        style={{ marginTop: 10 }}
      >
        {contactSaving ? "저장 중..." : "연락처 저장"}
      </button>

      {contactMsg && (
        <div className={`contact-msg ${contactMsg.ok ? "ok" : "fail"}`}>
          {contactMsg.text}
        </div>
      )}
    </div>
                </div>
              </div>

              {/* 상품 정보 */}
              <div className="card">
                <div className="card-header"><span className="card-title">🛒 상품 정보</span></div>
                <div className="card-body">
                  <div className="detail-row">
                    <div className="dk">MC (카테고리)</div>
                    <div className="dv">{d.mc_list?.map((m,i)=><span key={i} className="badge b-mc">{m}</span>)}</div>
                  </div>
                  <div className="detail-row">
                    <div className="dk">구분</div>
                    <div className="dv">
                      {Array.from(new Set(res?.skus?.map(s=>s.category).filter(Boolean)||[])).map((c,i)=>(
                        <span key={i} className="badge b-cat">{c}</span>
                      ))}
                    </div>
                  </div>
                  <div className="detail-row">
                    <div className="dk">제조국</div>
                    <div className="dv">{d.country||"-"}</div>
                  </div>
                  <div className="detail-row">
                    <div className="dk">주요 제품</div>
                    <div className="dv" style={{fontSize:12}}>
                      {Array.from(new Set(res?.skus?.map(s=>s.sku_name)||[])).slice(0,5).join(", ")}
                      {(res?.skus?.length||0)>5&&` 외 ${(res?.skus?.length||0)-5}건`}
                    </div>
                  </div>
                </div>
              </div>

              {/* OEM/소싱 */}
              <div className="card">
                <div className="card-header"><span className="card-title">🏭 OEM / 소싱 정보</span></div>
                <div className="card-body">
                  <div className="detail-row">
                    <div className="dk">OEM 이력</div>
                    <div className="dv">
                      {d.oem_status==="OEM"
                        ? <span className="badge b-green">OEM 이력 있음</span>
                        : <span className="badge b-gray">수입만 있음</span>}
                    </div>
                  </div>
                  <div className="detail-row">
                    <div className="dk">거래 수입업체</div>
                    <div className="dv">
                      <div style={{display:"flex",gap:4,flexWrap:"wrap"}}>
                        {d.importers?.slice(0,5).map((imp,i)=><span key={i} className="badge b-gray">{imp}</span>)}
                        {(d.importers?.length||0)>5&&<span className="badge b-gray">+{d.importers.length-5}</span>}
                      </div>
                    </div>
                  </div>
                  <div className="detail-row">
                    <div className="dk">한국 수출이력</div>
                    <div className="dv"><span className="badge b-count">{d.export_count}건</span></div>
                  </div>
                  <div className="detail-row">
                    <div className="dk">최근 수입일</div>
                    <div className="dv">{d.latest_import||"-"}</div>
                  </div>
                </div>
              </div>

              {/* 인증서 */}
              <div className="card">
                <div className="card-header"><span className="card-title">✅ 인증서</span></div>
                <div className="card-body">
                  <div style={{display:"flex",flexWrap:"wrap",gap:5}}>
                    {d.certificates?.length
                      ? d.certificates.map((c,i)=><span key={i} className="badge b-teal">{c}</span>)
                      : <span style={{color:"#9ca3af",fontSize:12}}>인증 정보 없음 (추후 업데이트 예정)</span>}
                  </div>
                </div>
              </div>
            </div>

            {/* 취급 SKU 목록 */}
            <div className="card" style={{marginTop:12}}>
              <button className="acc-toggle" onClick={()=>setSkuOpen(v=>!v)} aria-expanded={skuOpen}>
                <span>📦 취급 제품 목록 ({res?.skus?.length||0}건)</span>
                <span>{skuOpen?"▲":"▼"}</span>
              </button>
              {skuOpen&&(
                <div className="table-wrap">
                  <table>
                    <thead>
                      <tr>
                        <th style={{minWidth:240}}>제품명</th>
                        <th style={{minWidth:120}}>MC (카테고리)</th>
                        <th style={{minWidth:90}}>구분</th>
                        <th style={{minWidth:150}}>수입업체</th>
                        <th style={{minWidth:70}}>수입횟수</th>
                      </tr>
                    </thead>
                    <tbody>
                      {res?.skus?.map((r,i)=>(
                        <tr key={i}>
                          <td title={r.sku_name}>
                            <span className="link-cell" onClick={()=>navigate("sku",{row:{sku_name:r.sku_name,mc:r.mc,category:r.category,importer:r.importer}})}>
                              {r.sku_name}
                            </span>
                          </td>
                          <td><span className="badge b-mc">{r.mc||"-"}</span></td>
                          <td><span className="badge b-cat">{r.category||"-"}</span></td>
                          <td style={{fontSize:12}} title={r.importer}>{r.importer||"-"}</td>
                          <td><span className="badge b-count">{r.import_count}</span></td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          </>
        )}
      </div>
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════════════════
// PAGE 4: 국가별 상세
// ═══════════════════════════════════════════════════════════════════════════════
const PIE_COLORS = ["#2563eb","#16a34a","#f59e0b","#dc2626","#7c3aed","#0891b2","#db2777","#65a30d","#ea580c","#4338ca"];

function polarToCartesian(cx, cy, r, angleDeg) {
  const rad = (angleDeg - 90) * Math.PI / 180;
  return { x: cx + r * Math.cos(rad), y: cy + r * Math.sin(rad) };
}

function pieSlicePath(cx, cy, r, startAngle, endAngle) {
  const start = polarToCartesian(cx, cy, r, startAngle);
  const end   = polarToCartesian(cx, cy, r, endAngle);
  const largeArc = endAngle - startAngle <= 180 ? 0 : 1;
  return `M ${cx} ${cy} L ${start.x} ${start.y} A ${r} ${r} 0 ${largeArc} 1 ${end.x} ${end.y} Z`;
}

function PieChart({ slices, size = 120 }) {
  const r = size / 2;
  const total = slices.reduce((s, x) => s + (x.value || 0), 0);
  if (!total) {
    return (
      <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`}>
        <circle cx={r} cy={r} r={r - 1} fill="#f3f4f6" stroke="#e5e7eb" />
      </svg>
    );
  }
  let angle = 0;
  const paths = slices.map((s, i) => {
    const portion = (s.value || 0) / total;
    const startAngle = angle;
    const endAngle = angle + portion * 360;
    angle = endAngle;
    return (
      <path
        key={i}
        d={pieSlicePath(r, r, r - 1, startAngle, endAngle)}
        fill={s.color}
        onClick={s.onClick}
        style={{ cursor: s.onClick ? "pointer" : "default" }}
      />
    );
  });
  return <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`}>{paths}</svg>;
}

function CountryDetail({ navigate, state }) {
  const country = state?.country;
  const [summary,   setSummary]   = useState(null);
  const [topItems,  setTopItems]  = useState(null);
  const [amountShare, setAmountShare] = useState(null);
  const [sumLoading, setSumLoading] = useState(true);
  const [topLoading, setTopLoading] = useState(true);
  const [error,     setError]     = useState(null);

  const [search,    setSearch]    = useState("");
  const [debSearch, setDebSearch] = useState("");
  const [mcFilter,  setMcFilter]  = useState("");
  const [page,      setPage]      = useState(1);
  const [sortBy,    setSortBy]    = useState(null);
  const [sortDir,   setSortDir]   = useState("desc");

  const [mfrRes,     setMfrRes]     = useState(null);
  const [mfrLoading, setMfrLoading] = useState(true);

  useEffect(()=>{const t=setTimeout(()=>{setDebSearch(search);setPage(1);},400);return()=>clearTimeout(t);},[search]);

  useEffect(()=>{
    if (!country) return;
    setSumLoading(true); setError(null);
    fetchCountrySummary(country).then(setSummary).catch(e=>setError(e.message)).finally(()=>setSumLoading(false));
    setTopLoading(true);
    fetchCountryTopItems(country).then(setTopItems).catch(e=>setError(e.message)).finally(()=>setTopLoading(false));
    fetchCountryAmountShare().then(setAmountShare).catch(e=>setError(e.message));
  },[country]);

  useEffect(()=>{
    if (!country) return;
    setMfrLoading(true);
    fetchCountryManufacturers(country, {
      mc: mcFilter || undefined,
      query: debSearch || undefined,
      sortBy: sortBy || undefined,
      sortOrder: sortDir,
      page, pageSize: 20,
    }).then(setMfrRes).catch(e=>setError(e.message)).finally(()=>setMfrLoading(false));
  },[country,mcFilter,debSearch,sortBy,sortDir,page]);

  function handleSort(col) {
    if (sortBy === col) setSortDir(d => d === "asc" ? "desc" : "asc");
    else { setSortBy(col); setSortDir("desc"); }
    setPage(1);
  }

  function resetFilters() {
    setSearch(""); setDebSearch(""); setMcFilter(""); setPage(1);
  }

  const activeFilters = [];
  if (debSearch) activeFilters.push(`검색: "${debSearch}"`);
  if (mcFilter)  activeFilters.push(`MC: ${mcFilter}`);

  if (!country) {
    return (
      <div className="app">
        <style>{styles}</style>
        <div className="page">
          <div className="empty-state">국가 정보가 없습니다.</div>
          <button className="back-btn" onClick={()=>navigate("main")}>← 돌아가기</button>
        </div>
      </div>
    );
  }

  return (
    <div className="app">
      <style>{styles}</style>
      <div className="banner">
        <div className="banner-inner">
          <div className="banner-left">
            <h1>🌐 Global Factory Sourcing Database</h1>
            <p>수입식품정보 기반으로 구축한 해외 제조업체 · SKU · 수입/OEM 이력 통합 DB</p>
          </div>
        </div>
      </div>
      <div className="page">
        <button className="back-btn" onClick={()=>navigate("main")}>← 수입/OEM SKU 이력으로 돌아가기</button>

        {error && <div className="error-box">오류: {error}</div>}

        <div className="card country-header-card">
          {sumLoading ? (
            <>
              <div className="skeleton" style={{width:"30%",height:22,marginBottom:8}}/>
              <div className="skeleton" style={{width:"55%",height:13}}/>
            </>
          ) : summary ? (
            <div className="country-header-row">
              <div className="country-title-block">
                <div className="country-title">{summary.flag} {summary.country}</div>
                {summary.has_amount_stats ? (
                  <div className="country-rank-line">
                    대한민국 수입금액 기준 국가 순위 <strong>{summary.amount_rank}위</strong>
                    {" "}(비중 {summary.amount_share_pct}%)
                  </div>
                ) : (
                  <div className="country-rank-line muted">수입금액 통계 없음</div>
                )}
                <div className="country-stat-row">
                  <span className="badge b-gray">제조사 {summary.manufacturer_count}개</span>
                  <span className="badge b-gray">수입이력 {summary.total_import_count}건</span>
                </div>
              </div>
              {amountShare?.items?.length > 0 && (
                <div className="country-amount-share-grid">
                  {amountShare.items.map((it,i)=>(
                    <div
                      key={it.country}
                      className="country-amount-share-item"
                      style={{cursor: it.is_other ? "default" : "pointer", fontWeight: it.country===summary.country?700:400}}
                      onClick={it.is_other ? undefined : () => navigate("country", { country: it.country })}
                    >
                      <span className="legend-dot" style={{background: it.is_other ? "#e5e7eb" : PIE_COLORS[i % PIE_COLORS.length]}}/>
                      {it.flag} {it.country} ({it.pct}%)
                    </div>
                  ))}
                </div>
              )}
            </div>
          ) : null}
        </div>

        {!topLoading && topItems?.items?.length > 0 && (() => {
          const topPctSum = topItems.items.reduce((s,it)=>s+(it.pct||0), 0);
          const otherPct = Math.max(100 - topPctSum, 0);
          const legendItems = otherPct > 0.005
            ? [...topItems.items, { rank: null, name: "기타", pct: Math.round(otherPct*100)/100, isOther: true }]
            : topItems.items;
          return (
            <div className="card" style={{marginTop:12}}>
              <div className="card-header"><span className="card-title">📊 국가별 주요 수입품목 TOP10</span></div>
              <div className="card-body country-top-items">
                <PieChart slices={legendItems.map((it,i)=>({value:it.pct, color: it.isOther ? "#e5e7eb" : PIE_COLORS[i % PIE_COLORS.length]}))} size={140}/>
                <div className="country-pie-legend country-top-legend">
                  {legendItems.map((it,i)=>(
                    <div key={i}>
                      <span className="legend-dot" style={{background: it.isOther ? "#e5e7eb" : PIE_COLORS[i % PIE_COLORS.length]}}/>
                      {it.isOther ? "기타" : `${it.rank}. ${it.name}`} ({it.pct}%)
                    </div>
                  ))}
                </div>
              </div>
            </div>
          );
        })()}

        <div className="card" style={{marginTop:12}}>
          <div className="card-header"><span className="card-title">해당 국가 제조사 목록</span></div>
          <div className="toolbar">
            <div className="search-wrap">
              <span className="search-icon">🔍</span>
              <input placeholder="SKU명 또는 MC명을 검색하세요" value={search} onChange={e=>setSearch(e.target.value)}/>
            </div>
            <span className="count-label">{mfrRes?`${mfrRes.meta.total}개 제조사`:""}</span>
          </div>
          {activeFilters.length > 0 && (
            <div className="filter-bar">
              <span className="filter-label">적용된 필터: {activeFilters.join(" / ")}</span>
              <button className="icon-btn" onClick={resetFilters}>필터 초기화</button>
            </div>
          )}
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th style={{minWidth:40}}>순위</th>
                  <th style={{minWidth:200}}>제조사명</th>
                  <th style={{minWidth:80}}>제조국</th>
                  <th style={{minWidth:160}}>주요 MC</th>
                  <th style={{minWidth:80}} onClick={()=>handleSort("sku_count")}>취급 SKU 수 <SortIcon col="sku_count" sortCol={sortBy} sortDir={sortDir}/></th>
                  <th style={{minWidth:90}} onClick={()=>handleSort("total_import_count")}>총수입횟수 <SortIcon col="total_import_count" sortCol={sortBy} sortDir={sortDir}/></th>
                  <th style={{minWidth:100}} onClick={()=>handleSort("top5_count")}>탑5 거래 유통사 수 <SortIcon col="top5_count" sortCol={sortBy} sortDir={sortDir}/></th>
                  <th style={{minWidth:90}} onClick={()=>handleSort("latest_import")}>최근 수입일 <SortIcon col="latest_import" sortCol={sortBy} sortDir={sortDir}/></th>
                  <th style={{minWidth:90}} onClick={()=>handleSort("ranking_score")}>제조사 점수 <SortIcon col="ranking_score" sortCol={sortBy} sortDir={sortDir}/></th>
                </tr>
              </thead>
              <tbody>
                {mfrLoading ? <SkeletonRows cols={9}/>
                : !mfrRes?.data?.length
                  ? <tr><td colSpan={9}><div className="empty-state">조건에 맞는 제조사가 없습니다.</div></td></tr>
                  : mfrRes.data.map((m,i)=>(
                    <tr key={i}>
                      <td>{m.rank}</td>
                      <td title={m.manufacturer}>
                        <span className="link-cell" onClick={()=>navigate("mfr",{row:{manufacturer:m.manufacturer,factory:m.factory||m.manufacturer},from:"country",countryState:state})}>
                          {m.manufacturer}
                        </span>
                      </td>
                      <td>{m.country||"-"}</td>
                      <td>{m.primary_mc ? <span className="badge b-mc">{m.primary_mc}</span> : "-"}</td>
                      <td>{m.sku_count}</td>
                      <td>{m.total_import_count}</td>
                      <td>{m.top5_count}</td>
                      <td>{m.latest_import||"-"}</td>
                      <td><span className="score-cell">{m.ranking_score!=null?`${m.ranking_score.toFixed(1)}점`:"-"}</span></td>
                    </tr>
                  ))}
              </tbody>
            </table>
          </div>
          <Pagination meta={mfrRes?.meta} page={page} setPage={setPage}/>
        </div>
      </div>
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════════════════
// 국가별 지도 보기
// ═══════════════════════════════════════════════════════════════════════════════
function CountryMapPage({ navigate }) {
  const [dbCountries, setDbCountries] = useState(null);
  const [hovered, setHovered] = useState(null); // { name, x, y, inDb }
  const [search,  setSearch]  = useState("");
  const [showSuggest, setShowSuggest] = useState(false);
  const [countryInfoCache, setCountryInfoCache] = useState({}); // { [koreanName]: { summary, topItems } }

  function loadCountryInfo(koreanName) {
    if (!koreanName || countryInfoCache[koreanName]) return;
    setCountryInfoCache(prev => ({ ...prev, [koreanName]: {} }));
    Promise.all([
      fetchCountrySummary(koreanName).catch(() => null),
      fetchCountryTopItems(koreanName).catch(() => null),
    ]).then(([summary, topItems]) => {
      setCountryInfoCache(prev => ({ ...prev, [koreanName]: { summary, topItems } }));
    });
  }

  useEffect(() => {
    fetchColumnValues("country").then(vals => setDbCountries(new Set(vals)));
  }, []);

  const dbCountryList = useMemo(() => dbCountries ? [...dbCountries].sort() : [], [dbCountries]);
  const suggestions = useMemo(() => {
    if (!search.trim()) return [];
    const q = search.trim().toLowerCase();
    return dbCountryList.filter(c => c.toLowerCase().includes(q)).slice(0, 8);
  }, [search, dbCountryList]);

  function goToCountry(koreanName) {
    if (!koreanName || !dbCountries || !dbCountries.has(koreanName)) return;
    navigate("country", { country: koreanName });
  }

  return (
    <div className="app">
      <style>{styles}</style>
      <div className="banner">
        <div className="banner-inner">
          <div className="banner-left">
            <h1>🌐 Global Factory Sourcing Database</h1>
            <p>수입식품정보 기반으로 구축한 해외 제조업체 · SKU · 수입/OEM 이력 통합 DB</p>
          </div>
        </div>
      </div>
      <div className="page">
        <button className="back-btn" onClick={()=>navigate("main")}>← 수입/OEM SKU 이력으로 돌아가기</button>

        <div className="card" style={{padding:16}}>
          <div className="page-header" style={{display:"flex",alignItems:"center",justifyContent:"space-between",gap:12,flexWrap:"wrap"}}>
            <h2>국가별로 보기</h2>
            <div style={{position:"relative", width:260}}>
              <input
                className="date-range-input"
                style={{width:"100%"}}
                placeholder="국가명 검색 (예: 미국, 베트남...)"
                value={search}
                onChange={e=>{ setSearch(e.target.value); setShowSuggest(true); }}
                onFocus={()=>setShowSuggest(true)}
                onKeyDown={e=>{ if (e.key==="Enter" && suggestions.length>0) goToCountry(suggestions[0]); }}
              />
              {showSuggest && suggestions.length > 0 && (
                <div className="filter-dropdown" style={{minWidth:"100%", maxWidth:"100%"}}>
                  <div className="filter-list" style={{maxHeight:220}}>
                    {suggestions.map(c => (
                      <div key={c} className="filter-item" style={{justifyContent:"flex-start"}}
                        onClick={()=>{ setShowSuggest(false); setSearch(""); goToCountry(c); }}>
                        <span>{c}</span>
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </div>
          </div>

          <p style={{fontSize:12, color:"#6b7280", marginBottom:10}}>
            지도 위에서 마우스를 올리면 국가명이 표시됩니다. 데이터베이스에 보유한 국가(초록색)를 클릭하면 해당 국가의 상세 페이지로 이동합니다.
          </p>

          <div style={{position:"relative", border:"1px solid #e8eaed", borderRadius:8, overflow:"hidden", background:"#eff6ff"}}
            onMouseLeave={()=>setHovered(null)}>
            <ComposableMap projectionConfig={{ scale: 148 }} width={980} height={460} style={{width:"100%",height:"auto",display:"block"}}>
              <Geographies geography={worldGeoData}>
                {({ geographies }) =>
                  geographies.map(geo => {
                    const koreanName = getKoreanName(geo.properties.name);
                    const inDb = !!(koreanName && dbCountries && dbCountries.has(koreanName));
                    return (
                      <Geography
                        key={geo.rsmKey}
                        geography={geo}
                        onMouseMove={e => setHovered({
                          name: koreanName || geo.properties.name,
                          inDb,
                          x: e.clientX, y: e.clientY,
                        })}
                        onMouseEnter={e => {
                          setHovered({
                            name: koreanName || geo.properties.name,
                            inDb,
                            x: e.clientX, y: e.clientY,
                          });
                          if (inDb) loadCountryInfo(koreanName);
                        }}
                        onClick={() => goToCountry(koreanName)}
                        style={{
                          default: {
                            fill: inDb ? "#16a34a" : "#cbd5e1",
                            stroke: "#fff", strokeWidth: 0.5,
                            outline: "none", cursor: inDb ? "pointer" : "default",
                            transition: "fill .1s",
                          },
                          hover: {
                            fill: inDb ? "#15803d" : "#94a3b8",
                            stroke: "#fff", strokeWidth: 0.5, outline: "none",
                            cursor: inDb ? "pointer" : "default",
                          },
                          pressed: { fill: "#166534", stroke: "#fff", strokeWidth: 0.5, outline: "none" },
                        }}
                      />
                    );
                  })
                }
              </Geographies>
            </ComposableMap>

            {hovered && (() => {
              const info = hovered.inDb ? countryInfoCache[hovered.name] : null;
              const summary = info?.summary;
              const topItems = info?.topItems?.items;
              return (
                <div style={{
                  position:"fixed", left: hovered.x + 12, top: hovered.y + 12, zIndex: 300,
                  background:"#1a1a2e", color:"#fff", padding:"8px 12px", borderRadius:6,
                  fontSize:12, pointerEvents:"none", whiteSpace:"normal",
                  width:380, lineHeight:1.5,
                }}>
                  <div style={{fontWeight:600, marginBottom: hovered.inDb ? 4 : 0}}>
                    {hovered.name}{hovered.inDb ? "" : " (데이터 없음)"}
                  </div>
                  {hovered.inDb && summary?.has_amount_stats && (
                    <div style={{color:"#cbd5e1", marginBottom: topItems?.length ? 4 : 0}}>
                      대한민국 수입금액 기준 국가 순위 {summary.amount_rank}위 (비중 {summary.amount_share_pct}%)
                    </div>
                  )}
                  {hovered.inDb && topItems?.length > 0 && (
                    <div>
                      {topItems.map((it, i) => (
                        <div key={i}>{it.rank}. {it.name} ({it.pct}%)</div>
                      ))}
                    </div>
                  )}
                </div>
              );
            })()}
          </div>
        </div>
      </div>
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════════════════
// ROOT
// ═══════════════════════════════════════════════════════════════════════════════
export default function App() {
  const [page, setPage] = useState({ name:"main", state:null });
  function navigate(name, state) { setPage({name,state}); window.scrollTo({top:0,behavior:"smooth"}); }
  if (page.name==="sku") return <SkuManufacturers navigate={navigate} state={page.state}/>;
  if (page.name==="mfr") return <ManufacturerDetail navigate={navigate} state={page.state}/>;
  if (page.name==="country") return <CountryDetail navigate={navigate} state={page.state}/>;
  if (page.name==="country-map") return <CountryMapPage navigate={navigate}/>;
  return <MainDashboard navigate={navigate}/>;
}
