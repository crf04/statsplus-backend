"""Disposable PostgreSQL verification of issue 46; never writes production."""
import contextlib
import fcntl
import hashlib
import importlib.util
import io
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import uuid
from unittest.mock import patch

OUT = Path('/tmp/statsplus-46-spec-loop')
COORD = Path('/Users/chrisfu/.t3/worktrees/statsplus/t3code-1b6a5bef')
BACKEND = COORD / '.statsplus/worktrees/backend'
sys.path.insert(0, str(BACKEND))
os.chdir(BACKEND)
spec = importlib.util.spec_from_file_location('qa_environment', COORD / '.agents/skills/verify-statsplus/scripts/qa-backend.py')
qa = importlib.util.module_from_spec(spec)
spec.loader.exec_module(qa)
from psycopg2 import sql
from sqlalchemy import create_engine, text

STREAMS = ('player_per36', 'exact_shot_zones_opponent_season', 'exact_shot_zones', 'assist_locations_season')
RESIDENTIAL = ('publication_streams', 'publication_pointers', 'publication_versions', 'player_per36_stats', 'opp_shooting_zone', 'player_shooting_zones', 'pbp_opponent_stats')
GAME_TABLES = ('player_game_logs', 'player_game_log_sync', 'player_game_log_refreshes')
report = {'backendHead': subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(), 'snapshotSha256': hashlib.sha256((qa.QA/'snapshot.json').read_bytes()).hexdigest(), 'scenarios': [], 'environment': 'isolated disposable PostgreSQL', 'qaAdjustments': ['Frozen catalog freshness windows extended to 365 days in this process only.', 'One game removed from local copy to trigger real PBP ingestion; production untouched.'], 'cleanedUp': False}

def hashes(engine, names):
    result = {}
    with engine.connect() as c:
        for name in names:
            result[name] = dict(c.execute(text(f'''SELECT count(*) AS rows, md5(coalesce(string_agg(payload, '' ORDER BY payload), '')) AS checksum FROM (SELECT row_to_json(t)::text AS payload FROM "{name}" t) s''')).mappings().one())
    return result

def completions(engine):
    with engine.connect() as c:
        return dict(c.execute(text('SELECT surface, last_success_at FROM stats_refreshes')).all())

def main():
    env = {k:v for k,v in os.environ.items() if k in {'PATH','HOME','USER','LOGNAME','LANG','TMPDIR'}}
    os.environ.clear(); os.environ.update(env)
    config = json.loads((qa.QA/'config.json').read_text())
    name = 'courtai_qa_' + uuid.uuid4().hex[:16]
    report['database'] = name
    with (qa.LOCAL/'.qa.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX|fcntl.LOCK_NB)
        with qa.postgres(), contextlib.closing(qa.admin()) as root:
            engine = None
            try:
                qa.database(root,name,qa.SEED)
                qa.configure(config,name)
                os.environ.update({'EVENT_CATALOG_MAX_AGE_HOURS':'8760','ATHLETE_CATALOG_FRESHNESS_DAYS':'365'})
                from app.migrations import run_migrations
                from scripts import nightly_refresh as nightly
                from app.providers.nba_stats import NBAStatsAdapter
                from app.providers.pbp_game_logs import PBPGameLogAdapter
                from app.services.database_first_activation import LegacyWriteFence
                from app.services.table_publisher import AtomicTablePublisher
                from app.services.player_game_log_ingest import PlayerGameLogIngestService
                url = qa.local_url(name,config['password'])
                engine = create_engine(url)
                with contextlib.redirect_stdout(io.StringIO()): run_migrations(engine)
                with engine.connect() as c:
                    state=dict(c.execute(text('SELECT stream_key, enabled FROM publication_streams WHERE stream_key IN :keys').bindparams(__import__('sqlalchemy').bindparam('keys',expanding=True)),{'keys':STREAMS}).all())
                assert all(state.get(k) for k in STREAMS), state
                report['activation'] = state
                logging.disable(logging.CRITICAL)
                original_fetch = PBPGameLogAdapter.fetch_game_player_logs
                original_refresh = PlayerGameLogIngestService.refresh
                scenario_calls = []; ingestions = []
                def fetch(self,game_id,*args,**kwargs):
                    scenario_calls.append(game_id)
                    if len(scenario_calls)>4: raise AssertionError('Unexpected broad PBP collection in QA')
                    return original_fetch(self,game_id,*args,**kwargs)
                def refresh(self,*args,**kwargs):
                    result=original_refresh(self,*args,**kwargs)
                    ingestions.append({'gamesProcessed':result.games_processed,'rowCount':result.row_count})
                    return result
                def run(label, expected, metadata_success, extra=None, expect_logs_success=True):
                    scenario_calls.clear(); ingestions.clear()
                    before_meta=hashes(engine,('player_information',)); before_game=hashes(engine,GAME_TABLES); before_res=hashes(engine,RESIDENTIAL); before_c=completions(engine)
                    output=io.StringIO()
                    with contextlib.ExitStack() as stack:
                        if extra: extra(stack)
                        stack.enter_context(contextlib.redirect_stderr(output))
                        stack.enter_context(contextlib.redirect_stdout(output))
                        status=nightly._run(url,hosted_only=True)
                    after_c=completions(engine)
                    assert status==expected, (label,status,output.getvalue()[-1000:])
                    assert hashes(engine,RESIDENTIAL)==before_res, label+' changed residential data'
                    if metadata_success: assert after_c.get('stats_tables') and after_c.get('stats_tables')!=before_c.get('stats_tables'), label
                    else:
                        assert hashes(engine,('player_information',))==before_meta, label
                        assert after_c.get('stats_tables')==before_c.get('stats_tables'), label
                    if expect_logs_success:
                        assert ingestions, label+' did not complete real ingestion'
                        assert after_c.get('player_game_logs')!=before_c.get('player_game_logs'),label
                    else:
                        assert hashes(engine,GAME_TABLES)==before_game,label
                        assert after_c.get('player_game_logs')==before_c.get('player_game_logs'),label
                    row={'scenario':label,'passed':True,'exitStatus':status,'pbpGamesRequested':list(scenario_calls),'ingestions':list(ingestions),'completionBefore':before_c,'completionAfter':after_c,'residentialUnchanged':True,'metadataRowsAfter':hashes(engine,('player_information',))['player_information']['rows'],'diagnostics':output.getvalue()[-1800:]}
                    report['scenarios'].append(row); print(json.dumps({'scenario':label,'passed':True,'status':status,'pbpCalls':list(scenario_calls)}),flush=True)
                with patch.object(NBAStatsAdapter,'__init__',side_effect=AssertionError('NBA adapter constructed')), patch.object(PBPGameLogAdapter,'fetch_game_player_logs',fetch), patch.object(PlayerGameLogIngestService,'refresh',refresh):
                    run('no-new-game hosted success',0,True)
                    for stream in STREAMS:
                        with engine.begin() as c: c.execute(text('UPDATE publication_streams SET enabled=false WHERE stream_key=:key'),{'key':stream})
                        try: run('disabled '+stream,1,False)
                        finally:
                            with engine.begin() as c: c.execute(text('UPDATE publication_streams SET enabled=true WHERE stream_key=:key'),{'key':stream})
                    run('unreadable activation',1,False,lambda s:s.enter_context(patch.object(LegacyWriteFence,'is_activated',side_effect=RuntimeError('QA activation unavailable'))))
                    original_swap=AtomicTablePublisher._swap
                    def swap_then_fail(self,connection,*args):
                        original_swap(connection,*args)
                        raise RuntimeError('QA injected after swap')
                    run('metadata transaction rollback after swap',1,False,lambda s:s.enter_context(patch.object(AtomicTablePublisher,'_swap',swap_then_fail)))
                    game='0022501174'
                    with engine.begin() as c:
                        assert c.execute(text('SELECT count(*) FROM player_game_logs WHERE game_id=:id'),{'id':game}).scalar()>0
                        c.execute(text('DELETE FROM player_game_logs WHERE game_id=:id'),{'id':game})
                        c.execute(text('DELETE FROM player_game_log_sync WHERE game_id=:id'),{'id':game})
                    run('PBP provider failure preserves last good',1,True,lambda s:s.enter_context(patch.object(PBPGameLogAdapter,'fetch_game_player_logs',side_effect=RuntimeError('QA PBP unavailable'))),False)
                    run('real PBP game recovery',0,True)
                report['zeroNBAAdapterConstruction'] = True
            finally:
                if engine: engine.dispose()
                with root.cursor() as cur: cur.execute(sql.SQL('DROP DATABASE IF EXISTS {} WITH (FORCE)').format(sql.Identifier(name)))
                report['cleanedUp']=True

try:
    main()
except Exception as error:
    report['errorType']=type(error).__name__
    # Assertion messages contain only scenario names/booleans; provider errors are withheld.
    if isinstance(error,AssertionError): report['assertion']=str(error)
    print(json.dumps({'failed':True,'errorType':type(error).__name__,'assertion':report.get('assertion')}),flush=True)
    raise SystemExit(1)
finally:
    (OUT/'qa-nightly.json').write_text(json.dumps(report,indent=2,default=str)+'\n')
