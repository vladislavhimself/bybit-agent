"""Проверка на прочность лучшего, что нашлось на крипте (research/crypto_strategies.py): пробой вверх 20 дн с объёмом → лонг, разворот −12% к BTC → лонг.
1. Только ликвидные (топ по истории/обороту) против всех; издержки 0.15% и 0.3%; удержание 3/5/10 дней.
2. Медиана, результат без 2% лучших сделок (не держится ли всё на хвосте), топ-8 сделок, бумаг в плюсе.
3. Режим BTC: только когда BTC выше 200-дневной средней (бычий) против остального — крипто-лонги живут в бычьем режиме?
4. Выходы: трейлинг 5%/8% от лучшего закрытия, предохранитель 10 дней; портфель ≤8 позиций без плеча.
Оговорка: вселенная — пары, торгуемые на Bybit сейчас; умершие монеты в ней отсутствуют (ошибка выжившего завышает лонги альтов).
Запуск из корня: python research/crypto_breakout_check.py."""
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from research import price_strategies as ps  # noqa: E402
from research.crypto_strategies import COST, SPLIT, breakout_signals, load_daily, portfolio  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")
LIQUID = ["BTCUSDT", "ETHUSDT", "XRPUSDT", "SOLUSDT", "DOGEUSDT", "ADAUSDT", "BNBUSDT", "LINKUSDT", "AVAXUSDT", "DOTUSDT", "LTCUSDT", "BCHUSDT", "TRXUSDT",
          "UNIUSDT", "AAVEUSDT", "ATOMUSDT", "NEARUSDT", "XLMUSDT", "ETCUSDT", "FILUSDT", "APTUSDT", "ARBUSDT", "OPUSDT", "INJUSDT", "SUIUSDT", "1000PEPEUSDT"]


def run(panel, syms, side, hold_max, cost, trailing=None, bull=None, sigs=None):
    close, opn, dates = panel["close"], panel["open"], panel["close"].index
    out = []
    for i, sym in sigs:
        if sym not in syms or i + 2 >= len(dates):
            continue
        d0 = dates[i + 1]
        if bull is not None and bull.get(dates[i]) is not True:
            continue
        o = opn[sym].iloc[i + 1]
        if np.isnan(o):
            continue
        best, j_exit, why = -1e9, min(i + hold_max, len(dates) - 1), "время"
        for j in range(i + 1, min(i + hold_max, len(dates) - 1) + 1):
            c = close[sym].iloc[j]
            if np.isnan(c):
                continue
            r = side * (c / o - 1) * 100
            best = max(best, r)
            if trailing is not None and best > 0 and best - r >= trailing:
                why, j_exit = "трейлинг", j
                break
        c = close[sym].iloc[j_exit]
        if np.isnan(c):
            continue
        out.append({"entry": d0, "exit": dates[j_exit], "sym": sym, "pnl": side * (c / o - 1) * 100 - cost, "why": why})
    return out


def summ(name, tr):
    if len(tr) < 20:
        print(f"  {name}: мало ({len(tr)})")
        return
    pn = np.array([t["pnl"] for t in tr])
    d = pd.to_datetime([t["entry"] for t in tr])
    bef, aft = pn[d < SPLIT], pn[d >= SPLIT]
    k = max(1, int(len(pn) * 0.02))
    trimmed = np.sort(pn)[:-k]
    by_sym = defaultdict(list)
    for t in tr:
        by_sym[t["sym"]].append(t["pnl"])
    by_year = defaultdict(list)
    for t in tr:
        by_year[t["entry"].year].append(t["pnl"])
    print(f"  {name}: n={len(pn)} (~{len(pn) / max(1, (d.max() - d.min()).days / 7):.1f}/нед) приб. {np.mean(pn > 0):.0%} средняя {pn.mean():+.2f}% медиана {np.median(pn):+.2f}% "
          f"без 2% лучших {trimmed.mean():+.2f}% | до 2024 {bef.sum():+.0f}% ({np.mean(bef > 0) if len(bef) else 0:.0%}) после {aft.sum():+.0f}% ({np.mean(aft > 0) if len(aft) else 0:.0%}) | "
          f"пар в плюсе {sum(1 for v in by_sym.values() if sum(v) > 0)}/{len(by_sym)}")
    print("     по годам: " + ", ".join(f"{y}: {sum(v):+.0f}% ({len(v)}, {np.mean(np.array(v) > 0):.0%})" for y, v in sorted(by_year.items())))


def main():
    daily = load_daily()
    btc = daily["BTCUSDT"]
    panel = ps.build_panel(daily, btc[["open", "close", "high", "low"]])
    liq = [s for s in LIQUID if s in panel["close"]]
    allsyms = list(panel["close"].columns)
    sma200 = btc["close"].rolling(200).mean()
    bull = ((btc["close"] > sma200) & (btc["close"].rolling(50).mean() > sma200)).shift(1)
    bull = {k: bool(v) if not pd.isna(v) else None for k, v in bull.items()}
    print(f"пар: все {len(allsyms)}, ликвидные {len(liq)}; дней в бычьем режиме BTC: {sum(1 for v in bull.values() if v)} из {len(bull)}")
    sigs_up = breakout_signals(panel, 1)
    print("\nПРОБОЙ ВВЕРХ → ЛОНГ")
    for ulab, uni in (("все", allsyms), ("ликвидные", liq)):
        for hold in (3, 5, 10):
            for cost in (0.15, 0.3):
                tr = run(panel, uni, 1, hold, cost, sigs=sigs_up)
                summ(f"{ulab}, {hold} д, издержки {cost}%", tr)
                if hold == 5 and cost == 0.3:
                    portfolio(tr)
    print("\n  режим BTC (ликвидные, 5 д, 0.3%):")
    tr_b = run(panel, liq, 1, 5, 0.3, bull=bull, sigs=sigs_up)
    summ("только когда BTC в бычьем режиме", tr_b)
    portfolio(tr_b)
    nb = {k: (not v) if v is not None else None for k, v in bull.items()}
    tr_nb = run(panel, liq, 1, 5, 0.3, bull=nb, sigs=sigs_up)
    summ("только когда BTC НЕ в бычьем режиме", tr_nb)
    print("\n  выходы (ликвидные, 0.3%):")
    for tl, hm in ((5.0, 10), (8.0, 10), (5.0, 20), (8.0, 20)):
        tr = run(panel, liq, 1, hm, 0.3, trailing=tl, sigs=sigs_up)
        summ(f"трейлинг {tl}%, макс {hm} д", tr)
        portfolio(tr)
    tr = run(panel, liq, 1, 5, 0.3, sigs=sigs_up)
    best = sorted(tr, key=lambda t: -t["pnl"])[:8]
    print("     топ-8: " + "; ".join(f"{t['sym']} {t['entry'].date()} {t['pnl']:+.0f}%" for t in best))
    print("\nРАЗВОРОТ −12% К BTC ЗА 5 ДН → ЛОНГ")
    rel5 = panel["rel"].rolling(5).sum()
    sigs_r = []
    for sym in rel5.columns:
        r = rel5[sym].to_numpy()
        last = -99
        for i in np.where(r <= -12)[0]:
            if i - last < 5:
                continue
            sigs_r.append((i, sym))
            last = i
    for ulab, uni in (("все", allsyms), ("ликвидные", liq)):
        for hold in (3, 5):
            tr = run(panel, uni, 1, hold, 0.3, sigs=sigs_r)
            summ(f"{ulab}, {hold} д, 0.3%", tr)
            if hold == 3:
                portfolio(tr)
    tr = run(panel, liq, 1, 3, 0.3, bull=bull, sigs=sigs_r)
    summ("ликвидные, 3 д, только бычий режим BTC", tr)
    portfolio(tr)
    print("\nВМЕСТЕ: пробой вверх (трейлинг 8%, макс 10 д) + разворот (3 д), ликвидные, 0.3%, только бычий режим BTC")
    both = run(panel, liq, 1, 10, 0.3, trailing=8.0, bull=bull, sigs=sigs_up) + run(panel, liq, 1, 3, 0.3, bull=bull, sigs=sigs_r)
    summ("лонги вместе", both)
    portfolio(both, 8)
    portfolio(both, 15)


if __name__ == "__main__":
    main()
