// Cloudflare Worker + Durable Object: брокер между головой бота (Германия) и ретранслятором (Россия)
// плюс внешний сторож. Обе стороны держат по WebSocket к объекту Broker. Голова шлёт запросы
// {id, method, url, ...}, брокер отдаёт их ретранслятору, ответ {id, status, ...} возвращается голове.
// Сторож: голова раз в минуту шлёт POST /heartbeat; cron каждые 5 минут проверяет, жива ли голова и подключён ли
// ретранслятор, и пишет в Telegram владельцу, если нет (секреты TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID).
// Доступ только с ключом RELAY_KEY (секрет воркера): ?key=... или заголовок X-Relay-Key.

const HEAD_SILENT_SEC = 10 * 60;     // голова молчит дольше — тревога
const RELAY_OFF_SEC = 10 * 60;       // ретранслятор отключён дольше — тревога
const REPEAT_SEC = 6 * 3600;         // повтор одной тревоги не чаще раза в 6 часов

export class Broker {
  constructor(state, env) {
    this.state = state;
    this.env = env;
  }

  async fetch(req) {
    const url = new URL(req.url);
    if (url.pathname === "/heartbeat") {
      const body = await req.json().catch(() => ({}));
      await this.state.storage.put("head_seen", Date.now());
      await this.state.storage.put("head_info", body);
      return Response.json({ ok: true });
    }
    if (url.pathname === "/check") {
      return Response.json(await this.check());
    }
    if (req.headers.get("Upgrade") !== "websocket") {
      const heads = this.state.getWebSockets("head").length;
      const relays = this.state.getWebSockets("relay").length;
      return Response.json({ heads, relays, head_seen: await this.state.storage.get("head_seen") });
    }
    const role = url.searchParams.get("role");
    if (role !== "head" && role !== "relay") return new Response("bad role", { status: 400 });
    for (const old of this.state.getWebSockets(role)) {
      try { old.close(1000, "replaced"); } catch (e) {}
    }
    const pair = new WebSocketPair();
    const [client, server] = Object.values(pair);
    this.state.acceptWebSocket(server, [role]);
    try { this.state.setWebSocketAutoResponse(new WebSocketRequestResponsePair("ping", "pong")); } catch (e) {}
    if (role === "relay") await this.state.storage.put("relay_seen", Date.now());
    return new Response(null, { status: 101, webSocket: client });
  }

  async webSocketMessage(ws, msg) {
    const role = this.state.getTags(ws)[0];
    if (role === "head") {
      const relays = this.state.getWebSockets("relay");
      if (!relays.length) {
        let id = null;
        try { id = JSON.parse(msg).id; } catch (e) {}
        ws.send(JSON.stringify({ id, error: "relay not connected" }));
        return;
      }
      relays[relays.length - 1].send(msg);
    } else {
      for (const h of this.state.getWebSockets("head")) h.send(msg);
    }
  }

  async webSocketClose(ws, code, reason) {
    try { ws.close(code, reason); } catch (e) {}
  }

  async webSocketError(ws) {
    try { ws.close(1011, "error"); } catch (e) {}
  }

  // ---- сторож ----
  async check() {
    const now = Date.now();
    const headSeen = (await this.state.storage.get("head_seen")) || 0;
    const relays = this.state.getWebSockets("relay").length;
    if (relays) await this.state.storage.put("relay_seen", now);
    const relaySeen = (await this.state.storage.get("relay_seen")) || 0;
    const alerts = (await this.state.storage.get("alerts")) || {};
    const problems = {};
    if (headSeen && now - headSeen > HEAD_SILENT_SEC * 1000) {
      problems.head = `Немецкий сервер с головой бота молчит ${Math.round((now - headSeen) / 60000)} мин: выключен, без сети или не оплачен. Сделки не открываются, позиции под стопами брокера.`;
    }
    if (relaySeen && now - relaySeen > RELAY_OFF_SEC * 1000 && !relays) {
      problems.relay = `Московский ретранслятор отключён от брокера ${Math.round((now - relaySeen) / 60000)} мин: голова не видит Мосбиржу.`;
    }
    const sent = [];
    for (const [k, text] of Object.entries(problems)) {
      if (!alerts[k] || now - alerts[k] > REPEAT_SEC * 1000) {
        await this.telegram("🔧 <b>ТЕХНИЧЕСКАЯ ПРОБЛЕМА</b>: " + (k === "head" ? "сервер с головой бота" : "московский ретранслятор") + "\n\n<b>Что случилось</b>\n" + text + "\n\n<b>Кто проверил</b>\nСторож Cloudflare, независимо от сервера");
        alerts[k] = now;
        sent.push(k);
      }
    }
    for (const k of Object.keys(alerts)) {
      if (!problems[k]) {
        await this.telegram("✅ <b>ИСПРАВЛЕНО</b>: " + (k === "head" ? "сервер с головой бота" : "московский ретранслятор") + "\n\n<b>Что было</b>\n" + (k === "head" ? "немецкий сервер снова на связи" : "московский ретранслятор снова подключён"));
        delete alerts[k];
      }
    }
    await this.state.storage.put("alerts", alerts);
    return { problems: Object.keys(problems), sent, head_seen: headSeen, relay_seen: relaySeen, relays };
  }

  async telegram(text) {
    if (!this.env.TELEGRAM_BOT_TOKEN || !this.env.TELEGRAM_CHAT_ID) return;
    try {
      await fetch(`https://api.telegram.org/bot${this.env.TELEGRAM_BOT_TOKEN}/sendMessage`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ chat_id: this.env.TELEGRAM_CHAT_ID, text, parse_mode: "HTML" }),
      });
    } catch (e) {}
  }
}

function broker(env) {
  return env.BROKER.get(env.BROKER.idFromName("main"));
}

export default {
  async fetch(req, env) {
    const url = new URL(req.url);
    if (url.pathname === "/") return new Response("ok");
    const key = req.headers.get("X-Relay-Key") || url.searchParams.get("key");
    if (!env.RELAY_KEY || key !== env.RELAY_KEY) return new Response("forbidden", { status: 403 });
    if (!["/ws", "/status", "/heartbeat", "/check"].includes(url.pathname)) return new Response("not found", { status: 404 });
    return broker(env).fetch(req);
  },

  async scheduled(event, env, ctx) {
    ctx.waitUntil(broker(env).fetch(new Request("https://broker/check")));
  },
};
