# -*- coding: utf-8 -*-
"""1차 결과 파일(들)에서 exclusive/parallel_possible 건만 골라 크로스체크 큐를 만든다.
1차 결과 파일 자체는 읽기만 하고 절대 수정하지 않는다."""
import json, os, sys
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cross_check import fetch_candidates, cross_check_verdict, aggregate_by_factory, judge, _row_key

SCRATCH = os.path.dirname(os.path.abspath(__file__))

SOURCE_FILES = sys.argv[1:] if len(sys.argv) > 1 else ['pilot_results.json']
OUT_QUEUE = f'{SCRATCH}\\cross_check_queue.json'
OUT_MISMATCH = f'{SCRATCH}\\cross_check_mismatches.json'

targets = []
for fname in SOURCE_FILES:
    data = json.load(open(f'{SCRATCH}\\{fname}', encoding='utf-8'))
    for r in data['results']:
        if r['status'] in ('exclusive', 'parallel_possible'):
            targets.append({**r, '_source_file': fname})

print(f'대상(exclusive/parallel_possible): {len(targets)}건 (원본 소스 {len(SOURCE_FILES)}개 파일, 읽기 전용)')

distinct_brands = sorted(set(t['brand_en'] for t in targets))
print('재조회할 distinct 브랜드:', len(distinct_brands))


def do_fetch(b):
    try:
        return b, fetch_candidates(b), None
    except Exception as e:
        return b, None, str(e)


cand_by_brand = {}
errors = []
with ThreadPoolExecutor(max_workers=8) as ex:
    futs = [ex.submit(do_fetch, b) for b in distinct_brands]
    for fut in as_completed(futs):
        b, c, e = fut.result()
        if e:
            errors.append(b)
        else:
            cand_by_brand[b] = c
for b in errors[:]:
    try:
        cand_by_brand[b] = fetch_candidates(b)
        errors.remove(b)
    except Exception:
        pass
print('fetch 에러 남은 것:', len(errors))

queue = []
mismatches = []
matched_cache = {}  # target_key -> {"confirmed": [...], "ambiguous": [...]} (apply 단계 재조회 방지용)
for t in targets:
    cands = cand_by_brand.get(t['brand_en'])
    if cands is None:
        mismatches.append({**t, '_cc_issue': 'refetch_failed'})
        continue

    cc = cross_check_verdict(t['target_en'], t['brand_en'], cands)

    # 1차와 동일 계산인지 확인 (원본 함수 재호출이므로 다르면 이상 신호)
    recomputed_groups = aggregate_by_factory(cc['all_matched'])
    recomputed_verdicts = judge(recomputed_groups)
    orig_factories = {v['factory'] for v in (t.get('verdicts') or [])}
    new_factories = {v['factory'] for v in recomputed_verdicts}
    if orig_factories != new_factories:
        mismatches.append({
            **t, '_cc_issue': 'factory_set_mismatch',
            '_orig_factories': sorted(orig_factories), '_recomputed_factories': sorted(new_factories),
        })

    target_key = f"{t['type']}|||{t['distributor']}|||{t['rank_col']}"
    matched_cache[target_key] = {
        'type': t['type'], 'distributor': t['distributor'], 'rank_col': t['rank_col'],
        'brand_en': t['brand_en'], 'target_en': t['target_en'], 'orig_status': t['status'],
        'confirmed': cc['confirmed'],
        'ambiguous': [{**row, 'dedup_key': f"{t['target_en']}|||{row.get('sku_name_en') or ''}"} for row in cc['ambiguous']],
    }

    for row in cc['ambiguous']:
        cand_en = row.get('sku_name_en') or ''
        dedup_key = f"{t['target_en']}|||{cand_en}"
        queue.append({
            'type': t['type'], 'distributor': t['distributor'], 'rank_col': t['rank_col'],
            'brand_en': t['brand_en'], 'target_en': t['target_en'],
            'orig_status': t['status'],
            'candidate_sku_name_en': row.get('sku_name_en'),
            'candidate_factory': row.get('factory'),
            'candidate_manufacturer': row.get('manufacturer'),
            'candidate_importer': row.get('importer'),
            'candidate_sku_name': row.get('sku_name'),
            'candidate_txn_date': row.get('txn_date'),
            'tier': row.get('_cc_tier'),
            'matched_via': row.get('_cc_matched_via'),
            'row_key': list(_row_key(row)),
            'dedup_key': dedup_key,
        })

print('애매한 매칭(검토 큐, 거래기록 단위):', len(queue))
print('불일치/이상 신호:', len(mismatches))

# 검토는 "브랜드+상품설명 조합" 단위로만 하면 되고, 같은 조합을 공유하는 거래기록들은
# 한 번의 판단으로 같이 적용된다(apply_cross_check.py에서 dedup_key로 매핑).
review_items = {}
for q in queue:
    k = q['dedup_key']
    if k not in review_items:
        review_items[k] = {
            'dedup_key': k, 'brand_en': q['brand_en'], 'target_en': q['target_en'],
            'candidate_sku_name_en': q['candidate_sku_name_en'],
            'candidate_factory': q['candidate_factory'],
            'candidate_manufacturer': q['candidate_manufacturer'],
            'tier': q['tier'], 'matched_via': q['matched_via'],
            'occurrence_count': 0,
        }
    review_items[k]['occurrence_count'] += 1

review_list = sorted(review_items.values(), key=lambda x: (x['brand_en'], x['target_en']))
print('실제 판단 필요(브랜드+상품설명 조합, dedup):', len(review_list))

with open(OUT_QUEUE, 'w', encoding='utf-8') as f:
    json.dump(queue, f, ensure_ascii=False, indent=1)
with open(OUT_MISMATCH, 'w', encoding='utf-8') as f:
    json.dump(mismatches, f, ensure_ascii=False, indent=1)
with open(f'{SCRATCH}\\cross_check_review_items.json', 'w', encoding='utf-8') as f:
    json.dump(review_list, f, ensure_ascii=False, indent=1)
with open(f'{SCRATCH}\\cross_check_matched_cache.json', 'w', encoding='utf-8') as f:
    json.dump(matched_cache, f, ensure_ascii=False, indent=1)

print('saved:', OUT_QUEUE, OUT_MISMATCH, f'{SCRATCH}\\cross_check_review_items.json',
      f'{SCRATCH}\\cross_check_matched_cache.json')
