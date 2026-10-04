"""analyze.py: assert footer, structured provenance == production backend block (../../api), get_slate cap/indices, repeat identity."""
import json, os, datetime
R = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); A = os.path.join(os.path.dirname(R), 'api')
CAP = 150_000  # statsplus_mcp.results.MAX_RESULT_CHARS
ld = lambda n: json.load(open(f'{A}/{n}.json'))
apps, noapps = json.load(open(f'{R}/apps.json')), json.load(open(f'{R}/noapps.json'))
BACK = {('get_matchup', 'DET @ CHA'): 'matchup-0022501171', ('get_matchup', None): 'unscheduled-cade-vs-cha',
        ('query_game_logs', None): 'game-logs-cade-mcp-equivalent', ('get_targets', None): 'targets-resolve-2026-04-10', ('get_slate', None): 'slate-2026-04-10'}
res = []
def check(where, name, ok, obs):
    res.append({'where': where, 'check': name, 'pass': bool(ok), 'observed': obs}); print('PASS' if ok else 'FAIL', where, '|', name, '|', json.dumps(obs)[:260])
ts = lambda s: datetime.datetime.fromisoformat(s)
ident = lambda g: (g['stream_key'], g['season'], g['publication_id'])
def strip_age(block):
    b = json.loads(json.dumps(block))
    for k, v in b.items():
        if k == 'generation': [g.pop('age_seconds') for g in v]
        elif isinstance(v, dict) and 'age_seconds' in v: v.pop('age_seconds')
    return b
def tail_lines(text): return text.splitlines()
for mode, run in (('apps', apps), ('noapps', noapps)):
    calls = run['calls']
    for i in range(0, len(calls), 2):
        a, b = calls[i], calls[i + 1]; where = f"{mode} {a['tool']} {json.dumps(a['args'])}"
        t = a['text'][0]; lines = tail_lines(t)
        check(where, 'not isError', not a['isError'] and not b['isError'], [a['isError'], b['isError']])
        fi = max(j for j, l in enumerate(lines) if l.startswith('Data as of'))
        footer = lines[fi:]
        check(where, "footer block starts 'Data as of'; last text line", footer[0].startswith('Data as of'), {'footer_lines': len(footer), 'last_line_starts': lines[-1][:40]})
        check(where, 'repeat: identical footer and identical text', tail_lines(b['text'][0])[fi:] == footer and b['text'] == a['text'], {'gap_s': round((ts(b['called_at']) - ts(a['called_at'])).total_seconds(), 1), 'elapsed_s': [a['elapsed_s'], b['elapsed_s']]})
        if mode == 'noapps':
            ap = apps['calls'][i]
            check(where, 'no structuredContent; text identical to the Apps run', a['structuredContent'] is None and a['text'] == ap['text'], {'structuredContent': a['structuredContent']})
            continue
        sc = a['structuredContent']
        check(where, 'text footer == structuredContent.data_as_of', sc.get('data_as_of') == footer, sc.get('data_as_of', [])[:1])
        if a['tool'] in ('get_slate', 'get_matchup'):  # shared-cache tools
            check(where, 'repeat: identical structuredContent (shared cache)', b['structuredContent'] == sc, None)
        else:  # per-user tools are never cached (server.py): the repeat refetches, so only age_seconds may move
            bsc = b['structuredContent']
            same = {**bsc, 'provenance': strip_age(bsc['provenance'])} == {**sc, 'provenance': strip_age(sc['provenance'])}
            d = sorted({y['age_seconds'] - x['age_seconds'] for x, y in zip(sc['provenance']['generation'], bsc['provenance']['generation'])})
            check(where, 'repeat (uncached per-user tool): structuredContent identical except age_seconds, which advance by the call gap', same, {'age_advance_s': d, 'gap_s': round((ts(b['called_at']) - ts(a['called_at'])).total_seconds(), 1)})
        check(where, f'text+structured under {CAP}', a['textChars'] + a['structuredChars'] < CAP, {'text': a['textChars'], 'structured': a['structuredChars']})
        key = (a['tool'], a['args'].get('game')) if a['tool'] == 'get_matchup' else (a['tool'], None)
        bk = ld(BACK[key]); bp = bk['response']['provenance']
        sp = sc.get('provenance')
        exact = sp == bp
        check(where, f'structured provenance == backend block ({BACK[key]}), ignoring age_seconds', strip_age(sp) == strip_age(bp), {'byte_equal_incl_age': exact})
        if not exact and bp.get('generation'):
            d = sorted({sg['age_seconds'] - bg['age_seconds'] for sg, bg in zip(sp['generation'], bp['generation'])})
            wall = round((ts(a['called_at']) - ts(bk['requested_at'])).total_seconds())
            check(where, 'age_seconds offset is one constant == wall time between backend and MCP reads (+-cache age)', len(set(d)) <= 2 and d[-1] - d[0] <= 1, {'age_offsets_s': d, 'mcp_minus_backend_request_s': wall})
        if a['tool'] == 'get_slate':
            mg, games = sc['matchup_generation'], sc['games']
            check(where, 'matchup_generation has no duplicate identities', len({ident(g) for g in mg}) == len(mg), {'entries': len(mg), 'identities': len({ident(g) for g in mg})})
            bad, srcbad, union = [], [], set()
            LBL = {f"{x['away_team']['tricode']} @ {x['home_team']['tricode']}": x['game_id'] for x in ld('slate-2026-04-10')['response']['games']}
            for g in games:
                gid = LBL[g['label']]
                mp = g.get('matchup_provenance')
                idx = mp['generation']
                ok = all(isinstance(x, int) and 0 <= x < len(mg) for x in idx) and len(set(idx)) == len(idx)
                be = ld(f'matchup-{gid}')['response']['provenance']
                union |= {ident(x) for x in be['generation']}
                if not ok or {ident(mg[x]) for x in idx} != {ident(x) for x in be['generation']}: bad.append(gid)
                if mp['sources'] != be['sources']: srcbad.append(gid)
            check(where, 'per-game indices valid, unique, and resolve to that game backend Matchup identities', not bad, {'games': len(games), 'bad': bad})
            check(where, 'per-game matchup_provenance.sources == backend Matchup sources', not srcbad, srcbad)
            check(where, 'matchup_generation identities == union of 15 backend Matchups', {ident(g) for g in mg} == union, len(union))
            check(where, 'per-game entries carry no legacy stream-keyed provenance', all(set(g['matchup_provenance']) == {'generation', 'sources'} and 'provenance' not in g for g in games), sorted(games[0]['matchup_provenance']))
json.dump(res, open(f'{R}/analysis.json', 'w'), indent=1); print('TOTAL', len(res), 'FAIL', sum(not r['pass'] for r in res))
