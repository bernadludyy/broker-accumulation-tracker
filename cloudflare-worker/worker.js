export default {
  async fetch(request, env) {
    if (request.method !== "POST") {
      return new Response("OK", { status: 200 });
    }

    const telegramSecret =
      request.headers.get("X-Telegram-Bot-Api-Secret-Token") || "";

    if (
      !env.TELEGRAM_WEBHOOK_SECRET ||
      telegramSecret !== env.TELEGRAM_WEBHOOK_SECRET
    ) {
      return new Response("Unauthorized", { status: 401 });
    }

    let update;
    try {
      update = await request.json();
    } catch {
      return new Response("Invalid JSON", { status: 400 });
    }

    const message = update.message || update.edited_message;

    if (!message || !message.text) {
      return Response.json({ ok: true, ignored: "non-text update" });
    }

    const chatId = String(message.chat?.id ?? "");

    if (
      env.TELEGRAM_CHAT_ID &&
      chatId !== String(env.TELEGRAM_CHAT_ID)
    ) {
      return Response.json({ ok: true, ignored: "unauthorized chat" });
    }

    const owner = env.GITHUB_OWNER;
    const repo = env.GITHUB_REPO;
    const token = env.GITHUB_TOKEN;

    if (!owner || !repo || !token) {
      return new Response(
        "GitHub Worker environment variables are incomplete",
        { status: 500 }
      );
    }

    const response = await fetch(
      `https://api.github.com/repos/${owner}/${repo}/dispatches`,
      {
        method: "POST",
        headers: {
          "Authorization": `Bearer ${token}`,
          "Accept": "application/vnd.github+json",
          "X-GitHub-Api-Version": "2022-11-28",
          "User-Agent": "telegram-broker-bot-worker",
          "Content-Type": "application/json"
        },
        body: JSON.stringify({
          event_type: "telegram_update",
          client_payload: {
            telegram_update: update
          }
        })
      }
    );

    if (!response.ok) {
      const body = await response.text();
      return new Response(
        `GitHub dispatch failed: ${response.status}\n${body}`,
        { status: 502 }
      );
    }

    return Response.json({
      ok: true,
      dispatched: true,
      update_id: update.update_id ?? null
    });
  }
};
