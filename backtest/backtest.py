"""Бэктест технической части стратегии на часовых свечах (без новостей: исторических новостей с метками пока нет).
Учитывает комиссию тейкера, проскальзывание, ATR-стоп, тейк, трейлинг и тайм-стоп, как в боте.
Пример: python backtest/backtest.py BTCUSDT ETHUSDT SOLUSDT --days 365 --split 0.7
--split делит историю на обучающую и проверочную части (walk-forward в простейшем виде)."""
import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bot.config import DATA, Settings  # noqa: E402
from bot.ta import snapshot  # noqa: E402

FEE = 0.00055      # тейкер Bybit
SLIP = 0.0002      # проскальзывание на сторону


def load(symbol, interval="60"):
    p = DATA / "history" / f"{symbol}_{interval}.csv"
    if not p.exists():
        raise SystemExit(f"Нет файла {p}. Сначала: python scripts/fetch_history.py {symbol} --interval {interval}")
    return pd.read_csv(p)


def resample_4h(df1h):
    d = df1h.copy()
    d["t"] = pd.to_datetime(d["ts"], unit="ms")
    g = d.set_index("t").resample("4h")
    out = pd.DataFrame({"open": g["open"].first(), "high": g["high"].max(), "low": g["low"].min(),
                        "close": g["close"].last(), "volume": g["volume"].sum()}).dropna()
    out["ts"] = (out.index.astype("int64") // 10**6)
    return out.reset_index(drop=True)


def run(symbol, df, cfg, entry_threshold, start_equity=1000.0):
    r = cfg["risk"]
    ta_w = cfg["signals"]["ta_weight"]
    equity, trades = start_equity, []
    pos = None
    df4h_all = resample_4h(df)
    for i in range(250, len(df) - 1):
        window = df.iloc[: i + 1]
        bar_next = df.iloc[i + 1]
        if pos:
            # проверка стопа/тейка на следующей свече (консервативно: сначала стоп)
            long = pos["side"] == "long"
            hit_sl = bar_next["low"] <= pos["stop"] if long else bar_next["high"] >= pos["stop"]
            hit_tp = bar_next["high"] >= pos["tp"] if long else bar_next["low"] <= pos["tp"]
            hours = i + 1 - pos["i"]
            exit_price, reason = None, None
            if hit_sl:
                exit_price, reason = pos["stop"], "stop"
            elif hit_tp:
                exit_price, reason = pos["tp"], "tp"
            elif hours >= r["max_hours_in_trade"]:
                exit_price, reason = bar_next["close"], "time"
            if exit_price is not None:
                px = exit_price * (1 - SLIP) if long else exit_price * (1 + SLIP)
                pnl = (px - pos["entry"]) * pos["qty"] * (1 if long else -1)
                pnl -= (pos["entry"] + px) * pos["qty"] * FEE
                equity += pnl
                trades.append({"symbol": symbol, "side": pos["side"], "entry_i": pos["i"], "exit_i": i + 1,
                               "pnl": pnl, "pnl_r": pnl / pos["risk"], "reason": reason, "tier": pos["tier"]})
                pos = None
            else:
                # трейлинг ступенями по R
                mark = bar_next["close"]
                r_now = ((mark - pos["entry"]) if long else (pos["entry"] - mark)) / pos["dist"]
                if r_now >= r["trail_after_r"]:
                    step = int(r_now)
                    new_stop = pos["entry"] + (step - 1 + 0.2) * pos["dist"] * (1 if long else -1)
                    if (long and new_stop > pos["stop"]) or (not long and new_stop < pos["stop"]):
                        pos["stop"] = new_stop
            continue
        if i % 4 != 0:   # сканируем раз в 4 часа, как бот (scan_sec ≈ 5 мин, но ТА-сигнал меняется медленно)
            continue
        ts_now = window["ts"].iloc[-1]
        df4h = df4h_all[df4h_all["ts"] <= ts_now - 14_400_000]
        ta = snapshot(window.tail(300).reset_index(drop=True), df4h.tail(120).reset_index(drop=True))
        if not ta or ta["regime"] == "range":
            continue
        strength = abs(ta["score"]) * ta_w   # как в signals.py: без новостей и позиционирования остаётся ta_weight*|ТА|
        if strength < entry_threshold:
            continue
        side = "long" if ta["score"] > 0 else "short"
        if ta["trend_4h"] != 0 and (ta["trend_4h"] > 0) != (side == "long"):
            continue
        tier = "medium" if strength >= entry_threshold + 0.08 else "low"   # чистый ТА: не выше medium, как в боте
        if ta["regime"] == "high_vol":
            tier = {"high": "medium", "medium": "low", "low": "low"}[tier]
        entry = bar_next["open"] * (1 + SLIP) if side == "long" else bar_next["open"] * (1 - SLIP)
        dist = ta["atr"] * r["stop_atr_mult"]
        stop = entry - dist if side == "long" else entry + dist
        tp_r = max((l["r"] for l in r.get("tp_levels") or []), default=2.5)  # упрощение: один тейк на дальнем уровне
        tp = entry + dist * tp_r if side == "long" else entry - dist * tp_r
        risk_amt = equity * r["risk_per_trade_pct"][tier] / 100
        qty = risk_amt / dist
        notional_cap = equity * r["max_total_exposure_x"] / 2
        if qty * entry > notional_cap:
            qty = notional_cap / entry
        pos = {"side": side, "entry": entry, "stop": stop, "tp": tp, "qty": qty, "dist": dist,
               "risk": qty * dist, "i": i + 1, "tier": tier}
    return equity, trades


def report(name, trades, start, end):
    if not trades:
        print(f"{name}: сделок нет")
        return
    t = pd.DataFrame(trades)
    wins = t[t.pnl > 0]
    gl = -t[t.pnl <= 0].pnl.sum()
    pf = wins.pnl.sum() / gl if gl > 0 else float("inf")
    eq = start + t.pnl.cumsum()
    dd = ((eq.cummax() - eq) / eq.cummax()).max() * 100
    print(f"{name}: сделок {len(t)}, винрейт {len(wins)/len(t)*100:.1f}%, средний R {t.pnl_r.mean():+.2f}, "
          f"PF {pf:.2f}, итог {end/start*100-100:+.1f}%, макс. просадка {dd:.1f}%")
    print("   по причинам выхода:", t.reason.value_counts().to_dict())


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("symbols", nargs="+")
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--split", type=float, default=0.7, help="доля истории для 'обучения', остальное проверка")
    ap.add_argument("--threshold", type=float, default=None)
    a = ap.parse_args()
    cfg = Settings().cfg
    thr = a.threshold or cfg["signals"]["entry_threshold"]
    print(f"Порог входа {thr}, вес ТА {cfg['signals']['ta_weight']}: проверяется ТОЛЬКО техническая часть стратегии.")
    print("Чтобы оценить ТА-компонент сам по себе, запустите с --threshold 0.4")
    all_in, all_out = [], []
    for sym in a.symbols:
        df = load(sym)
        df = df[df["ts"] >= df["ts"].max() - a.days * 86_400_000].reset_index(drop=True)
        cut = int(len(df) * a.split)
        e1, t1 = run(sym, df.iloc[:cut].reset_index(drop=True), cfg, thr)
        e2, t2 = run(sym, df.iloc[max(0, cut - 300):].reset_index(drop=True), cfg, thr)
        print(f"\n== {sym} ==")
        report("  обучающая часть ", t1, 1000.0, e1)
        report("  проверочная часть", t2, 1000.0, e2)
        all_in += t1
        all_out += t2
    print("\n== ИТОГО ==")
    report("обучающая ", all_in, 1000.0, 1000.0 + sum(t["pnl"] for t in all_in))
    report("проверочная", all_out, 1000.0, 1000.0 + sum(t["pnl"] for t in all_out))
    out = DATA / "backtest_trades.csv"
    pd.DataFrame(all_in + all_out).to_csv(out, index=False)
    print(f"\nСделки сохранены: {out}")
