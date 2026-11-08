"""Новости крипты тем же методом, что Мосбиржа: (1) макро-данные США с ожиданием прямо из ленты («CPI 3.4% vs 3.2% expected») →
размер расхождения → реакция BTC; (2) знак новости по словам (ETF одобрен/отклонён, взлом, иск SEC, запрет, Трамп, ликвидации…) →
угадывает ли направление BTC за 1ч/4ч/день/неделю, до и после 2024; (3) шоки (тишина → взрыв) и продолжение за первым часом.
Архив: data/crypto_archive.db (research/news_archive.py --db …, англоязычные каналы с 2021). Свечи: data/history/BTCUSDT_60.csv (мс).
Запуск из корня: python research/crypto_news_study.py. Модель не вызывается."""
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parents[1]
SPLIT = int(datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp())
# макро США с ожиданием. sign_hot: +1 если «выше ожиданий» = жёстче для ФРС (плохо для BTC), −1 если выше = мягче (хорошо)
MACRO = [("CPI", re.compile(r"\b(core )?CPI\b", re.I), +1), ("PPI", re.compile(r"\bPPI\b", re.I), +1), ("PCE", re.compile(r"\bPCE\b", re.I), +1),
         ("NFP", re.compile(r"non-?farm|\bNFP\b|payrolls", re.I), +1), ("безработица", re.compile(r"unemployment rate", re.I), -1),
         ("ВВП", re.compile(r"\bGDP\b", re.I), +1), ("розница", re.compile(r"retail sales", re.I), +1), ("JOLTS", re.compile(r"\bJOLTS\b|job openings", re.I), +1),
         ("ставка ФРС", re.compile(r"\bFed\b.{0,40}(rate|hike|cut|hold)|FOMC", re.I), +1)]
VS = re.compile(r"(-?\d+(?:\.\d+)?)\s?%?\s*(?:vs\.?|versus|against|,)\s*(-?\d+(?:\.\d+)?)\s?%?\s*(expected|exp\.?|est\.?|estimate|forecast|consensus|expectations)", re.I)
VS2 = re.compile(r"(expected|est\.?|estimate|forecast|consensus)[:\s]*(-?\d+(?:\.\d+)?)\s?%?[^\n]{0,40}?(actual|comes in at|came in at|prints?)[:\s]*(-?\d+(?:\.\d+)?)", re.I)
TOPICS = [("ETF одобрен / приток", re.compile(r"ETF.{0,60}(approv|launch|inflow|record)|(approv|inflow).{0,60}ETF", re.I), +1),
          ("ETF отклонён / отток", re.compile(r"ETF.{0,60}(reject|delay|outflow)|(reject|outflow).{0,60}ETF", re.I), -1),
          ("взлом / эксплойт", re.compile(r"\bhack(ed|er)?\b|exploit|drained|stolen", re.I), -1),
          ("иск / SEC против", re.compile(r"\bSEC\b.{0,60}(sue|lawsuit|charge|wells notice)|(sue|lawsuit|charge).{0,40}\bSEC\b", re.I), -1),
          ("запрет / регулятор против", re.compile(r"\bban(s|ned)?\b|crackdown|prohibit", re.I), -1),
          ("банкротство / крах", re.compile(r"bankrupt|insolven|collapse|halts? withdrawals", re.I), -1),
          ("ликвидации", re.compile(r"liquidat", re.I), 0),
          ("Трамп про крипту", re.compile(r"Trump.{0,80}(crypto|bitcoin|BTC|reserve)|(crypto|bitcoin).{0,80}Trump", re.I), +1),
          ("стратегический резерв / госпокупки", re.compile(r"strategic (bitcoin )?reserve|government.{0,40}(buy|purchase).{0,40}bitcoin", re.I), +1),
          ("MicroStrategy покупает", re.compile(r"(MicroStrategy|Strategy|Saylor).{0,60}(buys?|bought|acquire|purchase)", re.I), +1),
          ("Tether / USDT риск", re.compile(r"Tether.{0,60}(depeg|investigat|probe|reserve)|USDT.{0,30}depeg", re.I), -1),
          ("тарифы / торговая война", re.compile(r"tariff", re.I), -1),
          ("ФРС мягче", re.compile(r"\bFed\b.{0,60}(cut|pause|dovish)|rate cut", re.I), +1),
          ("ФРС жёстче", re.compile(r"\bFed\b.{0,60}(hike|hawkish|higher for longer)|rate hike", re.I), -1),
          ("кит / крупный перевод", re.compile(r"whale|transferred from unknown wallet|moved.{0,40}(BTC|ETH)", re.I), 0),
          ("листинг на бирже", re.compile(r"(Binance|Coinbase|Bybit|OKX|Upbit).{0,40}(lists?|listing|will list)", re.I), +1)]

EXPWORD = re.compile(r"expect|estimate|forecast|consensus", re.I)
NUMK = r"(-?\d+(?:\.\d+)?)\s?(%|K|k|M|B)?"
EXP_PATS = [re.compile(r"(?:above|below|exceeding|topping|beating|missing|beat|versus|vs\.?|against|than)\s+(?:the\s+)?(?:market\s+)?(?:expectations?|estimates?|forecasts?|consensus)?\s*(?:of|for|at)?\s*" + NUMK, re.I),
            re.compile(NUMK + r"\s+(?:expected|estimate|forecast|consensus)", re.I),
            re.compile(r"(?:expectations?|estimates?|forecasts?|consensus)\s+(?:of|for|at)\s+(?:a\s+|an\s+)?(?:increase\s+of\s+|gain\s+of\s+|rise\s+of\s+)?" + NUMK, re.I),
            re.compile(r"the\s+" + NUMK + r"\s+(?:estimate|forecast|consensus)", re.I)]
ACT_PAT = re.compile(r"(?:rises?|rose|risen|comes?\s+in\s+at|came\s+in\s+at|hits?|holds?\s+(?:at|steady\s+at)|falls?\s+to|fell\s+to|slows?\s+to|increased?(?:\s+by)?|decreased?(?:\s+by)?|gained|dropped|rose\s+by|fell\s+by|by|at|to|of)\s+(?:just\s+)?" + NUMK, re.I)
INLINE_PAT = re.compile(r"in line with|matching|as expected|matched", re.I)


def _val(num, unit):
    v = float(num)
    return v * (1000 if unit in ("K", "k") else 1e6 if unit == "M" else 1e9 if unit == "B" else 1)


def parse_macro(head):
    """(факт, ожидание) из фразы вроде «CPI rises 3.7% YoY, exceeding the 3.6% estimate» или «Payrolls Rise by 165K, Beating 65K Forecast»."""
    exp_m, exp_pos = None, None
    for rx in EXP_PATS:
        m = rx.search(head)
        if m and (exp_pos is None or m.start() < exp_pos):
            exp_m, exp_pos = m, m.start()
    if not exp_m:
        return None
    expected = _val(exp_m.group(1), exp_m.group(2))
    before = head[:exp_pos]
    acts = [m for m in ACT_PAT.finditer(before)]
    if not acts:
        return None
    a = acts[-1]
    actual = _val(a.group(1), a.group(2))
    if INLINE_PAT.search(head[max(0, exp_pos - 30):exp_pos + 10]) and abs(actual - expected) < 1e-9:
        return None
    return actual, expected


def load_btc():
    df = pd.read_csv(ROOT / "data" / "history" / "BTCUSDT_60.csv").drop_duplicates("ts").sort_values("ts")
    ts = df["ts"].to_numpy()
    ts = ts // 1000 if ts.max() > 1e11 else ts
    return ts.astype(np.int64), df["open"].to_numpy(float), df["close"].to_numpy(float)


def react(arr, ts0, H=(1, 4, 24, 168)):
    ts, op, cl = arr
    i = int(np.searchsorted(ts, ts0, side="right"))
    if i < 2 or i + max(H) >= len(ts) or ts[i] - ts0 > 7200:
        return None
    p0 = op[i]
    out = {"jump": (p0 / op[i - 1] - 1) * 100}
    for h in H:
        out[f"f{h}"] = (cl[i + h - 1] / p0 - 1) * 100
    out["b72"] = (op[i - 1] / cl[i - 73] - 1) * 100
    return out


def main():
    c = sqlite3.connect(ROOT / "data" / "crypto_archive.db")
    n = c.execute("SELECT count(*), count(distinct channel), min(ts) FROM msgs").fetchone()
    print(f"архив крипты: {n[0]} сообщений, {n[1]} каналов, с {datetime.fromtimestamp(n[2], timezone.utc).date()}")
    btc = load_btc()
    rows = c.execute("SELECT channel, ts, text FROM msgs ORDER BY ts").fetchall()

    # ---- 1. макро с ожиданием ----
    print("\n1. МАКРО США С ОЖИДАНИЕМ ИЗ ЛЕНТЫ → реакция BTC (в сторону «мягче для ФРС = +»)")
    seen = {}
    ev = []
    for ch, ts, t in rows:
        t = t or ""
        head = t[:400]
        if not EXPWORD.search(head):
            continue
        pair = parse_macro(head)
        if not pair:
            continue
        actual, expected = pair
        kind = None
        for name, rx, hot in MACRO:
            if rx.search(head):
                kind, sign_hot = name, hot
                break
        if not kind:
            continue
        key = (kind, datetime.fromtimestamp(ts, timezone.utc).date())
        if key in seen:
            continue
        seen[key] = True
        diff = actual - expected
        soft = -sign_hot * np.sign(diff)   # +1 = мягче для ФРС (хорошо для BTC)
        r = react(btc, ts)
        if r is None:
            continue
        ev.append({"ts": ts, "kind": kind, "actual": actual, "expected": expected, "diff": diff, "soft": soft, "r": r, "half": "до 2024" if ts < SPLIT else "после 2024", "ch": ch})
    print(f"   событий с ожиданием: {len(ev)}")
    by = defaultdict(list)
    for e in ev:
        by[e["kind"]].append(e)
    print(f"   {'показатель':14} {'n':>4} " + " ".join(f"{k:>16}" for k in ("1ч", "4ч", "день", "неделя")) + "   (ход BTC в сторону «мягче = +», доля угадано)")
    for kind, lst in sorted(by.items(), key=lambda kv: -len(kv[1])):
        lst = [e for e in lst if e["soft"] != 0]
        if len(lst) < 8:
            continue
        cells = []
        for k in ("f1", "f4", "f24", "f168"):
            xs = [(e["soft"], e["r"][k]) for e in lst]
            cells.append(f"{np.mean([s * x for s, x in xs]):+.2f}% / {np.mean([(x > 0) == (s > 0) for s, x in xs]):.0%}")
        print(f"   {kind:14} {len(lst):4} " + " ".join(f"{c_:>16}" for c_ in cells))
    allm = [e for e in ev if e["soft"] != 0]
    for half in ("до 2024", "после 2024"):
        lst = [e for e in allm if e["half"] == half]
        if len(lst) >= 10:
            print(f"   все, {half:10} {len(lst):4} " + " ".join(f"{np.mean([e['soft'] * e['r'][k] for e in lst]):+.2f}% / {np.mean([(e['r'][k] > 0) == (e['soft'] > 0) for e in lst]):.0%}".rjust(16) for k in ("f1", "f4", "f24", "f168")))
    # размер: большие расхождения (|diff| ≥ 0.2 п.п. для % показателей)
    big = [e for e in allm if abs(e["diff"]) >= 0.2 and e["kind"] not in ("NFP", "JOLTS")]
    if len(big) >= 8:
        print(f"   большие расхождения (≥0.2 п.п.) n={len(big)}: " + " ".join(f"{k}: {np.mean([e['soft'] * e['r'][k] for e in big]):+.2f}% / {np.mean([(e['r'][k] > 0) == (e['soft'] > 0) for e in big]):.0%}" for k in ("f1", "f4", "f24", "f168")))

    # ---- 2. темы по словам ----
    print("\n2. ЗНАК НОВОСТИ ПО СЛОВАМ → направление BTC (угадано %, средний ход «в знак»), до | после 2024")
    topic_ev = defaultdict(list)
    last_ts = defaultdict(int)
    for ch, ts, t in rows:
        head = (t or "")[:300]
        for name, rx, sign in TOPICS:
            if rx.search(head):
                if ts - last_ts[name] < 1800:
                    continue   # серия = одно событие
                last_ts[name] = ts
                r = react(btc, ts)
                if r:
                    topic_ev[name].append((sign, r, ts))
    print(f"   {'тема':32} {'n до/после':>11} " + " ".join(f"{k:>18}" for k in ("1ч", "4ч", "день", "неделя")) + "   |ход 4ч|")
    for name, lst in sorted(topic_ev.items(), key=lambda kv: -len(kv[1])):
        sign = lst[0][0]
        bef = [x for x in lst if x[2] < SPLIT]
        aft = [x for x in lst if x[2] >= SPLIT]
        cells = []
        for k in ("f1", "f4", "f24", "f168"):
            parts = []
            for grp in (bef, aft):
                if len(grp) < 15 or sign == 0:
                    parts.append("   —   ")
                else:
                    parts.append(f"{np.mean([(r[k] > 0) == (sign > 0) for _, r, _ in grp]):.0%}/{np.mean([sign * r[k] for _, r, _ in grp]):+.1f}")
            cells.append(f"{parts[0]:>8}|{parts[1]:>8}")
        print(f"   {name:32} {f'{len(bef)}/{len(aft)}':>11} " + " ".join(cells) + f"   {np.mean([abs(r['f4']) for _, r, _ in lst]):.2f}%")
    base = np.abs(np.diff(btc[2][::4]) / btc[2][:-1:4] * 100)
    print(f"   фон: |ход BTC за 4ч| {base.mean():.2f}%")

    # ---- 3. шоки: продолжение за первым часом ----
    print("\n3. ПРОДОЛЖЕНИЕ ЗА ПЕРВЫМ ЧАСОМ после новости с темой (|1ч| ≥ 1%): в ту же сторону до 4ч / дня / недели")
    allt = [(r, ts) for lst in topic_ev.values() for _, r, ts in lst if abs(r["f1"]) >= 1.0]
    for half in ("до 2024", "после 2024"):
        sel = [r for r, ts in allt if (ts < SPLIT) == (half == "до 2024")]
        if len(sel) >= 20:
            print(f"   {half:10} n={len(sel):4}  " + "  ".join(f"→{lab} {np.mean([(r[k] - r['f1'] > 0) == (r['f1'] > 0) for r in sel]):.0%} / {np.mean([np.sign(r['f1']) * (r[k] - r['f1']) for r in sel]):+.2f}%"
                                                              for k, lab in (("f4", "4ч"), ("f24", "день"), ("f168", "неделя"))))


if __name__ == "__main__":
    main()
