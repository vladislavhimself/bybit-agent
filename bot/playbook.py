"""Исторический отклик рынка на типы событий (data/playbook.json, строится scripts/build_playbook.py).
Используется как априорная оценка для модели и как поправка к вкладу новости в сигнал."""
import json

from .config import DATA

PATH = DATA / "playbook.json"


class Playbook:
    def __init__(self):
        self.data = {}
        if PATH.exists():
            try:
                self.data = json.loads(PATH.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                self.data = {}

    def prior(self, event_type, direction):
        """Вернуть статистику для (тип, направление) или None."""
        return self.data.get("stats", {}).get(f"{event_type}:{direction}")

    def adjust(self, event_type, direction):
        """Множитель к вкладу новости: 0.6..1.3 по исторической доле отработавших событий."""
        p = self.prior(event_type, direction)
        if not p or p["n"] < 4:
            return 1.0
        hit = p["hit_24h"]
        return max(0.6, min(1.3, 0.6 + hit * 0.9))  # hit 0.5 -> 1.05, 0.8 -> 1.32, 0.3 -> 0.87

    def text(self):
        st = self.data.get("stats", {})
        if not st:
            return ""
        lines = ["Историческая реакция BTC на типы событий (медиана, % за 24ч / 72ч, доля отработавших в ожидаемую сторону):"]
        for k, v in sorted(st.items(), key=lambda kv: -kv[1]["n"]):
            lines.append(f"- {k}: n={v['n']}, 24ч {v['med_24h']:+.1f}%, 72ч {v['med_72h']:+.1f}%, "
                         f"отработало {v['hit_24h'] * 100:.0f}%, типичный размах 1ч {v['mean_abs_1h']:.1f}%")
        return "\n".join(lines)
