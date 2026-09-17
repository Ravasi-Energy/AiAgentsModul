"use client";

import { useEffect, useState } from "react";

import type { TelegramLinkCode } from "@/lib/api";

// Telegram bot usernames: 5-32 word characters (they always end in "bot").
const BOT_USERNAME_RE = /^[A-Za-z0-9_]{5,32}$/;

// Self-serve Telegram pairing. Mints a one-shot code and shows the deep link
// (https://t.me/<bot>?start=<code>); tapping it makes Telegram send
// "/start <code>" to the bot, which binds that chat to the Person. Shared by
// the Integrations card (link yourself) and the Person page (link someone
// else). No polling: the bot confirms in Telegram, and the Person page shows
// the chat id on its next load.
export default function TelegramLinkPanel({
  mint,
  fallbackBotUsername,
  buttonLabel = "Link my Telegram",
  compact = false,
}: {
  mint: () => Promise<TelegramLinkCode>;
  /** Bot username to build the link from when the API can't name it (e.g. the connected card's label). */
  fallbackBotUsername?: string | null;
  buttonLabel?: string;
  compact?: boolean;
}) {
  const [link, setLink] = useState<TelegramLinkCode | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [secondsLeft, setSecondsLeft] = useState<number | null>(null);

  useEffect(() => {
    if (!link) return;
    const expiresAt = new Date(link.expires_at).getTime();
    const tick = () => setSecondsLeft(Math.max(0, Math.round((expiresAt - Date.now()) / 1000)));
    tick();
    const timer = window.setInterval(tick, 1000);
    return () => window.clearInterval(timer);
  }, [link]);

  async function generate() {
    setBusy(true);
    setError(null);
    setCopied(false);
    try {
      const next = await mint();
      // Reset the countdown only with the new link: a stale 0 would flash
      // "expired" for a frame, but resetting before a failed mint would show
      // the old, dead link as live.
      setSecondsLeft(null);
      setLink(next);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Couldn't create a Telegram link");
    } finally {
      setBusy(false);
    }
  }

  // The fallback is a display label ("@exec_bot", or "Telegram bot" when the
  // provider gave no username): only a real username can build a URL.
  const fallback = fallbackBotUsername?.replace(/^@/, "") ?? "";
  const botUsername = link?.bot_username ?? (BOT_USERNAME_RE.test(fallback) ? fallback : null);
  const href = link ? (link.deep_link ?? (botUsername ? `https://t.me/${botUsername}?start=${link.code}` : null)) : null;
  const expired = secondsLeft !== null && secondsLeft <= 0;
  const countdown =
    secondsLeft === null ? "" : `${Math.floor(secondsLeft / 60)}:${String(secondsLeft % 60).padStart(2, "0")}`;

  async function copy(text: string) {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
    } catch {
      setCopied(false);
    }
  }

  return (
    <div className={compact ? "text-xs" : "mt-2 text-xs"}>
      {!link || expired ? (
        <div className="flex flex-wrap items-center gap-2">
          <button
            type="button"
            disabled={busy}
            onClick={() => void generate()}
            className="rounded-lg border border-line-strong bg-surface-overlay px-3 py-1.5 text-sm hover:bg-surface-input disabled:opacity-50"
          >
            {busy ? "Creating link…" : expired ? "Link expired — generate again" : buttonLabel}
          </button>
        </div>
      ) : (
        <div className="rounded-lg border border-line bg-surface-elevated px-3 py-2">
          {href ? (
            <p className="text-fg">
              <a
                href={href}
                target="_blank"
                rel="noopener noreferrer"
                className="font-medium text-indigo-300 underline hover:text-indigo-200"
              >
                Open @{botUsername} in Telegram
              </a>{" "}
              and tap <span className="font-medium">Start</span>.
              <button
                type="button"
                onClick={() => void copy(href)}
                className="ml-2 rounded border border-line px-1.5 py-0.5 text-[11px] text-fg-muted hover:bg-surface-input"
              >
                {copied ? "Copied" : "Copy link"}
              </button>
            </p>
          ) : null}
          <p className={href ? "mt-1 text-fg-muted" : "text-fg"}>
            {href ? "Or send" : `Send`}{" "}
            <code className="rounded bg-surface-input px-1 py-0.5 font-mono text-[11px] text-fg">/start {link.code}</code>{" "}
            to {botUsername ? `@${botUsername}` : "the bot"}.
            {!href ? (
              <button
                type="button"
                onClick={() => void copy(`/start ${link.code}`)}
                className="ml-2 rounded border border-line px-1.5 py-0.5 text-[11px] text-fg-muted hover:bg-surface-input"
              >
                {copied ? "Copied" : "Copy"}
              </button>
            ) : null}
          </p>
          <p className="mt-1 text-fg-muted">
            One use only · expires in {countdown}. The bot replies &ldquo;Linked&rdquo; when it worked.
          </p>
        </div>
      )}
      {error ? <p className="mt-1 text-rose-300">{error}</p> : null}
    </div>
  );
}
