"""Ожидание против факта — поштучно, без словарного усреднения.

Вопрос пользователя 07.11.2026: «новость не может не влиять; учитывал ли ты, что уже заложено в цене?» Здесь каждое событие
берётся отдельно, и к нему ищется ОЖИДАНИЕ:
1. Решения ЦБ по ключевой ставке: факт — из сообщений 13:25–13:45 МСК; ожидание — ставка, которую чаще всего называли
   в сообщениях «консенсус / опрос / ожидают / прогноз» за 7 дней до. Сюрприз = факт − ожидание (п.п.). Плюс словесный
   сюрприз в первые 2 часа после («неожиданно», «вопреки», «сюрприз» против «ожидаемо», «совпало с консенсусом»).
   Реакция: индекс Мосбиржи и Сбер — скачок (открытие 14:00 к открытию 13:00), час, до конца дня, 3 дня; ход за 3 дня до.
2. Пакеты санкций (ЕС 1–19, ключевые США) и переломные даты — список руками, дата проверяется по архиву (первое упоминание).
   Ожидание = сколько сообщений о «новом пакете / готовят санкции» было за 7 дней до (обсуждали — значит ждали),
   и ход индекса за 10 и 3 дня до. Реакция индекса: первая свеча, 4 часа, день, 3 дня.
3. Дивиденды: факт — «рекомендовал … дивиденды … X руб», ожидание — число из сообщений «ожида/прогноз/консенсус … дивиденд … Y руб»
   по той же компании за 45 дней до. Сюрприз = (X−Y)/Y. Реакция бумаги сверх индекса.
Запуск из корня: python research/news_expectations.py [--since 2019-01-01]. Модель не вызывается, только архив и свечи."""
import argparse
import json
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from research.news_psychology import NAME_RX, load_hist  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parents[1]
MSK = ZoneInfo("Europe/Moscow")
FOREIGN = re.compile(r"Турц|ЕЦБ|ФРС|Федрезерв|Бразил|Инди|Украин|Казахстан|Белорус|Банк Англии|Швеци|Норвег|Японии|Китая|Европ", re.I)
DEC = re.compile(r"(Банк России|ЦБ|Центробанк|ЦБ РФ|ЦБР|регулятор)[^.\n]{0,50}(сохранил|повысил|снизил|оставил|поднял|опустил|понизил|повышает|снижает|сохраняет)[^.\n]{0,60}ключев", re.I)
RATE = re.compile(r"(\d{1,2})[.,](\d{1,2})\s?%")
EXP = re.compile(r"(консенсус|опрос|ожида\w+|прогноз\w*|большинств\w+|аналитик\w+)", re.I)
SURPR = re.compile(r"неожидан|вопреки|сюрприз|не ожидал|шок", re.I)
ASEXP = re.compile(r"ожидаемо|как и ожидал|совпал\w* с (консенсус|прогноз|ожидан)|в соответствии с (прогноз|ожидан)|в рамках ожидан", re.I)
SIGNAL_HAWK = re.compile(r"(допускает|будет оценивать целесообразность)[^.]{0,40}повышени|жёсткий сигнал|жесткий сигнал|ужесточ", re.I)
SIGNAL_DOVE = re.compile(r"(допускает|будет оценивать целесообразность)[^.]{0,40}снижени|мягкий сигнал|смягч", re.I)


def ts_msk(y, m, d, hh=0, mm=0):
    return int(datetime(y, m, d, hh, mm, tzinfo=MSK).timestamp())


def fmt(x):
    return "   —  " if x is None else f"{x:+6.2f}"


def move(arr, ts0, horizons=(1, 4, 9, 27), back=(27, 90), pre_open=False):
    """Ход от открытия первой свечи после ts0 (в %): вперёд по закрытию h-й свечи, назад от закрытия b-й свечи до.
    pre_open=True (решение ЦБ в 13:30 внутри свечи 13:00): скачок считается от ОТКРЫТИЯ предыдущей свечи, т.е. от цены до решения."""
    ts, op, cl = arr
    i = int(np.searchsorted(ts, ts0, side="right"))
    if i >= len(ts) or i - max(back) < 0 or ts[i] - ts0 > 5 * 86400:
        return None
    p0 = op[i]
    # цена ДО новости: если новость пришла внутри торговой свечи i-1 (ts0 внутри её часа), то её открытие; иначе закрытие последней свечи до
    inside = ts[i - 1] <= ts0 < ts[i - 1] + 3600
    base = op[i - 1] if (pre_open or inside) else cl[i - 1]
    out = {"lag_h": round((ts[i] - ts0) / 3600, 1), "jump": (p0 / base - 1) * 100}
    for h in horizons:
        out[f"f{h}"] = (cl[i + h - 1] / p0 - 1) * 100 if i + h - 1 < len(ts) else None
    for b in back:
        out[f"b{b}"] = (cl[i - 1] / cl[i - 1 - b] - 1) * 100
    # до конца торгового дня (по МСК)
    day = datetime.fromtimestamp(ts[i], MSK).date()
    j = i
    while j + 1 < len(ts) and datetime.fromtimestamp(ts[j + 1], MSK).date() == day:
        j += 1
    out["day"] = (cl[j] / p0 - 1) * 100
    return out


def load_msgs(c, since_ts, like):
    return c.execute("SELECT channel, ts, text FROM msgs WHERE ts>=? AND text LIKE ? ORDER BY ts", (since_ts, like)).fetchall()


# ----------------------------------------------------------------------------------------------------------- 1. ЦБ
def load_official():
    """Официальная ключевая ставка по дням (cbr.ru/hd_base/KeyRate) и ROISfix — фиксинг свопов на RUONIA (НФА, архив cbr.ru,
    12:30 МСК, до 01.03.2022). ROISfix 1M на день решения = что рынок закладывал ДО решения; сдвиг к следующему дню = рыночный сюрприз."""
    key = {}
    p = ROOT / "data" / "key_rate_daily.csv"
    if p.exists():
        for r in pd.read_csv(p).itertuples():
            key[str(r.date)] = float(r.rate)
    rois = {}
    p = ROOT / "data" / "roisfix.csv"
    if p.exists():
        for r in pd.read_csv(p).itertuples():
            rois[str(r.date)] = {"w1": float(r.w1), "w2": float(r.w2), "m1": float(r.m1), "m3": float(r.m3)}
    return key, rois


def official_at(series, day, step=1, tries=10):
    d = day
    for _ in range(tries):
        v = series.get(str(d))
        if v is not None:
            return v
        d = d + timedelta(days=step)
    return None


def cb_decisions(c, since_ts, hist):
    key, rois = load_official()
    rows = load_msgs(c, since_ts, "%ключев%")
    byday = defaultdict(list)
    for ch, ts, t in rows:
        t = t or ""
        if FOREIGN.search(t[:120]):
            continue
        d = datetime.fromtimestamp(ts, timezone.utc).astimezone(MSK)
        if DEC.search(t) and (d.hour, d.minute) >= (13, 25) and (d.hour, d.minute) <= (14, 30):
            byday[d.date()].append((ts, ch, t))
    # экстренные решения вне 13:30
    extra = {datetime(2022, 2, 28).date(): "20", datetime(2023, 8, 15).date(): "12"}
    decisions = []
    for day, lst in sorted(byday.items()):
        if len(lst) < 3 and day not in extra:
            continue
        rates = Counter()
        for ts, ch, t in lst:
            for a, b in RATE.findall(t[:300]):
                rates[float(f"{a}.{b}")] += 1
        if not rates:
            continue
        actual = rates.most_common(1)[0][0]
        t0 = min(ts for ts, _, _ in lst)
        # ожидание: 7 дней до, сообщения про ставку с прогнозом; берём только уровни ставки (не «на 0,25 п.п.»), близкие к действующей
        cur = official_at(key, day, step=-1)
        exp = Counter()
        exp_msgs = c.execute("SELECT text FROM msgs WHERE ts BETWEEN ? AND ? AND text LIKE '%ключев%'", (t0 - 7 * 86400, t0 - 600)).fetchall()
        for (t,) in exp_msgs:
            t = t or ""
            if FOREIGN.search(t[:120]) or not EXP.search(t[:400]):
                continue
            for m in re.finditer(r"(ожида\w+|прогноз\w*|консенсус\w*|большинств\w+)[^.\n]{0,120}?(до|на уровне|уровн\w*|составит|в|к)\s(\d{1,2})[.,]?(\d{0,2})\s?%", t[:600], re.I):
                v = float(f"{m.group(3)}.{m.group(4) or 0}")
                if cur is None or abs(v - cur) <= 3.0:
                    exp[v] += 1
                    break
        expected = exp.most_common(1)[0][0] if exp else None
        # словесный сюрприз после
        after = c.execute("SELECT text FROM msgs WHERE ts BETWEEN ? AND ? AND text LIKE '%ставк%'", (t0, t0 + 2 * 3600)).fetchall()
        s_words = sum(1 for (t,) in after if SURPR.search(t or "") and not FOREIGN.search((t or "")[:120]))
        e_words = sum(1 for (t,) in after if ASEXP.search(t or "") and not FOREIGN.search((t or "")[:120]))
        hawk = sum(1 for (t,) in after if SIGNAL_HAWK.search(t or ""))
        dove = sum(1 for (t,) in after if SIGNAL_DOVE.search(t or ""))
        prev = decisions[-1]["actual"] if decisions else None
        # официальные данные: ставка до решения = серия в день решения, после = через 5 дней (новая действует с понедельника)
        off_prev = official_at(key, day, step=-1)
        off_new = official_at(key, day + timedelta(days=5), step=1)
        if off_prev is not None:
            prev = off_prev
        if off_new is not None:
            actual = off_new
        # рынок: ROISfix 1M в день решения (фиксинг 12:30, до решения) и на следующий день
        r0 = official_at(rois, day, step=-1, tries=5)
        r1 = official_at(rois, day + timedelta(days=1), step=1, tries=5)
        mkt_before = r0["m1"] if r0 else None  # что закладывал рынок на месяц вперёд
        mkt_shift = round(r1["m1"] - r0["m1"], 2) if r0 and r1 else None  # сдвиг ожиданий после решения = рыночный сюрприз
        rec = {"date": str(day), "ts": t0, "actual": actual, "prev": prev, "expected": expected, "n_exp": sum(exp.values()),
               "surprise_pp": None if expected is None else round(actual - expected, 2), "surpr_words": s_words, "asexp_words": e_words,
               "hawk": hawk, "dove": dove, "n_msgs": len(lst), "roisfix_1m_before": mkt_before, "roisfix_shift": mkt_shift,
               "mkt_priced": None if mkt_before is None else round(mkt_before - prev, 2)}
        for sym in ("IMOEX", "SBER", "RGBI"):
            # отсчёт от открытия свечи 14:00 (первая после решения), скачок — от открытия свечи 13:00 (цена до решения)
            rec[sym] = move(hist[sym], t0 + 600, horizons=(1, 4, 27, 45, 90, 180), back=(27,), pre_open=True) if sym in hist else None
        # облигации как измеритель сюрприза: RGBI за 4 часа после решения (падение = жёстче ожиданий, рост = мягче)
        rec["rgbi_4h"] = round(rec["RGBI"]["jump"] + rec["RGBI"]["f4"], 2) if rec.get("RGBI") else None
        decisions.append(rec)
    return decisions


def cb_report(decisions):
    print(f"\n1. РЕШЕНИЯ ЦБ ПО СТАВКЕ: {len(decisions)} решений, с найденным ожиданием {sum(1 for d in decisions if d['expected'] is not None)}")
    print("   реакция индекса: скачок = открытие свечи после решения к закрытию предыдущей; 1ч, 4ч, день — от открытия; «до» — 3 дня до")
    print("   «своп до» = ROISfix 1M за день до минус действующая ставка (что рынок закладывал); «сдвиг» = ROISfix 1M на следующий день минус до (рыночный сюрприз)")
    print(f"   {'дата':10} {'было':>5} {'ждали':>5} {'факт':>5} {'сюрп':>5} слова    {'своп до':>7} {'сдвиг':>6} {'RGBI4ч':>6} {'до3д':>6} {'скач':>6} {'1ч':>6} {'4ч':>6} {'день':>6} {'3д':>6} | Сбер день")
    groups = defaultdict(list)
    for d in decisions:
        mv = d["IMOEX"]
        if not mv:
            continue
        sp = d["surprise_pp"]
        words = ("СЮРПРИЗ" if d["surpr_words"] > d["asexp_words"] and d["surpr_words"] >= 2 else "ожидаемо" if d["asexp_words"] >= 2 else "?")
        sb = d["SBER"]["day"] if d["SBER"] else None
        mp, ms = d.get("mkt_priced"), d.get("roisfix_shift")
        print(f"   {d['date']} {fmt(d['prev']) if d['prev'] else '   — ':>5} {fmt(d['expected']) if d['expected'] is not None else '   — ':>5} {d['actual']:5.2f} "
              f"{fmt(sp) if sp is not None else '   — ':>5} {words:8} {fmt(mp) if mp is not None else '    —  ':>7} {fmt(ms) if ms is not None else '   —  ':>6} "
              f"{fmt(d.get('rgbi_4h'))} {fmt(mv['b27'])} {fmt(mv['jump'])} {fmt(mv['f1'])} {fmt(mv['f4'])} {fmt(mv['day'])} "
              f"{fmt(mv.get('f27')) if mv.get('f27') is not None else '   —  '} | {fmt(sb)}")
        if sp is not None:
            key = "ставка ниже ожиданий (мягче)" if sp <= -0.25 else "ставка выше ожиданий (жёстче)" if sp >= 0.25 else "как ждали"
            groups[key].append((mv, sb, d))
        if ms is not None:
            key = "РЫНОК (своп): жёстче ожиданий" if ms >= 0.15 else "РЫНОК (своп): мягче ожиданий" if ms <= -0.15 else "РЫНОК (своп): как закладывал"
            groups[key].append((mv, sb, d))
        rg = d.get("rgbi_4h")
        if rg is not None:
            key = "ОБЛИГАЦИИ: жёстче (RGBI 4ч ≤ −0.15%)" if rg <= -0.15 else "ОБЛИГАЦИИ: мягче (RGBI 4ч ≥ +0.15%)" if rg >= 0.15 else "ОБЛИГАЦИИ: без сюрприза"
            groups[key].append((mv, sb, d))
        if words == "СЮРПРИЗ":
            groups["словами: сюрприз"].append((mv, sb, d))
        elif words == "ожидаемо":
            groups["словами: ожидаемо"].append((mv, sb, d))
        if d["prev"] is not None:
            chg = d["actual"] - d["prev"]
            groups["снизил" if chg < 0 else "повысил" if chg > 0 else "сохранил"].append((mv, sb, d))
    print("\n   ИТОГ по группам (индекс, средний ход; в скобках доля вверх):")
    out = {}
    for k, lst in groups.items():
        def agg(key, src="IMOEX"):
            xs = [(m[key] if src == "IMOEX" else s) for m, s, _ in lst if (m.get(key) if src == "IMOEX" else s) is not None]
            return (f"{np.mean(xs):+.2f}% ({np.mean([x > 0 for x in xs]):.0%})" if xs else "—")
        print(f"   {k:38} n={len(lst):3}  до3д {agg('b27')}  скачок {agg('jump')}  1ч {agg('f1')}  4ч {agg('f4')}  день {agg('day')}  3д {agg('f27')}  "
              f"нед {agg('f45')}  2нед {agg('f90')}  мес {agg('f180')}  | Сбер день {agg('day', 'SBER')}")
        out[k] = {"n": len(lst), "day": agg("day"), "f27": agg("f27"), "f45": agg("f45"), "f180": agg("f180")}
    # совпадение знака: сюрприз-мягче => вверх?
    xs = [(d["surprise_pp"], m["day"]) for m, s, d in sum(groups.values(), []) if d["surprise_pp"] not in (None, 0)]
    if xs:
        hit = np.mean([(sp < 0) == (r > 0) for sp, r in xs])
        print(f"   знак дня индекса = против знака сюрприза (мягче → вверх): {hit:.0%} на n={len(xs)}")
    ms = [(d["roisfix_shift"], d["IMOEX"]["day"], d["IMOEX"]["f4"]) for d in decisions if d["IMOEX"] and d.get("roisfix_shift") is not None and abs(d["roisfix_shift"]) >= 0.1]
    if ms:
        print(f"   по свопам (|сдвиг| ≥ 0.10): индекс за день против сдвига ставок {np.mean([(s > 0) == (r < 0) for s, r, _ in ms]):.0%}, "
              f"за 4ч {np.mean([(s > 0) == (r4 < 0) for s, _, r4 in ms]):.0%} на n={len(ms)}")
    rg = [(d["rgbi_4h"], d["IMOEX"]["day"], d["IMOEX"]["f4"], d["IMOEX"].get("f27")) for d in decisions if d["IMOEX"] and d.get("rgbi_4h") is not None and abs(d["rgbi_4h"]) >= 0.15]
    if rg:
        print(f"   по облигациям (|RGBI 4ч| ≥ 0.15%): индекс в ту же сторону, что облигации — за 4ч {np.mean([(g > 0) == (r4 > 0) for g, _, r4, _ in rg]):.0%}, "
              f"за день {np.mean([(g > 0) == (r > 0) for g, r, _, _ in rg]):.0%}, за 3 дня {np.mean([(g > 0) == (r3 > 0) for g, _, _, r3 in rg if r3 is not None]):.0%} на n={len(rg)}")
    return out


# ------------------------------------------------------------------------------------------------ 2. САНКЦИИ И ДАТЫ
SANCTIONS = [
    ("США: санкции за Скрипалей (2-й раунд)", 2019, 8, 2, 20, 0), ("США: санкции на первичный ОФЗ", 2021, 4, 15, 16, 0),
    ("США: признание ЛДНР, первые санкции", 2022, 2, 22, 20, 0), ("ЕС пакет 1", 2022, 2, 23, 18, 0), ("США: SDN Сбер/ВТБ, ЕС пакет 2", 2022, 2, 24, 22, 0),
    ("ЕС пакет 4 (рынок закрыт)", 2022, 3, 15, 12, 0), ("ЕС пакет 5", 2022, 4, 8, 12, 0), ("ЕС пакет 6 (нефть)", 2022, 6, 3, 12, 0),
    ("ЕС пакет 7", 2022, 7, 21, 12, 0), ("ЕС пакет 8", 2022, 10, 6, 12, 0), ("ЕС пакет 9", 2022, 12, 16, 12, 0), ("ЕС пакет 10", 2023, 2, 25, 12, 0),
    ("ЕС пакет 11", 2023, 6, 23, 12, 0), ("ЕС пакет 12", 2023, 12, 18, 12, 0), ("ЕС пакет 13", 2024, 2, 23, 12, 0),
    ("США: санкции на Мосбиржу/НКЦ", 2024, 6, 12, 19, 0), ("ЕС пакет 14", 2024, 6, 24, 12, 0), ("США: Газпромбанк", 2024, 11, 21, 19, 0),
    ("ЕС пакет 15", 2024, 12, 16, 12, 0), ("США: Газпромнефть/Сургут, танкеры", 2025, 1, 10, 20, 0), ("ЕС пакет 16", 2025, 2, 24, 12, 0),
    ("ЕС пакет 17", 2025, 5, 20, 12, 0), ("ЕС пакет 18", 2025, 7, 18, 12, 0), ("США: Роснефть/Лукойл", 2025, 10, 22, 22, 0), ("ЕС пакет 19", 2025, 10, 23, 10, 0),
]
KEYDATES = [
    ("ОПЕК+ развал сделки (нефть −30%)", 2020, 3, 6, 18, 0), ("ОПЕК++ новая сделка", 2020, 4, 12, 20, 0), ("COVID: локдаун в Москве", 2020, 3, 26, 14, 0),
    ("Признание ЛДНР", 2022, 2, 21, 22, 0), ("Начало СВО", 2022, 2, 24, 6, 0), ("Открытие торгов после паузы", 2022, 3, 24, 10, 0),
    ("ЦБ экстренно 20%", 2022, 2, 28, 9, 30), ("Газпром: отмена дивидендов", 2022, 6, 30, 11, 0), ("Мобилизация", 2022, 9, 21, 9, 0),
    ("Референдумы/присоединение", 2022, 9, 30, 15, 0), ("Мятеж Пригожина", 2023, 6, 24, 0, 0), ("ЦБ экстренно 12%", 2023, 8, 15, 10, 30),
    ("Атака ХАМАС на Израиль", 2023, 10, 7, 10, 0), ("Вторжение в Курскую область", 2024, 8, 6, 9, 0), ("Победа Трампа", 2024, 11, 6, 9, 0),
    ("Звонок Трамп–Путин (первый)", 2025, 2, 12, 19, 0), ("Переговоры в Эр-Рияде", 2025, 2, 18, 12, 0), ("Звонок Трамп–Путин 18.03", 2025, 3, 18, 19, 0),
    ("Стамбул, переговоры делегаций", 2025, 5, 16, 12, 0), ("Удар Израиля по Ирану", 2025, 6, 13, 6, 0), ("Саммит на Аляске", 2025, 8, 15, 22, 0),
    ("Звонок Трамп–Путин, Будапешт", 2025, 10, 16, 19, 0), ("Отмена Будапешта", 2025, 10, 21, 22, 0),
]
ANTIC = re.compile(r"(нов\w+|очередн\w+|\d{1,2}-?[йм]?)\s+пакет\w*\s+санкц|готов\w+[^.]{0,40}санкц|обсужда\w+[^.]{0,40}санкц|санкц\w+[^.]{0,60}(готов|обсужда|могут ввести|планиру)", re.I)


def dated_events(c, hist, lst, kind):
    out = []
    idx = hist["IMOEX"]
    for name, y, m, d, hh, mm in lst:
        t0 = ts_msk(y, m, d, hh, mm)
        # первое упоминание в архиве ±1 день (слово из названия)
        key = {"санкц": "%санкц%"}.get("санкц") if kind == "sanctions" else None
        antic = c.execute("SELECT count(*) FROM msgs WHERE ts BETWEEN ? AND ? AND text LIKE '%санкц%'", (t0 - 7 * 86400, t0)).fetchone()[0] if kind == "sanctions" else None
        antic_strong = 0
        if kind == "sanctions":
            for (t,) in c.execute("SELECT text FROM msgs WHERE ts BETWEEN ? AND ? AND text LIKE '%санкц%'", (t0 - 7 * 86400, t0)).fetchall():
                if ANTIC.search(t or ""):
                    antic_strong += 1
        total_day = c.execute("SELECT count(*) FROM msgs WHERE ts BETWEEN ? AND ?", (t0, t0 + 86400)).fetchone()[0]
        mv = move(idx, t0, horizons=(1, 4, 9, 27, 45, 90, 180), back=(27, 90))
        out.append({"name": name, "date": f"{y}-{m:02d}-{d:02d} {hh:02d}:{mm:02d}", "ts": t0, "antic_msgs": antic, "antic_strong": antic_strong,
                    "msgs_24h": total_day, "mv": mv})
    return out


def dated_report(title, evs, show_antic):
    print(f"\n{title}")
    hdr = f"   {'событие':40} {'дата':16} " + (f"{'обсужд/7д':>9} {'явно':>5} " if show_antic else "") + f"{'до10д':>6} {'до3д':>6} {'1св':>6} {'4ч':>6} {'день':>6} {'3д':>6} {'нед':>6} {'2нед':>6} {'мес':>6}  лаг,ч"
    print(hdr)
    for e in evs:
        mv = e["mv"]
        if not mv:
            print(f"   {e['name']:40} {e['date']:16} свечей нет (биржа закрыта / до начала истории)")
            continue
        a = f"{e['antic_msgs']:>9} {e['antic_strong']:>5} " if show_antic else ""
        print(f"   {e['name']:40} {e['date']:16} {a}{fmt(mv['b90'])} {fmt(mv['b27'])} {fmt(mv['f1'])} {fmt(mv['f4'])} {fmt(mv['day'])} {fmt(mv.get('f27'))} "
              f"{fmt(mv.get('f45'))} {fmt(mv.get('f90'))} {fmt(mv.get('f180'))}  {mv['lag_h']}")
    ok = [e for e in evs if e["mv"]]
    if ok:
        for key, lab in (("b27", "до 3д"), ("f1", "1 свеча"), ("f4", "4ч"), ("day", "день"), ("f27", "3д"), ("f45", "неделя"), ("f90", "2 недели"), ("f180", "месяц")):
            xs = [e["mv"][key] for e in ok if e["mv"].get(key) is not None]
            print(f"   среднее {lab:8}: {np.mean(xs):+.2f}%  медиана {np.median(xs):+.2f}%  вниз в {np.mean([x < 0 for x in xs]):.0%}  (n={len(xs)})")
        if show_antic:
            lo = [e for e in ok if e["antic_strong"] < np.median([x["antic_strong"] for x in ok])]
            hi = [e for e in ok if e not in lo]
            for lab, grp in (("мало обсуждали до (сюрприз)", lo), ("много обсуждали до (ждали)", hi)):
                def m(key):
                    xs = [e["mv"][key] for e in grp if e["mv"].get(key) is not None]
                    return f"{np.mean(xs):+.2f}%" if xs else "—"
                print(f"   {lab:30}: n={len(grp)} до3д {m('b27')}  день {m('day')}  3д {m('f27')}  нед {m('f45')}  мес {m('f180')}")


# ---------------------------------------------------------------------------------------------------- 3. ДИВИДЕНДЫ
NUM = r"(?<![\d,.])(\d{1,3}(?:[  ]\d{3})*(?:[.,]\d{1,4})?)"  # «16 665,2», «25», «0,000828802» — без обрезания тысяч
REC = re.compile(r"рекомендова\w*[^.\n]{0,120}?дивиденд\w*[^.\n]{0,60}?(в размере|на акцию[:\s–-]*|по|–|-|—)\s?" + NUM + r"\s?(руб|₽)", re.I)
REC2 = re.compile(r"дивиденд\w*[^.\n]{0,40}?(в размере|на акцию[:\s–-]*)\s?" + NUM + r"\s?(руб|₽)[^.\n]{0,80}?рекомендова", re.I)
NOREC = re.compile(r"(не выплачивать|не рекомендова|отказ\w+ от (выплаты )?дивиденд|без дивиденд|дивиденды не будут|отмен\w+ дивиденд)", re.I)
DEXP = re.compile(r"(ожида\w+|прогноз\w*|консенсус\w*|может составить|оценива\w+|рассчитыва\w+)[^.\n]{0,100}?дивиденд\w*[^.\n]{0,60}?(в размере|на акцию[:\s–-]*|около|порядка|до|–|-|—)?\s?" + NUM + r"\s?(руб|₽)", re.I)
DEXP2 = re.compile(r"дивиденд\w*[^.\n]{0,80}?(ожида\w+|прогноз\w*|консенсус\w*|может составить|оценива\w+)[^.\n]{0,60}?(в размере|около|порядка|до|–|-|—)?\s?" + NUM + r"\s?(руб|₽)", re.I)
HASHTAG = re.compile(r"#([A-Z]{3,5})\b")


def company_of(text):
    """Компания новости: хэштег #SBER важнее названия; при двух разных названиях в первых 300 символах — не берём."""
    t = (text or "")[:300]
    tags = [h for h in HASHTAG.findall(t) if h in NAME_RX]
    if len(set(tags)) == 1:
        return tags[0]
    found = [k for k, rx in NAME_RX.items() if rx.search(t)]
    return found[0] if len(found) == 1 else None


def num(s):
    return float(s.replace(" ", "").replace(" ", "").replace(",", "."))


def dividends(c, since_ts, hist):
    rows = load_msgs(c, since_ts, "%дивиденд%")
    recs = []  # (ts, sym, amount)
    seen = set()
    for ch, ts, t in rows:
        t = t or ""
        sym = company_of(t)
        if not sym or sym not in hist:
            continue
        m = REC.search(t[:500]) or REC2.search(t[:500])
        if m and not NOREC.search(t[:300]):
            amt = num(m.group(2))
            key = (sym, round(amt, 2))
            # одно событие = первая публикация этой суммы по компании за 30 дней
            if any(k == key and ts - t1 < 30 * 86400 for k, t1 in seen):
                continue
            seen.add((key, ts))
            recs.append((ts, sym, amt, False, t[:160].replace("\n", " ")))
        elif NOREC.search(t[:300]) and re.search(r"совет директоров|СД |рекомендова", t[:300], re.I):
            key = (sym, "none")
            if any(k == key and ts - t1 < 30 * 86400 for k, t1 in seen):
                continue
            seen.add((key, ts))
            recs.append((ts, sym, None, True, t[:160].replace("\n", " ")))
    # ожидания: заранее разбираем все сообщения с прогнозом дивиденда по компаниям (один проход), потом окна по bisect
    import bisect
    exp_by_sym = defaultdict(list)  # sym -> [(ts, value)]
    for ch, ts, t in rows:
        t = t or ""
        m = DEXP.search(t[:600])
        v = m.group(3) if m else None
        if not m:
            m2 = DEXP2.search(t[:600])
            v = m2.group(3) if m2 else None
        if not v:
            continue
        sym = company_of(t)
        if sym:
            exp_by_sym[sym].append((ts, round(num(v), 2)))
    exp_ts = {s: [x[0] for x in lst] for s, lst in exp_by_sym.items()}
    out = []
    for ts, sym, amt, cancel, snippet in recs:
        exp = Counter()
        lst = exp_by_sym.get(sym, [])
        if lst:
            lo = bisect.bisect_left(exp_ts[sym], ts - 45 * 86400)
            hi = bisect.bisect_left(exp_ts[sym], ts - 3600)
            for _, v in lst[lo:hi]:
                exp[v] += 1
        expected = exp.most_common(1)[0][0] if exp else None
        # ожидание должно быть того же порядка, что факт (иначе это цена акции или другая бумага)
        if expected is not None and amt is not None and not (0.2 <= amt / expected <= 5):
            expected = None
        H = (1, 4, 9, 27, 45, 90, 180)
        mv = move(hist[sym], ts, horizons=H, back=(27, 90))
        mi = move(hist["IMOEX"], ts, horizons=H, back=(27, 90))
        if not mv or not mi:
            continue
        rel = {k: (mv[k] - mi[k]) if mv.get(k) is not None and mi.get(k) is not None else None for k in ("jump", "f1", "f4", "day", "f27", "f45", "f90", "f180", "b27", "b90")}
        surprise = None
        if cancel:
            surprise = -1.0
        elif expected and amt is not None:
            surprise = (amt - expected) / expected
        out.append({"ts": ts, "date": datetime.fromtimestamp(ts, MSK).strftime("%Y-%m-%d %H:%M"), "sym": sym, "amount": amt, "cancel": cancel,
                    "expected": expected, "n_exp": sum(exp.values()), "surprise": None if surprise is None else round(surprise, 2), "rel": rel, "snippet": snippet})
    return out


def div_report(divs):
    withexp = [d for d in divs if d["surprise"] is not None]
    print(f"\n3. ДИВИДЕНДЫ: рекомендаций найдено {len(divs)}, с ожиданием или отменой {len(withexp)} (бумага сверх индекса, %)")
    groups = {"отмена / не платить": [d for d in withexp if d["cancel"]],
              "сильно выше ожиданий (>+15%)": [d for d in withexp if not d["cancel"] and d["surprise"] > 0.15],
              "около ожиданий (±15%)": [d for d in withexp if not d["cancel"] and -0.15 <= d["surprise"] <= 0.15],
              "ниже ожиданий (<−15%)": [d for d in withexp if not d["cancel"] and d["surprise"] < -0.15],
              "ожидание не найдено": [d for d in divs if d["surprise"] is None and not d["cancel"]]}
    print(f"   {'группа':32} {'n':>4} {'разгон10д':>9} {'до3д':>6} {'скачок':>6} {'1ч':>6} {'4ч':>6} {'день':>6} {'3д':>6} {'нед':>6} {'2нед':>6} {'мес':>6}  вверх(день/нед/мес)")
    for k, lst in groups.items():
        if not lst:
            continue
        def agg(key):
            xs = [d["rel"][key] for d in lst if d["rel"].get(key) is not None]
            return np.mean(xs) if xs else None
        def ups(key):
            xs = [d["rel"][key] for d in lst if d["rel"].get(key) is not None]
            return f"{np.mean([x > 0 for x in xs]):.0%}" if xs else "—"
        print(f"   {k:32} {len(lst):4} {fmt(agg('b90')):>9} {fmt(agg('b27'))} {fmt(agg('jump'))} {fmt(agg('f1'))} {fmt(agg('f4'))} {fmt(agg('day'))} {fmt(agg('f27'))} "
              f"{fmt(agg('f45'))} {fmt(agg('f90'))} {fmt(agg('f180'))}  {ups('day')}/{ups('f45')}/{ups('f180')}")
    # разгон × сюрприз
    print("\n   разгон за 10 дней × сюрприз (сверх индекса):")
    for lab, cond in (("разгон 4%+", lambda d: d["rel"]["b90"] >= 4), ("без разгона", lambda d: d["rel"]["b90"] < 4)):
        for k in ("сильно выше ожиданий (>+15%)", "около ожиданий (±15%)", "ниже ожиданий (<−15%)", "отмена / не платить"):
            lst = [d for d in groups[k] if cond(d)]
            if len(lst) >= 5:
                def m(key):
                    xs = [d["rel"][key] for d in lst if d["rel"].get(key) is not None]
                    return f"{np.mean(xs):+.2f}% ({np.mean([x > 0 for x in xs]):.0%})" if xs else "—"
                print(f"     {lab:12} × {k:30} n={len(lst):3} день {m('day')}  3д {m('f27')}  нед {m('f45')}  мес {m('f180')}")
    print("\n   самые крупные сюрпризы (для проверки глазами):")
    big = sorted([d for d in withexp if not d["cancel"]], key=lambda d: -abs(d["surprise"]))[:15] + [d for d in withexp if d["cancel"]][:10]
    for d in big:
        print(f"     {d['date']} {d['sym']:5} факт {d['amount']} ждали {d['expected']} (n={d['n_exp']}) сюрприз {d['surprise']:+.0%}  разгон10д {fmt(d['rel']['b90'])} день {fmt(d['rel']['day'])} 3д {fmt(d['rel']['f27'])}  | {d['snippet'][:90]}")


# ------------------------------------------------------------------------------- 4. ТЕМЫ НА ДЛИННЫХ ГОРИЗОНТАХ
def topics_long(since_ts, split_ts, hist):
    """Знак по словам (как в news_event_study) против хода индекса за 3 дня, неделю, 2 недели, месяц. Вопрос пользователя:
    может, новость работает не за часы, а за недели. События = сообщения темы ближе 30 мин, только темы «на весь рынок» со знаком."""
    from research.news_event_study import TOPICS, build_events, load_msgs as load_topic_msgs
    mw_topics = {name for name, mw, _, _ in TOPICS if mw}
    items = load_topic_msgs(since_ts)
    events = build_events(items)
    idx = hist["IMOEX"]
    H = (27, 45, 90, 180)
    labels = {27: "3д", 45: "нед", 90: "2нед", 180: "мес"}
    by = defaultdict(lambda: {"before": [], "after": []})
    for ev in events:
        if ev["topic"] not in mw_topics or ev.get("sign") not in (1, -1):
            continue
        mv = move(idx, ev["ts"], horizons=H, back=(27,))
        if not mv:
            continue
        half = "before" if ev["ts"] < split_ts else "after"
        by[ev["topic"]][half].append((ev["sign"], mv))
    print(f"\n4. ЗНАК ПО СЛОВАМ НА ДЛИННЫХ ГОРИЗОНТАХ (индекс; угадано % / средний ход «в знак»), до и после {datetime.fromtimestamp(split_ts, MSK).date()}")
    print(f"   {'тема':34} {'n до/после':>11} " + " ".join(f"{labels[h]:>14}" for h in H) + "   (каждая ячейка: до | после)")
    fund = {}
    for topic, halves in sorted(by.items(), key=lambda kv: -len(kv[1]["before"]) - len(kv[1]["after"])):
        cells = []
        for h in H:
            parts = []
            for half in ("before", "after"):
                lst = [(s, mv[f"f{h}"]) for s, mv in halves[half] if mv.get(f"f{h}") is not None]
                if len(lst) < 30:
                    parts.append("   —   ")
                    continue
                hit = np.mean([(r > 0) == (s > 0) for s, r in lst])
                parts.append(f"{hit:.0%}/{np.mean([s * r for s, r in lst]):+.1f}")
            cells.append(f"{parts[0]:>7}|{parts[1]:>7}")
        n = f"{len(halves['before'])}/{len(halves['after'])}"
        print(f"   {topic:34} {n:>11} " + " ".join(cells))
        fund[topic] = n
    return fund


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2019-01-01")
    ap.add_argument("--split", default="2024-01-01")
    ap.add_argument("--skip-topics", action="store_true", help="не считать раздел 4 (долгая загрузка всего архива)")
    a = ap.parse_args()
    split_ts = int(datetime.strptime(a.split, "%Y-%m-%d").replace(tzinfo=MSK).timestamp())
    since_ts = int(datetime.strptime(a.since, "%Y-%m-%d").replace(tzinfo=MSK).timestamp())
    c = sqlite3.connect(ROOT / "data" / "news_archive.db")
    hist = load_hist(["IMOEX", "SBER", "RGBI"] + list(NAME_RX))
    print(f"свечи: {len(hist)} бумаг + индекс")

    decisions = cb_decisions(c, since_ts, hist)
    cb_out = cb_report(decisions)

    sanc = dated_events(c, hist, SANCTIONS, "sanctions")
    dated_report("2а. ПАКЕТЫ САНКЦИЙ (индекс Мосбиржи, %; «обсужд/7д» — сообщений со словом «санкции» за 7 дней до, «явно» — про новый/готовящийся пакет)", sanc, True)
    keys = dated_events(c, hist, KEYDATES, "key")
    dated_report("2б. ПЕРЕЛОМНЫЕ ДАТЫ (индекс Мосбиржи, %)", keys, False)

    divs = dividends(c, since_ts, hist)
    div_report(divs)
    sys.stdout.flush()

    topics = None if a.skip_topics else topics_long(since_ts, split_ts, hist)

    json.dump({"cb": decisions, "cb_groups": cb_out, "sanctions": sanc, "keydates": keys, "dividends": divs, "topics_long": topics},
              open(ROOT / "data" / "news_expectations.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1, default=str)
    print("\nсохранено: data/news_expectations.json")


if __name__ == "__main__":
    main()
