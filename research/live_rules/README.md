# Проверка правил бота на истории

Запуск из папки проекта (нужна часовая история: `python scripts/fetch_history.py <пары> --interval 60 --days 400`):

```
python research/live_rules/bt_entries.py data/history universe.txt
python research/live_rules/bt_params.py data/history universe.txt
python research/live_rules/bt_robustness.py data/history universe.txt
python research/live_rules/post_entry.py data/bot.db post.csv
```

`universe.txt` содержит список пар через пробел. Результаты 19.10.2026 (26 пар, 400 дней, комиссии Bybit учтены):

| Вариант | Сделок | Средний результат | PF |
|---|---|---|---|
| Теханализ как в боте до 19.10 | 2336 | +0.009R | 1.02 |
| + не входить после хода ≥2 ATR за 24ч | 654 | +0.057R | 1.16 |
| + откат 0.5 ATR, стоп 2.5 ATR, один вход на сторону в час | 413 | +0.075R | 1.22 |
| Вход по рынку вместо отката | 2480 | −0.026R | 0.94 |

Устойчивость слабая: на одной половине монет PF 1.39, на другой 1.03. Теханализ без новостей даёт в лучшем
случае тонкое преимущество, поэтому в боте он торгуется только малым размером.
