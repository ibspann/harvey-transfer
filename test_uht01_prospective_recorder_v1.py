import importlib.util
from pathlib import Path
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
import pandas as pd

P=Path('/mnt/data/uht01_recorder_v1/uht01_prospective_recorder_v1.py')
spec=importlib.util.spec_from_file_location('uht',P)
import sys
uht=importlib.util.module_from_spec(spec); sys.modules['uht']=uht; spec.loader.exec_module(uht)
ET=ZoneInfo('America/New_York')


def make_day(d, contract='MNQZ6', base=100.0):
    t=datetime(d.year,d.month,d.day,9,30,tzinfo=ET)
    rows=[]
    for i in range(390):
        px=base
        rows.append(dict(ts_et=t+timedelta(minutes=i), ts_utc=(t+timedelta(minutes=i)).astimezone(ZoneInfo('UTC')),
                         date_et=d, hm=(t+timedelta(minutes=i)).strftime('%H:%M'), open=px,high=px+0.25,low=px-0.25,close=px,
                         contract=contract,_source_file='synthetic.csv'))
    return pd.DataFrame(rows)


def setbar(df, hm, o,h,l,c):
    i=df.index[df.hm==hm][0]
    df.loc[i,['open','high','low','close']]=[o,h,l,c]
    return i


def test_contract_roll():
    assert uht.active_mnq_contract(date(2026,9,10))=='MNQU6'
    assert uht.active_mnq_contract(date(2026,9,11))=='MNQZ6'
    assert uht.active_mnq_contract(date(2026,12,10))=='MNQZ6'
    assert uht.active_mnq_contract(date(2026,12,11))=='MNQH7'


def test_c1_target_actual_fill():
    prev=make_day(date(2026,9,14)); prev['high']=100.0; prev['low']=99; prev['open']=99.5; prev['close']=99.5
    day=make_day(date(2026,9,15),base=99.0)
    setbar(day,'09:34',99,100.25,98.75,99.5)  # prior touch
    setbar(day,'09:40',100,102.0,98.5,99.5)  # rejection; stop 103; prescreen 3.5 FAIL actually
    # make prescreen 4.0 exactly
    setbar(day,'09:40',100,102.5,98.5,99.5)  # stop 103.5, risk 4.0
    setbar(day,'09:41',99.75,100,99.5,99.75) # fill => risk 3.75 => mismatch
    tr,state=uht.c1(day,prev,'syn','PASS')
    assert state=='FIRST_REJECTION_CONSUMED_FILL_RISK_MISMATCH' and len(tr)==0
    # Change next open to 99.5 => risk 4.0, target 89.5
    setbar(day,'09:41',99.5,100,99.25,99.5)
    setbar(day,'09:42',99.25,99.5,89.0,90.0)
    tr,state=uht.c1(day,prev,'syn','PASS')
    assert state=='QUALIFYING_TRADE' and len(tr)==1
    x=tr[0]
    assert x.entry_price==99.5 and x.actual_risk_points==4.0 and x.target_price==89.5
    assert x.exit_reason=='TARGET' and x.net_usd_1mnq_baseline==16.0


def test_c1_first_rejection_consumes_no_rescue():
    prev=make_day(date(2026,9,14)); prev['high']=100.0
    day=make_day(date(2026,9,15),base=99.0)
    # First rejection before eligible time; later perfect setup must not count.
    setbar(day,'09:31',100,101.25,99,99.5)
    setbar(day,'09:36',99.5,100.25,99,99.5)
    setbar(day,'09:40',100,105,98,99)
    tr,state=uht.c1(day,prev,'syn','PASS')
    assert state=='FIRST_REJECTION_CONSUMED_TIME_FAIL' and not tr


def test_c2_short_target():
    day=make_day(date(2026,9,15),base=100.0)
    # OR15 high ~100.25 low ~99.75. Create short sweep with huge direct risk.
    setbar(day,'09:45',101,130,100,100)  # short sweep; midpoint115; direct stop131
    setbar(day,'09:46',100,116,99,110)   # +1 open100, direct risk31>20; secondary high>=115 close<115; stop117; pre risk7
    setbar(day,'09:47',110,111,109,110)  # entry110, final risk7, target92.5
    setbar(day,'09:48',109,110,92.0,93)
    tr,state=uht.c2(day,'syn','PASS')
    assert state=='QUALIFYING_TRADE' and len(tr)==1
    x=tr[0]
    assert x.side=='SHORT' and x.structural_stop==117.0 and x.entry_price==110.0 and x.target_price==92.5
    assert x.exit_reason=='TARGET' and x.net_usd_1mnq_baseline==31.0


def test_c2_secondary_consumption_risk_fail():
    day=make_day(date(2026,9,15),base=100.0)
    setbar(day,'09:45',101,130,100,100)  # short sweep midpoint115 stop131
    # first secondary occurs +1 but has prescreen risk <4; later +2 could qualify but is forbidden
    setbar(day,'09:46',100,115.25,100,113.0)  # stop116.25 - close113=3.25 fail
    setbar(day,'09:47',110,120,100,110)
    tr,state=uht.c2(day,'syn','PASS')
    assert state=='SECONDARY_CONSUMED_PRESCREEN_RISK_FAIL' and not tr


def test_stop_first_ambiguity():
    day=make_day(date(2026,9,15),base=100.0)
    # direct lifecycle test: short entry100 stop105 target90; bar touches both; must stop.
    i=10
    day.loc[i,['open','high','low','close']]=[100,106,89,100]
    ex_i,px,reason=uht.resolve_trade(day,i,'SHORT',100,105,90)
    assert ex_i==i and reason=='STOP' and px==105


def test_required_window_contiguous():
    d=make_day(date(2026,9,15))
    assert uht.required_window_contiguous(d)
    d=d[d.hm!='10:17'].copy()
    assert not uht.required_window_contiguous(d)

if __name__=='__main__':
    tests=[v for k,v in globals().items() if k.startswith('test_')]
    for t in tests:
        t(); print('PASS',t.__name__)
    print(f'{len(tests)}/{len(tests)} passed')