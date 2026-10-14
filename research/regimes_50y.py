"""Режимы и межрыночные связи на дневных данных: что бывает после страха (VIX), после обвалов S&P, после сильного
доллара, вокруг халвингов, по дням недели; как долго BTC и S&P выходили из просадок. Без ручных списков событий,
только данные. Результат -> data/regimes.json (в контекст модели). Запуск: python research/regimes_50y.py"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bot.config import DATA  # noqa: E402

HALVINGS = ["2012-11-28", "2016-07-09", "2020-05-11", "2024-04-20"]


def load(name):
    p = DATA / "history" / f"{name}_D.csv"
    return pd.read_csv(p, parse_dates=["date"]).set_index("date").sort_index() if p.exists() else None


def fwd(c, n):
    return c.shift(-n) / c - 1


def cond_stats(c, mask, label, horizons=(5, 20, 60)):
    out = {"label": label, "n": int(mask.sum())}
    for h in horizons:
        f = fwd(c, h)[mask]
        base = fwd(c, h)
        out[f"mean{h}"] = float(f.mean() * 100)
        out[f"hit{h}"] = float((f > 0).mean())
        out[f"edge{h}"] = float((f.mean() - base.mean()) * 100)
    return out


def drawdowns(c, threshold):
    """Сколько дней от дна просадки глубже threshold до нового максимума."""
    peak, rows, in_dd, trough, trough_i, peak_i = c.iloc[0], [], False, None, None, c.index[0]
    for i, v in c.items():
        if v >= peak:
            if in_dd:
                rows.append({"peak": str(peak_i.date()), "trough": str(trough_i.date()), "depth": float(trough / peak - 1) * 100,
                             "days_to_trough": int((trough_i - peak_i).days), "days_to_recover": int((i - trough_i).days)})
                in_dd = False
            peak, peak_i = v, i
            continue
        dd = v / peak - 1
        if dd <= -threshold and not in_dd:
            in_dd, trough, trough_i = True, v, i
        if in_dd and v < trough:
            trough, trough_i = v, i
    return rows


def main():
    spx, vix, dxy, btc = load("SPX"), load("VIX"), load("DXY"), load("BTC")
    res = {"built": datetime.now(timezone.utc).isoformat()}

    # --- страх: VIX ---
    if spx is not None and vix is not None:
        j = spx.join(vix["close"].rename("vix"), how="inner")
        c = j["close"]
        res["spx_after_vix"] = [
            cond_stats(c, j["vix"] > 30, "S&P после закрытия VIX > 30"),
            cond_stats(c, j["vix"] > 40, "S&P после закрытия VIX > 40"),
            cond_stats(c, j["vix"] < 13, "S&P при VIX < 13 (самоуспокоенность)"),
        ]
        if btc is not None:
            jb = btc.join(vix["close"].rename("vix"), how="inner")
            res["btc_after_vix"] = [
                cond_stats(jb["close"], jb["vix"] > 30, "BTC после закрытия VIX > 30"),
                cond_stats(jb["close"], jb["vix"] < 13, "BTC при VIX < 13"),
            ]

    # --- межрыночные: обвал S&P, сильный доллар ---
    if btc is not None and spx is not None and dxy is not None:
        j = btc.join(spx["close"].rename("spx"), how="inner").join(dxy["close"].rename("dxy"), how="inner")
        c = j["close"]
        spx_r1, dxy_r5 = j["spx"].pct_change(), j["dxy"].pct_change(5)
        res["btc_cross"] = [
            cond_stats(c, spx_r1 < -0.03, "BTC после дня S&P -3%"),
            cond_stats(c, spx_r1 > 0.02, "BTC после дня S&P +2%"),
            cond_stats(c, dxy_r5 > 0.015, "BTC после роста доллара > 1.5% за неделю"),
            cond_stats(c, dxy_r5 < -0.015, "BTC после падения доллара > 1.5% за неделю"),
        ]
        corr = {}
        for yrs in ((2015, 2019), (2020, 2022), (2023, 2026)):
            seg = j[(j.index.year >= yrs[0]) & (j.index.year <= yrs[1])]
            corr[f"{yrs[0]}-{yrs[1]}"] = {"spx": float(seg["close"].pct_change().corr(seg["spx"].pct_change())),
                                          "dxy": float(seg["close"].pct_change().corr(seg["dxy"].pct_change()))}
        res["btc_corr_daily"] = corr

    # --- халвинги ---
    if btc is not None:
        c = btc["close"]
        rows = []
        for h in HALVINGS:
            dt = pd.Timestamp(h)
            if dt not in c.index:
                dt = c.index[c.index.searchsorted(dt)]
            base = c.loc[dt]
            rec = {"date": h}
            for name, days in (("m3_before", -90), ("m1_after", 30), ("m3_after", 90), ("m6_after", 180), ("y1_after", 365)):
                t2 = dt + pd.Timedelta(days=days)
                if t2 in c.index or (c.index.min() <= t2 <= c.index.max()):
                    idx = c.index.searchsorted(t2)
                    if idx < len(c):
                        rec[name] = float(c.iloc[idx] / base - 1) * 100
            rows.append(rec)
        res["halvings"] = rows

        # --- дни недели (BTC торгуется 7 дней) ---
        r = c.pct_change()
        res["btc_weekday"] = {d: {"mean": float(r[r.index.dayofweek == i].mean() * 100),
                                  "hit": float((r[r.index.dayofweek == i] > 0).mean())}
                              for i, d in enumerate(["пн", "вт", "ср", "чт", "пт", "сб", "вс"])}

        # --- просадки ---
        dd = drawdowns(c, 0.30)
        res["btc_drawdowns_30"] = {"n": len(dd), "median_depth": float(np.median([x["depth"] for x in dd])) if dd else 0,
                                   "median_days_to_recover": float(np.median([x["days_to_recover"] for x in dd])) if dd else 0,
                                   "list": dd}
    if spx is not None:
        dd = drawdowns(spx["close"], 0.20)
        res["spx_drawdowns_20"] = {"n": len(dd), "median_depth": float(np.median([x["depth"] for x in dd])) if dd else 0,
                                   "median_days_to_recover": float(np.median([x["days_to_recover"] for x in dd])) if dd else 0,
                                   "list": dd}

    (DATA / "regimes.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    for key in ("spx_after_vix", "btc_after_vix", "btc_cross"):
        for s in res.get(key, []):
            print(f"{s['label']:48s} n={s['n']:5d}  20д {s['mean20']:+.2f}% ({s['hit20'] * 100:.0f}%) преим. {s['edge20']:+.2f}%  "
                  f"60д {s['mean60']:+.2f}% преим. {s['edge60']:+.2f}%")
    print("корреляция дневных доходностей BTC:", res.get("btc_corr_daily"))
    print("халвинги:", [{k: (round(v, 0) if isinstance(v, float) else v) for k, v in h.items()} for h in res.get("halvings", [])])
    print("дни недели BTC:", {k: round(v["mean"], 2) for k, v in res.get("btc_weekday", {}).items()})
    for key in ("btc_drawdowns_30", "spx_drawdowns_20"):
        d = res.get(key)
        if d:
            print(f"{key}: n={d['n']}, медианная глубина {d['median_depth']:.0f}%, медиана дней до восстановления {d['median_days_to_recover']:.0f}")
    print(f"\nСохранено: {DATA / 'regimes.json'}")


if __name__ == "__main__":
    main()
