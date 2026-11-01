import { useQuery } from "@tanstack/react-query";
import { useEffect, useId, useRef, useState } from "react";
import type { SyntheticEvent } from "react";
import { Link } from "react-router";
import { ApiError, api } from "../api/client";
import type { ChatResponse, StreamToolCall } from "../api/client";
import { Button } from "../ui/Button";
import { Icon } from "../ui/Icon";
import { ReplyText } from "./ReplyText";
import { streamChat } from "./stream";

/** What the indicator badge says while a tool runs (TASK-061). */
export const TOOL_LABELS: Record<StreamToolCall["tool"], string> = {
  get_account_summary: "Checking account balance...",
  get_recent_transactions: "Looking up recent transactions...",
  get_spend: "Adding up your spending...",
  get_balance_forecast: "Forecasting your balance...",
  get_loan_affordability: "Checking loan affordability...",
};

export const STARTER_PROMPTS = [
  "What's my balance?",
  "How much did I spend on dining?",
  "Can I afford a personal loan of 2 lakh over 3 years?",
  "What will my balance be next week?",
] as const;

/** Only links to pages this app has; the server may name pages that are not built yet. */
const ROUTABLE = new Set(["/accounts", "/insights", "/profile"]);

interface Message {
  id: string;
  role: "user" | "assistant";
  text: string;
  link?: string | null;
  outcome?: ChatResponse["outcome"];
}

function errorText(error: unknown): string {
  if (error instanceof ApiError && error.status === 429) {
    return "You've sent a lot of messages. Please wait a minute and try again.";
  }
  if (error instanceof ApiError && error.status === 422) return "That message couldn't be sent.";
  return "The assistant is unavailable right now. Please try again.";
}

/**
 * Floating "Ask PBIP" button and side drawer, on every customer page (ADR-0016 shell and tokens).
 * Native <dialog> (showModal) gives focus trap, Esc and focus return to the button. The message
 * list is an aria-live log so screen readers hear replies as they stream in.
 */
export function AssistantDrawer() {
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState("");
  const [sent, setSent] = useState<Message[]>([]);
  const [streaming, setStreaming] = useState<string | null>(null);
  const [activeTool, setActiveTool] = useState<StreamToolCall["tool"] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const dialogRef = useRef<HTMLDialogElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const abortRef = useRef<AbortController | null>(null);
  const logRef = useRef<HTMLDivElement>(null);
  const titleId = useId();

  const history = useQuery({
    queryKey: ["assistant-history"],
    queryFn: () => api.assistantHistory(),
    enabled: open,
    retry: false,
    staleTime: Infinity,
  });

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    if (open && !dialog.open) {
      dialog.showModal();
      inputRef.current?.focus();
    }
    if (!open && dialog.open) dialog.close();
  }, [open]);

  useEffect(() => {
    const log = logRef.current;
    if (log) log.scrollTop = log.scrollHeight;
  }, [sent, streaming]);

  useEffect(() => () => abortRef.current?.abort(), []);

  const past: Message[] = (history.data?.items ?? []).map((item, i) => ({
    id: `h${String(i)}`,
    role: item.role,
    text: item.content,
  }));
  const messages = [...past, ...sent];

  async function send(text: string) {
    const message = text.trim();
    if (!message || busy) return;
    setBusy(true);
    setError(null);
    setDraft("");
    setSent((m) => [...m, { id: `u${String(Date.now())}`, role: "user", text: message }]);
    setStreaming("");
    const controller = new AbortController();
    abortRef.current = controller;
    try {
      await streamChat(
        message,
        (event) => {
          if (event.type === "tool_call") setActiveTool(event.data.tool);
          else if (event.type === "tool_result") setActiveTool(null);
          else if (event.type === "token") setStreaming((s) => (s ?? "") + event.data.text);
          else if (event.type === "error") setError(event.data.detail);
          else {
            const done = event.data;
            setSent((m) => [
              ...m,
              {
                id: `a${String(Date.now())}`,
                role: "assistant",
                text: done.reply,
                link: done.link,
                outcome: done.outcome,
              },
            ]);
          }
        },
        controller.signal,
      );
    } catch (err) {
      if (!controller.signal.aborted) setError(errorText(err));
    } finally {
      setStreaming(null);
      setActiveTool(null);
      setBusy(false);
    }
  }

  function onSubmit(event: SyntheticEvent<HTMLFormElement>) {
    event.preventDefault();
    void send(draft);
  }

  return (
    <>
      <Button
        className="assistant-fab"
        aria-label="Ask the PBIP assistant"
        aria-haspopup="dialog"
        aria-expanded={open}
        onClick={() => {
          setOpen(true);
        }}
      >
        <Icon name="chat" size={20} />
        <span className="assistant-fab__label">Ask PBIP</span>
      </Button>
      <dialog
        ref={dialogRef}
        className="assistant-drawer"
        aria-labelledby={titleId}
        onCancel={(event) => {
          event.preventDefault();
          setOpen(false);
        }}
      >
        <header className="assistant-drawer__header">
          <h2 id={titleId} className="assistant-drawer__title">
            PBIP assistant
          </h2>
          <Button
            variant="ghost"
            size="sm"
            aria-label="Close assistant"
            onClick={() => {
              setOpen(false);
            }}
          >
            <Icon name="close" />
          </Button>
        </header>
        <p className="assistant-drawer__note muted">
          Read-only: it can look at your own accounts but can't move money.
        </p>
        <div
          ref={logRef}
          className="assistant-log"
          role="log"
          aria-live="polite"
          aria-relevant="additions"
        >
          {history.isPending && open && <p className="muted">Loading conversation...</p>}
          {history.isError && <p className="muted">Earlier messages couldn't be loaded.</p>}
          {messages.length === 0 && !history.isPending && (
            <div className="assistant-starters">
              <p className="muted">Try asking:</p>
              <ul>
                {STARTER_PROMPTS.map((prompt) => (
                  <li key={prompt}>
                    <Button
                      variant="secondary"
                      size="sm"
                      className="assistant-chip"
                      disabled={busy}
                      onClick={() => {
                        void send(prompt);
                      }}
                    >
                      {prompt}
                    </Button>
                  </li>
                ))}
              </ul>
            </div>
          )}
          {messages.map((m) => (
            <div key={m.id} className={`assistant-msg assistant-msg--${m.role}`}>
              <span className="sr-only">{m.role === "user" ? "You:" : "Assistant:"}</span>
              {m.role === "user" ? <p>{m.text}</p> : <ReplyText text={m.text} />}
              {m.link && ROUTABLE.has(m.link) && (
                <Link
                  className="assistant-msg__link"
                  to={m.link}
                  onClick={() => {
                    setOpen(false);
                  }}
                >
                  Open {m.link.slice(1)}
                </Link>
              )}
            </div>
          ))}
          {activeTool && (
            <p className="assistant-tool" role="status">
              <span className="assistant-tool__dot" aria-hidden="true" />
              {TOOL_LABELS[activeTool]}
            </p>
          )}
          {streaming !== null && streaming !== "" && (
            <div className="assistant-msg assistant-msg--assistant" aria-busy="true">
              <ReplyText text={streaming} />
            </div>
          )}
          {busy && !activeTool && streaming === "" && (
            <p className="assistant-tool" role="status">
              Thinking...
            </p>
          )}
          {error && (
            <p className="assistant-error" role="alert">
              {error}
            </p>
          )}
        </div>
        <form className="assistant-form" onSubmit={onSubmit}>
          <label htmlFor={`${titleId}-input`} className="sr-only">
            Message
          </label>
          <textarea
            id={`${titleId}-input`}
            ref={inputRef}
            className="assistant-input"
            rows={2}
            maxLength={500}
            value={draft}
            placeholder="Ask about your balance, spending or a loan"
            onChange={(event) => {
              setDraft(event.target.value);
            }}
            onKeyDown={(event) => {
              if (event.key === "Enter" && !event.shiftKey) {
                event.preventDefault();
                void send(draft);
              }
            }}
          />
          <Button type="submit" disabled={busy || draft.trim() === ""}>
            Send
          </Button>
        </form>
      </dialog>
    </>
  );
}
