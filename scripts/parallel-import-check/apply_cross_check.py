# -*- coding: utf-8 -*-
"""cross_check_matched_cache.json(=build_cross_check_queue.py가 이미 재조회해둔 매칭
결과) + cross_check_decisions.json(사람이 판단한 keep/reject)을 반영해서 revised_status를
계산한다. API 재조회 없음. 1차 결과 파일(pilot_results.json 등)은 여기서도 절대 읽거나
쓰지 않는다 — orig_status는 이미 matched_cache 안에 캐시돼 있음."""
import json, sys, os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cross_check import aggregate_by_factory, judge

SCRATCH = os.path.dirname(os.path.abspath(__file__))

cache = json.load(open(f'{SCRATCH}\\cross_check_matched_cache.json', encoding='utf-8'))
decisions = json.load(open(f'{SCRATCH}\\cross_check_decisions.json', encoding='utf-8'))

STATUS_ORDER = {'no_history': 0, 'exclusive': 1, 'parallel_possible': 2}

results = []
changed = 0
pending = 0
for target_key, entry in cache.items():
    kept_ambiguous = []
    has_pending = False
    for row in entry['ambiguous']:
        dk = row['dedup_key']
        decision = decisions.get(dk)
        if decision is False:
            continue  # 반려 -> 매칭에서 제외
        if dk not in decisions:
            has_pending = True
        kept_ambiguous.append(row)
    if has_pending:
        pending += 1

    final_matched = entry['confirmed'] + kept_ambiguous
    groups = aggregate_by_factory(final_matched)
    verdicts = judge(groups)
    revised_status = 'parallel_possible' if any(v['importer_count'] >= 2 for v in verdicts) else (
        'exclusive' if verdicts else 'no_history'
    )

    orig_status = entry['orig_status']
    is_changed = revised_status != orig_status
    if is_changed:
        changed += 1

    results.append({
        'type': entry['type'], 'distributor': entry['distributor'], 'rank_col': entry['rank_col'],
        'brand_en': entry['brand_en'], 'target_en': entry['target_en'],
        'orig_status': orig_status, 'revised_status': revised_status,
        'changed': is_changed, 'has_pending_review': has_pending,
        'revised_verdicts': verdicts,
    })

print('총 대상:', len(results))
print('원래 판정과 달라진 건(revised_status != orig_status):', changed)
print('미검토(pending) 항목이 남아있는 대상:', pending)

for r in results:
    if r['changed']:
        print(f" - [{r['orig_status']} -> {r['revised_status']}] {r['type']} / {r['brand_en']} / {r['target_en'][:60]}")

with open(f'{SCRATCH}\\cross_check_results.json', 'w', encoding='utf-8') as f:
    json.dump(results, f, ensure_ascii=False, indent=1)
print('saved: cross_check_results.json')
