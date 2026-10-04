"""get.py LABEL PATH: authenticated READ-ONLY GET against the production backend. Saves
{method,path,status,requested_at,elapsed_s,response} to ../LABEL.json (no tokens) and prints a summary."""
import json, os, sys, time, datetime, urllib.error, urllib.request
BASE = 'https://statsplus-backend-production.up.railway.app'
E = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
label, path = sys.argv[1:3]
tok = json.load(open('/tmp/sp107-pd-secrets/session.json'))['idToken']
req = urllib.request.Request(BASE + path, method='GET', headers={'Authorization': 'Bearer ' + tok})
t0 = time.time(); at = datetime.datetime.now(datetime.timezone.utc).isoformat()
try:
    r = urllib.request.urlopen(req, timeout=180); status = r.status; data = json.load(r)
except urllib.error.HTTPError as e:
    status = e.code; raw = e.read() or b'{}'
    try: data = json.loads(raw)
    except ValueError: data = {'_raw': raw.decode(errors='replace')[:2000]}
json.dump({'method': 'GET', 'path': path, 'status': status, 'requested_at': at, 'elapsed_s': round(time.time() - t0, 2), 'response': data},
          open(f'{E}/{label}.json', 'w'), indent=1)
p = data.get('provenance') if isinstance(data, dict) else None
s = None
if isinstance(p, dict):
    g = p.get('generation')
    s = {'keys': sorted(p.keys()), 'gen': [(x.get('stream_key'), x.get('season'), x.get('freshness'), x.get('age_seconds')) for x in g] if isinstance(g, list) else g, 'sources': p.get('sources')}
print(label, status, sorted(data.keys()) if isinstance(data, dict) else type(data).__name__, '|', json.dumps(s)[:2500])
