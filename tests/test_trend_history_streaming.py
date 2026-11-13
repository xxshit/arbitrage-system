"""Generated price history only; AST loading never starts the application."""
import ast
from copy import deepcopy
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import weakref

from sqlalchemy import Column, Float, Integer, String, create_engine, event
from sqlalchemy.orm import declarative_base, Session

Base = declarative_base()
SOURCE = Path(os.environ.get('ARBI_TREND_TEST_SOURCE', Path(__file__).resolve().parents[1]/'app.py'))

class Spot(Base):
    __tablename__='synthetic_spot'
    id=Column(Integer,primary_key=True)
    symbol=Column(String)
    bucket_at=Column(Integer)
    price=Column(Float)

class Dual(Base):
    __tablename__='synthetic_dual'
    id=Column(Integer,primary_key=True)
    symbol=Column(String)
    exchange=Column(String)
    bucket_at=Column(Integer)
    price=Column(Float)

class Snapshot(Base):
    __tablename__='synthetic_snapshot'
    id=Column(Integer,primary_key=True)
    symbol=Column(String)
    basis=Column(Float)
    short_bid=Column(Float)
    short_ask=Column(Float)

def load_helpers(session, source=None):
    source=Path(source or SOURCE)
    names={'trend_candidate_buckets','nearest_trend_points','trend_history_points',
           'contract_mid_price','enrich_price_changes','enrich_dual_futures_price_changes',
           'enrich_dual_binance_reference'}
    nodes=[n for n in ast.parse(source.read_text()).body if isinstance(n,ast.FunctionDef) and n.name in names]
    assert len(nodes)==len(names)
    ns=dict(db=SimpleNamespace(session=session),PRICE_HISTORY_BUCKET_SECONDS=300)
    exec(compile(ast.Module(nodes,[]),str(source),'exec'),ns)
    return ns

class TrendHistoryStreamingTests(unittest.TestCase):
    def setUp(self):
        self.engine=create_engine('sqlite://')
        Base.metadata.create_all(self.engine)
        self.session=Session(self.engine)
        self.addCleanup(self.engine.dispose);self.addCleanup(self.session.close)
        self.ns=load_helpers(self.session)

    def seed(self):
        data=[]
        for symbol in ('ONE/USDT','二/USDT'):
            for stamp,price in ((900,1.5),(1200,0.),(1500,-.125),(1800,2**40+.125),(2400,3.125)):
                data.append(dict(symbol=symbol,bucket_at=stamp,price=price))
        self.session.execute(Spot.__table__.insert(),data)
        self.session.execute(Dual.__table__.insert(),[dict(row,exchange=e) for row in data for e in ('A','B')])
        self.session.commit()

    def test_all_scalar_values_and_nearest_past_selection_match_orm(self):
        self.seed();targets=[1200,1800,2100,3000,3300];symbols=['ONE/USDT','二/USDT','ABSENT']
        for model,dual in ((Spot,False),(Dual,True)):
            old=self.session.query(model).filter(model.symbol.in_(symbols),model.bucket_at.in_(self.ns['trend_candidate_buckets'](targets))).all()
            expected=self.ns['nearest_trend_points'](old,(lambda r:(r.symbol,r.exchange)) if dual else (lambda r:(r.symbol,)),targets)
            actual=self.ns['trend_history_points'](model,symbols,targets,dual=dual)
            self.assertEqual(actual,expected)
            self.assertTrue(all(k[-1]!=3300 for k in actual))

    def test_no_orm_instances_and_transaction_is_still_owned_by_caller(self):
        self.seed();loaded=[]
        event.listen(self.session,'loaded_as_persistent',lambda session,row:loaded.append(weakref.ref(row)))
        self.ns['trend_history_points'](Spot,['ONE/USDT'],[1800])
        self.assertEqual(loaded,[])
        self.assertTrue(self.session.in_transaction())
        self.assertEqual(self.session.query(Spot).count(),10)

    def test_empty_input_does_not_read_unrequested_history(self):
        self.seed()
        self.assertEqual(self.ns['trend_history_points'](Spot,[],[1800]),{})
        self.assertEqual(self.ns['trend_history_points'](Dual,['ONE/USDT'],[],dual=True),{})

    def test_result_is_closed_on_success_and_fold_failure(self):
        self.seed();real=self.session.execute
        for fail in (False,True):
            captured=[]
            def execute(statement):
                self.assertEqual(statement.get_execution_options()['yield_per'],500)
                result=real(statement);captured.append(result);return result
            with patch.object(self.session,'execute',side_effect=execute):
                if fail:
                    with patch.dict(self.ns,nearest_trend_points=lambda *args:(_ for _ in ()).throw(ValueError('synthetic fold'))):
                        with self.assertRaises(ValueError):self.ns['trend_history_points'](Spot,['ONE/USDT'],[1800])
                else:self.ns['trend_history_points'](Spot,['ONE/USDT'],[1800])
            self.assertTrue(captured[0].closed)

    def test_more_than_one_batch_keeps_all_values(self):
        rows=[dict(symbol=f'X{i}/USDT',bucket_at=1800,price=i/17) for i in range(1301)]
        self.session.execute(Spot.__table__.insert(),rows);self.session.commit()
        actual=self.ns['trend_history_points'](Spot,[r['symbol'] for r in rows],[1800])
        self.assertEqual(actual,{(r['symbol'],1800):r['price'] for r in rows})

    def test_all_three_callers_match_legacy_history_with_fallbacks(self):
        self.seed()
        self.session.execute(Snapshot.__table__.insert(),[
            dict(symbol='FALLBACK',basis=.25,short_bid=7.,short_ask=9.)])
        self.session.commit()
        Snapshot.query=self.session.query(Snapshot)
        self.addCleanup(lambda:delattr(Snapshot,'query'))
        self.ns.update(
            time=SimpleNamespace(time=lambda:2401),FuturesPriceHistory=Spot,
            DualFuturesPriceHistory=Dual,LatestMarketSnapshot=Snapshot,
            TREND_WINDOWS={'change_5m':300,'change_10m':600,'change_20m':1200,'change_30d':2592000},
            SPOT_FUTURES_CACHE={'snapshot':{'symbols':[
                {'symbol':'二/USDT','rows':[{'basis':.125,'short_bid':5.,'short_ask':7.}]}]}},
            LONG_TREND_REFERENCES=SimpleNamespace(
                change=lambda s,p,b,raw_price=False:None if p is None else p/3,
                multiplier=lambda s:10))
        scalar=self.ns['trend_history_points']
        def legacy(model,symbols,targets,dual=False):
            rows=self.session.query(model).filter(model.symbol.in_(symbols),
                model.bucket_at.in_(self.ns['trend_candidate_buckets'](targets))).all()
            return self.ns['nearest_trend_points'](rows,
                (lambda r:(r.symbol,r.exchange)) if dual else (lambda r:(r.symbol,)),targets)
        groups=[{'symbol':symbol,'rows':[
            {'long_exchange':'Binance' if symbol=='ONE/USDT' else 'A','short_exchange':'B',
             'long_bid':3.,'long_ask':5.,'short_bid':5.,'short_ask':7.,'long_basis':.1,
             'alert_stream_route':{'long_factor':.01}}]} for symbol in
            ('ONE/USDT','二/USDT','FALLBACK','ABSENT')]
        for name in ('enrich_price_changes','enrich_dual_futures_price_changes','enrich_dual_binance_reference'):
            expected,actual=deepcopy(groups),deepcopy(groups)
            with patch.dict(self.ns,trend_history_points=legacy):self.ns[name](expected)
            with patch.dict(self.ns,trend_history_points=scalar):self.ns[name](actual)
            self.assertEqual(actual,expected,name)
            self.ns[name]([])
        custom=deepcopy(groups)
        self.ns['enrich_price_changes'](custom,windows={'custom':600})
        self.assertIn('custom',custom[0]['rows'][0])

    def test_mysql_projection_and_bounded_stream_options(self):
        from sqlalchemy.dialects.mysql.pymysql import dialect
        real=self.session.execute
        def execute(statement):
            self.assertEqual(statement.get_execution_options()['yield_per'],500)
            sql=str(statement.compile(dialect=dialect()))
            self.assertNotIn('.id',sql)
            self.assertIn('synthetic_dual.exchange',sql)
            self.assertIn('synthetic_dual.symbol IN',sql)
            self.assertIn('synthetic_dual.bucket_at IN',sql)
            return real(statement)
        with patch.object(self.session,'execute',side_effect=execute):
            self.assertEqual(self.ns['trend_history_points'](Dual,['X'],[1800],dual=True),{})

if __name__=='__main__':unittest.main()
