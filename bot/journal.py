"""Журнал: сделки, новости, события, уроки, история equity. SQLite в data/bot.db."""
import json
import sqlite3
import threading
import time

from .config import DATA

SCHEMA = """
CREATE TABLE IF NOT EXISTS news (
    id TEXT PRIMARY KEY, source TEXT, title TEXT, summary TEXT, url TEXT,
    published_ts INTEGER, fetched_ts INTEGER, analyzed INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS events (
    news_id TEXT PRIMARY KEY, ts INTEGER, event_type TEXT, importance INTEGER, direction TEXT,
    confidence REAL, affected TEXT, market_wide INTEGER, horizon_hours INTEGER, reasoning TEXT, title TEXT,
    crowd_reaction TEXT, contrarian_risk REAL
);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, side TEXT, qty REAL, entry REAL, stop REAL, tp REAL,
    stop_init REAL, tier TEXT, score REAL, confidence REAL, reasons TEXT, regime TEXT, atr REAL, leverage REAL,
    opened_ts INTEGER, closed_ts INTEGER, exit REAL, pnl REAL, pnl_r REAL, status TEXT, close_reason TEXT,
    order_id TEXT, news_driven INTEGER, qty_init REAL, tp_plan TEXT, tp_hits INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS pending (
    id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, side TEXT, qty REAL, price REAL, stop REAL, order_id TEXT,
    created_ts INTEGER, expires_ts INTEGER, event_key TEXT, payload TEXT
);
CREATE TABLE IF NOT EXISTS shadow (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, symbol TEXT, side TEXT, reason TEXT, strength REAL,
    threshold REAL, price REAL, atr REAL, stop_dist REAL, pull_atr REAL, regime TEXT, comps TEXT, ext REAL,
    news_driven INTEGER, event_key TEXT, status TEXT DEFAULT 'open', next_check_ts INTEGER,
    r_market REAL, r_pullback REAL, exit_market TEXT, mfe REAL, mae REAL, resolved_ts INTEGER
);
CREATE INDEX IF NOT EXISTS shadow_key ON shadow(symbol, side, reason, ts);
CREATE TABLE IF NOT EXISTS lessons (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, text TEXT, source TEXT);
CREATE TABLE IF NOT EXISTS equity (ts INTEGER PRIMARY KEY, equity REAL);
CREATE TABLE IF NOT EXISTS reviews (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, summary TEXT, changes TEXT, applied INTEGER);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
"""


class Journal:
    def __init__(self, path=None):
        self.path = str(path or DATA / "bot.db")
        self.lock = threading.Lock()
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        with self.lock:
            self.db.executescript(SCHEMA)
            # миграции для баз, созданных ранними версиями
            for col, typ in (("qty_init", "REAL"), ("tp_plan", "TEXT"), ("tp_hits", "INTEGER DEFAULT 0")):
                try:
                    self.db.execute(f"ALTER TABLE trades ADD COLUMN {col} {typ}")
                except sqlite3.OperationalError:
                    pass
            for col, typ in (("crowd_reaction", "TEXT"), ("contrarian_risk", "REAL")):
                try:
                    self.db.execute(f"ALTER TABLE events ADD COLUMN {col} {typ}")
                except sqlite3.OperationalError:
                    pass
            for col, typ in (("event_key", "TEXT DEFAULT ''"), ("mfe_r", "REAL DEFAULT 0"), ("mfe_ts", "INTEGER DEFAULT 0")):
                try:
                    self.db.execute(f"ALTER TABLE trades ADD COLUMN {col} {typ}")
                except sqlite3.OperationalError:
                    pass
            self.db.commit()

    def _run(self, sql, params=(), fetch=None):
        with self.lock:
            cur = self.db.execute(sql, params)
            self.db.commit()
            if fetch == "all":
                return [dict(r) for r in cur.fetchall()]
            if fetch == "one":
                r = cur.fetchone()
                return dict(r) if r else None
            return cur.lastrowid

    # ---- news / events ----
    def add_news(self, item) -> bool:
        try:
            self._run("INSERT INTO news(id,source,title,summary,url,published_ts,fetched_ts) VALUES(?,?,?,?,?,?,?)",
                      (item["id"], item["source"], item["title"], item.get("summary", ""), item.get("url", ""),
                       item["published_ts"], int(time.time())))
            return True
        except sqlite3.IntegrityError:
            return False

    def unanalyzed_news(self, limit=20):
        return self._run("SELECT * FROM news WHERE analyzed=0 ORDER BY published_ts DESC LIMIT ?", (limit,), "all")

    def mark_analyzed(self, news_id):
        self._run("UPDATE news SET analyzed=1 WHERE id=?", (news_id,))

    def add_event(self, news_id, ts, ev: dict, title):
        self._run("INSERT OR REPLACE INTO events(news_id,ts,event_type,importance,direction,confidence,affected,"
                  "market_wide,horizon_hours,reasoning,title,crowd_reaction,contrarian_risk) "
                  "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (news_id, ts, ev["event_type"], ev["importance"], ev["direction"], ev["confidence"],
                   json.dumps(ev["affected_symbols"]), int(ev["market_wide"]), ev["horizon_hours"],
                   ev["reasoning"], title, ev.get("crowd_reaction", "neutral"), float(ev.get("contrarian_risk") or 0)))

    def recent_events(self, hours=72):
        rows = self._run("SELECT * FROM events WHERE ts>=? ORDER BY ts DESC",
                         (int(time.time()) - hours * 3600,), "all")
        for r in rows:
            r["affected"] = json.loads(r["affected"] or "[]")
        return rows

    # ---- trades ----
    def open_trade(self, t: dict) -> int:
        return self._run(
            "INSERT INTO trades(symbol,side,qty,entry,stop,tp,stop_init,tier,score,confidence,reasons,regime,atr,"
            "leverage,opened_ts,status,order_id,news_driven,qty_init,tp_plan,tp_hits,event_key) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'open',?,?,?,?,0,?)",
            (t["symbol"], t["side"], t["qty"], t["entry"], t["stop"], t["tp"], t["stop"], t["tier"], t["score"],
             t["confidence"], json.dumps(t["reasons"], ensure_ascii=False), t["regime"], t["atr"], t["leverage"],
             int(time.time()), t.get("order_id", ""), int(t.get("news_driven", False)), t["qty"],
             json.dumps(t.get("tp_plan", [])), t.get("event_key", "")))

    # ---- отложенные входы (лимитные ордера на откат) ----
    def add_pending(self, p: dict) -> int:
        return self._run(
            "INSERT INTO pending(symbol,side,qty,price,stop,order_id,created_ts,expires_ts,event_key,payload) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (p["symbol"], p["side"], p["qty"], p["price"], p["stop"], p["order_id"], int(time.time()),
             p["expires_ts"], p.get("event_key", ""), json.dumps(p["payload"], ensure_ascii=False)))

    def pendings(self):
        rows = self._run("SELECT * FROM pending", (), "all")
        for r in rows:
            r["payload"] = json.loads(r["payload"] or "{}")
        return rows

    def remove_pending(self, pid):
        self._run("DELETE FROM pending WHERE id=?", (pid,))

    def update_pnl(self, trade_id, exit_price, pnl, reason=None):
        t = self._run("SELECT * FROM trades WHERE id=?", (trade_id,), "one")
        risk = abs(t["entry"] - t["stop_init"]) * (t["qty_init"] or t["qty"])
        pnl_r = pnl / risk if risk > 0 else 0.0
        self._run("UPDATE trades SET exit=?, pnl=?, pnl_r=?, close_reason=COALESCE(?, close_reason) WHERE id=?",
                  (exit_price, pnl, pnl_r, reason, trade_id))

    def unreconciled(self, hours=48):
        """Закрытые сделки, у которых результат не удалось получить с биржи."""
        return self._run("SELECT * FROM trades WHERE status='closed' AND closed_ts>=? AND close_reason LIKE '%не получен%'",
                         (int(time.time()) - hours * 3600,), "all")

    def open_trades(self):
        rows = self._run("SELECT * FROM trades WHERE status='open'", (), "all")
        for r in rows:
            r["reasons"] = json.loads(r["reasons"] or "[]")
            r["tp_plan"] = json.loads(r["tp_plan"] or "[]")
        return rows

    def update_stop(self, trade_id, stop):
        self._run("UPDATE trades SET stop=? WHERE id=?", (stop, trade_id))

    # ---- теневые сделки ----
    def add_shadow(self, d):
        cols = ",".join(d)
        return self._run(f"INSERT INTO shadow({cols}) VALUES({','.join('?' * len(d))})", tuple(d.values()))

    def shadow_exists(self, symbol, side, reason, since_ts):
        r = self._run("SELECT 1 FROM shadow WHERE symbol=? AND side=? AND reason=? AND ts>=? LIMIT 1",
                      (symbol, side, reason, since_ts), "one")
        return r is not None

    def shadows_due(self, now, limit):
        return self._run("SELECT * FROM shadow WHERE status='open' AND next_check_ts<=? ORDER BY next_check_ts LIMIT ?",
                         (now, limit), "all")

    def shadow_next_check(self, sid, ts):
        self._run("UPDATE shadow SET next_check_ts=? WHERE id=?", (ts, sid))

    def resolve_shadow(self, sid, r_market, r_pullback, exit_market, mfe, mae, now):
        self._run("UPDATE shadow SET status='done', r_market=?, r_pullback=?, exit_market=?, mfe=?, mae=?, resolved_ts=? "
                  "WHERE id=?", (r_market, r_pullback, exit_market, mfe, mae, now, sid))

    def shadows_resolved(self, since_resolved_ts):
        return self._run("SELECT * FROM shadow WHERE status='done' AND resolved_ts>? ORDER BY resolved_ts DESC",
                         (since_resolved_ts,), "all")

    def shadows_open_count(self):
        return self._run("SELECT COUNT(*) AS n FROM shadow WHERE status='open'", (), "one")["n"]

    def last_entry_ts(self, symbol, side):
        r = self._run("SELECT MAX(opened_ts) AS m FROM trades WHERE symbol=? AND side=?", (symbol, side), "one")
        return (r or {}).get("m") or 0

    def update_mfe(self, trade_id, mfe_r):
        self._run("UPDATE trades SET mfe_r=?, mfe_ts=? WHERE id=?", (mfe_r, int(time.time()), trade_id))

    def update_partial(self, trade_id, qty, tp_hits):
        self._run("UPDATE trades SET qty=?, tp_hits=? WHERE id=?", (qty, tp_hits, trade_id))

    def close_trade(self, trade_id, exit_price, pnl, reason):
        t = self._run("SELECT * FROM trades WHERE id=?", (trade_id,), "one")
        risk = abs(t["entry"] - t["stop_init"]) * (t["qty_init"] or t["qty"])
        pnl_r = pnl / risk if risk > 0 else 0.0
        self._run("UPDATE trades SET status='closed', closed_ts=?, exit=?, pnl=?, pnl_r=?, close_reason=? WHERE id=?",
                  (int(time.time()), exit_price, pnl, pnl_r, reason, trade_id))

    def closed_trades(self, limit=50):
        rows = self._run("SELECT * FROM trades WHERE status='closed' ORDER BY closed_ts DESC LIMIT ?", (limit,), "all")
        for r in rows:
            r["reasons"] = json.loads(r["reasons"] or "[]")
        return rows

    def closed_count(self):
        return self._run("SELECT COUNT(*) AS n FROM trades WHERE status='closed'", (), "one")["n"]

    def stats(self, limit=200):
        rows = self.closed_trades(limit)
        if not rows:
            return {"n": 0}
        wins = [r for r in rows if r["pnl"] > 0]
        gross_win = sum(r["pnl"] for r in wins)
        gross_loss = -sum(r["pnl"] for r in rows if r["pnl"] <= 0)
        return {
            "n": len(rows), "win_rate": round(len(wins) / len(rows) * 100, 1),
            "pnl": round(sum(r["pnl"] for r in rows), 2),
            "avg_r": round(sum(r["pnl_r"] for r in rows) / len(rows), 2),
            "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else float("inf"),
        }

    # ---- lessons / reviews / misc ----
    def add_lesson(self, text, source="review"):
        self._run("INSERT INTO lessons(ts,text,source) VALUES(?,?,?)", (int(time.time()), text, source))

    def lessons(self, limit=25):
        return self._run("SELECT * FROM lessons ORDER BY ts DESC LIMIT ?", (limit,), "all")

    def add_review(self, summary, changes, applied):
        self._run("INSERT INTO reviews(ts,summary,changes,applied) VALUES(?,?,?,?)",
                  (int(time.time()), summary, json.dumps(changes, ensure_ascii=False), int(applied)))

    def log_equity(self, equity):
        self._run("INSERT OR REPLACE INTO equity VALUES(?,?)", (int(time.time()), equity))

    def equity_series(self, hours=24 * 30):
        return self._run("SELECT * FROM equity WHERE ts>=? ORDER BY ts", (int(time.time()) - hours * 3600,), "all")

    def kv_get(self, k, default=None):
        r = self._run("SELECT v FROM kv WHERE k=?", (k,), "one")
        return json.loads(r["v"]) if r else default

    def kv_set(self, k, v):
        self._run("INSERT OR REPLACE INTO kv VALUES(?,?)", (k, json.dumps(v)))
