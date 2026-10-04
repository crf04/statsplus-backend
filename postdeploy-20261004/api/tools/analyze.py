"""analyze.py: assert the #107 provenance contract on the saved production responses; writes ../analysis.json, prints PASS/FAIL lines."""
import json, os, sys, datetime
sys.path.insert(0, '/Users/chrisfu/statsplus-107/backend')
from app.services.collection_control import _SURFACE_REGISTRY_RAW
from app.services.database_first_activation import PUBLICATION_FRESHNESS_SECONDS
E = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RULE = {d['stream_key']: d['freshness'] for d in _SURFACE_REGISTRY_RAW}
UNSUP = {d['stream_key'] for d in _SURFACE_REGISTRY_RAW if d.get('reason') == 'provider_window_unsupported'}
KEYS = ['stream_key', 'publication_id', 'season', 'coverage_cutoff', 'version', 'status', 'freshness', 'age_seconds', 'source',
        'legacy_fallback_allowed', 'payload_checksum', 'retrieved_at', 'fence', 'unavailable_reason', 'manifest_id',
        'event_catalog_publication_id', 'event_catalog_checksum']
SOURCES = {'slate': ['injuries', 'pool', 'schedule'], 'matchup': ['injuries', 'pool', 'schedule'], 'unscheduled': [],
           'game-logs': [], 'targets-resolve': ['injuries', 'pool', 'schedule']}
results = []
def check(label, name, ok, observed):
    results.append({'file': label, 'check': name, 'pass': bool(ok), 'observed': observed})
    print(('PASS' if ok else 'FAIL'), label, '|', name, '|', json.dumps(observed)[:300])
def ts(s): return datetime.datetime.fromisoformat(s.replace('Z', '+00:00'))
for f in sorted(os.listdir(E)):
    if not f.endswith('.json') or f == 'analysis.json': continue
    label = f[:-5]; rec = json.load(open(f'{E}/{f}')); body = rec['response']
    route = next(k for k in SOURCES if label.startswith(k))
    check(label, 'status 200', rec['status'] == 200, rec['status'])
    p = body.get('provenance'); check(label, 'provenance object present (deploy proof)', isinstance(p, dict), type(p).__name__)
    if not isinstance(p, dict): continue
    gen, src = p.get('generation'), p.get('sources')
    extra = sorted(set(p) - {'generation', 'sources'})
    if route in ('matchup', 'unscheduled'):
        check(label, 'legacy stream-keyed entries kept beside generation/sources', len(extra) > 0 and all(isinstance(p[k], dict) and p[k].get('stream_key') == k for k in extra), {'legacy_keys': len(extra)})
    else:
        check(label, 'block has exactly generation+sources', extra == [], extra)
    check(label, 'generation is list', isinstance(gen, list), len(gen) if isinstance(gen, list) else gen)
    order = [(g['stream_key'], g['season'] or '') for g in gen]
    check(label, 'generation sorted by stream_key (then season)', order == sorted(order), [g['stream_key'] for g in gen])
    check(label, 'entries are PublicationRead.to_dict shape', all(sorted(g) == sorted(KEYS) for g in gen), {'key_sets_equal_to_to_dict': sorted({sorted(g) == sorted(KEYS) for g in gen}), 'n_keys': len(KEYS)})
    check(label, 'freshness in fresh|stale|unavailable', all(g['freshness'] in ('fresh', 'stale', 'unavailable') for g in gen), sorted({g['freshness'] for g in gen}))
    check(label, 'no provider_window_unsupported stream in generation', not any(g['stream_key'] in UNSUP or g['unavailable_reason'] == 'provider_window_unsupported' for g in gen), [g['stream_key'] for g in gen if g['stream_key'] in UNSUP])
    # Freshness cross-check against age_seconds and the stream's freshness_rule, and age vs retrieved_at at request time.
    req_at = ts(rec['requested_at']); bad = []
    for g in gen:
        if g['freshness'] == 'unavailable': continue
        thr = PUBLICATION_FRESHNESS_SECONDS[RULE[g['stream_key']]]
        want = 'fresh' if g['age_seconds'] <= thr else 'stale'
        approx = (req_at - ts(g['retrieved_at'])).total_seconds()
        if want != g['freshness'] or abs(approx - g['age_seconds']) > rec['elapsed_s'] + 5:
            bad.append((g['stream_key'], g['freshness'], want, g['age_seconds'], round(approx)))
    check(label, 'freshness == rule(age_seconds); age_seconds ~= request time - retrieved_at', not bad, bad or {'rules': sorted({RULE[g['stream_key']] for g in gen}), 'age_days_range': [round(min((g['age_seconds'] for g in gen), default=0)/86400, 1), round(max((g['age_seconds'] for g in gen), default=0)/86400, 1)]})
    check(label, f'sources keys == {SOURCES[route]}', isinstance(src, dict) and sorted(src) == SOURCES[route], sorted(src) if isinstance(src, dict) else src)
    if route == 'slate':
        check(label, 'slate generation == []', gen == [], len(gen))
    if route in ('slate', 'matchup'):
        fr = body['freshness']
        same = all(src[k] == {'status': fr[k]['status'], 'retrieved_at': fr[k]['retrieved_at']} for k in src if k in fr)
        check(label, 'sources copy response freshness surface {status,retrieved_at}', same, {k: src[k] for k in src})
    if route in ('matchup', 'unscheduled'):
        legacy_unsup = [k for k in extra if p[k].get('unavailable_reason') == 'provider_window_unsupported']
        gen_keys = {g['stream_key'] for g in gen}
        check(label, 'generation == legacy entries minus provider_window_unsupported', gen_keys == set(extra) - set(legacy_unsup), {'omitted': legacy_unsup})
        diffs = [g['stream_key'] for g in gen if {k: v for k, v in g.items() if k not in ('freshness', 'age_seconds')} != {k: v for k, v in p[g['stream_key']].items() if k not in ('freshness', 'age_seconds')}]
        check(label, 'generation entries match legacy entries (except freshness/age)', not diffs, diffs)
json.dump(results, open(f'{E}/analysis.json', 'w'), indent=1)
print('TOTAL', len(results), 'FAIL', sum(not r['pass'] for r in results))
