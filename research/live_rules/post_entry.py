import sys, time, json, sqlite3
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, r"C:\Users\Владислав\AppData\Roaming\Claude\scratch-workspaces\7e7b2fac-0aac-4f9c-9293-17fe02398278\ac08b648-649e-491c-ba2b-af0090a44474\scratch-2026-10-13-bb4ff5\bybit-agent")
import pandas as pd
from pybit.unified_trading import HTTP
mk = HTTP()
db = sqlite3.connect(sys.argv[1]); db.row_factory = sqlite3.Row

def k5(sym, start_ms, end_ms):
    out = []; cur = start_ms
    while cur < end_ms:
        r = mk.get_kline(category="linear", symbol=sym, interval="5", start=cur, end=min(end_ms, cur + 999*300000), limit=1000)["result"]["list"]
        if not r: break
        df = pd.DataFrame(r, columns=["ts","o","h","l","c","v","t"]).astype(float).iloc[::-1]
        out.append(df); cur = int(df.ts.iloc[-1]) + 300000
        if len(r) < 1000: break
        time.sleep(0.2)
    d = pd.concat(out).drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
    d["ts"] = d.ts.astype("int64"); return d

rows = []
now = int(time.time()*1000)
for t in db.execute("select * from trades order by id"):
    t = dict(t); sym = t["symbol"]; long = t["side"] == "Buy"; sg = 1 if long else -1
    e = t["entry"]; R = abs(e - t["stop_init"]); o = t["opened_ts"]*1000
    d = k5(sym, o - 24*3600*1000, min(now, o + 48*3600*1000))
    pre = d[d.ts < o]; post = d[d.ts >= o].reset_index(drop=True)
    def at(h):
        x = post[post.ts <= o + h*3600*1000]
        return None if x.empty or (post.ts.iloc[-1] < o + h*3600*1000 - 600000) else round(sg*(x.c.iloc[-1]-e)/R, 2)
    # первый момент касания стопа
    adverse = (post.l <= t["stop_init"]) if long else (post.h >= t["stop_init"])
    hit_i = int(adverse.idxmax()) if adverse.any() else None
    seg = post.iloc[:hit_i+1] if hit_i is not None else post
    mfe_before_stop = round(max(0, (sg*((seg.h if long else seg.l) - e)).max()/R), 2)
    fav24 = post[post.ts <= o + 24*3600*1000]
    mfe24 = round((sg*((fav24.h if long else fav24.l) - e)).max()/R, 2)
    mae24 = round((sg*((fav24.l if long else fav24.h) - e)).min()/R, 2)
    # где вход относительно предыдущего движения
    p4 = pre[pre.ts >= o - 4*3600*1000]; p24 = pre
    run4 = round(sg*(e/p4.o.iloc[0]-1)*100, 2) if len(p4) else None
    run24 = round(sg*(e/p24.o.iloc[0]-1)*100, 2) if len(p24) else None
    stop_pct = round(R/e*100, 2)
    # Стоп в 2 раза шире: выбило бы? где была бы цена через 24ч (в новых R)
    w = 2.0; st2 = e - sg*R*w
    adv2 = (post.l <= st2) if long else (post.h >= st2)
    hit2 = adv2[post.ts <= o + 24*3600*1000].any()
    r24 = at(24)
    rows.append(dict(id=t["id"], sym=sym.replace("USDT",""), side="L" if long else "S", tier=t["tier"],
        opened=time.strftime("%d.%m %H:%M", time.gmtime(t["opened_ts"])), pnl=round(t["pnl"] or 0, 2),
        stop_pct=stop_pct, run4_pct=run4, run24_pct=run24,
        hit_stop_min=(None if hit_i is None else int((post.ts.iloc[hit_i]-o)/60000)),
        mfe_bs=mfe_before_stop, r1h=at(1), r4h=at(4), r12h=at(12), r24h=r24, mfe24=mfe24, mae24=mae24,
        stop2x_hit24=bool(hit2)))
    time.sleep(0.2)
df = pd.DataFrame(rows)
pd.set_option("display.width", 250); pd.set_option("display.max_columns", 30)
print(df.to_string(index=False))
df.to_csv(sys.argv[2], index=False)
