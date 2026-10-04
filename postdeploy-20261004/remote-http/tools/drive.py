"""drive.py MODE(apps|noapps): authenticated Streamable HTTP session against the production remote MCP. Captures initialize
(instructions, capabilities), resources, statsplus://vocabulary, and each tool call twice back-to-back. Saves ../MODE.json (no tokens)."""
import asyncio, json, os, re, sys, time, datetime
import httpx2
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client
E = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
URL = 'https://statsplus-mcp-production.up.railway.app/mcp'
mode = sys.argv[1]; APPS = {'io.modelcontextprotocol/ui': {'mimeTypes': ['text/html;profile=mcp-app']}} if mode == 'apps' else None
tok = json.load(open('/tmp/sp107-pd-secrets/mcp_token.json'))['access_token']
DATE = '2026-04-10'; GAP = 3.6  # stay under the 20 calls/minute per-uid cap
def dump(o):
    return json.loads(json.dumps(o.model_dump(mode='json', by_alias=True, exclude_none=True) if hasattr(o, 'model_dump') else o, default=str))
async def main():
    out = {'mode': mode, 'url': URL, 'started_at': datetime.datetime.now(datetime.timezone.utc).isoformat(), 'calls': []}
    http = create_mcp_http_client(headers={'Authorization': 'Bearer ' + tok}, timeout=httpx2.Timeout(30, read=300))
    async with http, streamable_http_client(URL, http_client=http) as streams:
        r, w = streams[0], streams[1]
        async with (ClientSession(r, w, extensions=APPS) if APPS else ClientSession(r, w)) as c:
            init = await c.initialize(); out['initialize'] = dump(init)
            out['resources'] = dump(await c.list_resources())
            out['vocabulary'] = dump(await c.read_resource('statsplus://vocabulary'))
            async def call(name, args):
                for attempt in (1, 2):
                    t0 = time.time(); at = datetime.datetime.now(datetime.timezone.utc).isoformat()
                    res = await c.call_tool(name, args); d = dump(res)
                    texts = [x['text'] for x in d.get('content', []) if x.get('type') == 'text']
                    sc = d.get('structuredContent')
                    out['calls'].append({'tool': name, 'args': args, 'attempt': attempt, 'called_at': at, 'elapsed_s': round(time.time() - t0, 2),
                                         'isError': d.get('isError', False), 'text': texts, 'structuredContent': sc,
                                         'textChars': sum(map(len, texts)), 'structuredChars': len(json.dumps(sc)) if sc is not None else None})
                    o = out['calls'][-1]; last = (texts[0].strip().splitlines() or [''])[-1] if texts else ''
                    print(mode, name, json.dumps(args), 'try', attempt, 'isError', o['isError'], 'text', o['textChars'], 'struct', o['structuredChars'], '|', last[:300], flush=True)
                    await asyncio.sleep(GAP)
                return out['calls'][-1]
            await call('get_slate', {'date': DATE})
            m = await call('get_matchup', {'game': 'DET @ CHA', 'date': DATE})
            await call('get_matchup', {'player': 'Cade Cunningham', 'opponent': 'CHA'})
            blob = json.dumps(m['structuredContent']) + ' '.join(m['text'])
            rows = re.findall(r'sheet:[a-z_]+:[A-Za-z0-9 ]+:[A-Z0-9_]+', blob)
            row = rows[0] if rows else 'sheet:play_types:PRBallHandler:PTS'
            out['driver_row'] = {'row': row, 'from_matchup': bool(rows), 'candidates': sorted(set(rows))[:20]}
            await call('query_game_logs', {'player': 'Cade Cunningham', 'vs': [{'row': row, 'ranks': [1, 10]}], 'date': DATE})
            await call('get_targets', {'date': DATE})
    json.dump(out, open(f'{E}/{mode}.json', 'w'), indent=1)
asyncio.run(main())
