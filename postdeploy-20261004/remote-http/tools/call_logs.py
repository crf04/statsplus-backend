"""call_logs.py: read the MCP deployment's stdout via Railway GraphQL deploymentLogs and keep only the per-call JSON lines
(tool, arguments, cache_hit, backend_calls, response_size, outcome, latency_ms; hashed uid dropped). Writes ../call-logs.json."""
import json, os, urllib.request
R = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
tok = json.load(open(os.path.expanduser('~/.railway/config.json')))['user']['token']
q = 'query($d:String!,$n:Int){deploymentLogs(deploymentId:$d,limit:$n){timestamp message severity attributes{key value}}}'
r = urllib.request.Request('https://backboard.railway.com/graphql/v2', data=json.dumps({'query': q, 'variables': {'d': 'ac7378a5-c7fd-43be-bee2-b85bb9bfc467', 'n': 5000}}).encode(),
                           headers={'Authorization': 'Bearer ' + tok, 'Content-Type': 'application/json', 'User-Agent': 'curl/8'})
d = json.load(urllib.request.urlopen(r)); assert 'errors' not in d, [e.get('message') for e in d['errors']]
keep = []
for l in d['data']['deploymentLogs']:
    # Railway parses a JSON stdout line into attributes (message left empty); fall back to the message text.
    m = {a['key']: a['value'] for a in (l.get('attributes') or [])}
    if 'cache_hit' not in m:
        try: m = json.loads(l['message'])
        except ValueError: continue
    for k, v in list(m.items()):
        if isinstance(v, str):
            try: m[k] = json.loads(v)
            except ValueError: pass
    if isinstance(m, dict) and 'cache_hit' in m and 'tool' in m:
        keep.append({'timestamp': l['timestamp'], **{k: m.get(k) for k in ('tool', 'arguments', 'cache_hit', 'backend_calls', 'response_size', 'outcome', 'latency_ms')}})
json.dump(keep, open(f'{R}/call-logs.json', 'w'), indent=1)
print('log lines', len(d['data']['deploymentLogs']), 'call lines', len(keep))
for k in keep: print(k['timestamp'][11:19], k['tool'], json.dumps(k['arguments'], separators=(',', ':'))[:90], 'hit=', k['cache_hit'], 'backend_calls=', k['backend_calls'], 'size=', k['response_size'], k['outcome'], k['latency_ms'])
