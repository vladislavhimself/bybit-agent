// Cloudflare Worker + Durable Object: брокер между головой бота (Германия) и ретранслятором (Россия).
// Обе стороны держат по WebSocket к объекту Broker. Голова шлёт запросы {id, method, url, ...},
// брокер отдаёт их ретранслятору, ответ {id, status, ...} возвращается голове. Сам брокер ничего не хранит.
// Доступ только с ключом RELAY_KEY (секрет воркера): ?key=... или заголовок X-Relay-Key.

export class Broker {
  constructor(state, env) {
    this.state = state;
    this.env = env;
  }

  async fetch(req) {
    const url = new URL(req.url);
    if (req.headers.get("Upgrade") !== "websocket") {
      const heads = this.state.getWebSockets("head").length;
      const relays = this.state.getWebSockets("relay").length;
      return Response.json({ heads, relays });
    }
    const role = url.searchParams.get("role");
    if (role !== "head" && role !== "relay") return new Response("bad role", { status: 400 });
    // одна живая сессия на роль: старое соединение закрываем, чтобы не плодить зомби после обрывов
    for (const old of this.state.getWebSockets(role)) {
      try { old.close(1000, "replaced"); } catch (e) {}
    }
    const pair = new WebSocketPair();
    const [client, server] = Object.values(pair);
    this.state.acceptWebSocket(server, [role]);
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
}

export default {
  async fetch(req, env) {
    const url = new URL(req.url);
    if (url.pathname === "/") return new Response("ok");
    const key = req.headers.get("X-Relay-Key") || url.searchParams.get("key");
    if (!env.RELAY_KEY || key !== env.RELAY_KEY) return new Response("forbidden", { status: 403 });
    if (url.pathname !== "/ws" && url.pathname !== "/status") return new Response("not found", { status: 404 });
    const id = env.BROKER.idFromName("main");
    return env.BROKER.get(id).fetch(req);
  },
};
