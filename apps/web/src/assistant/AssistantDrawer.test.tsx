import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { json, urlOf } from "../test-utils";
import { AssistantDrawer, STARTER_PROMPTS, TOOL_LABELS } from "./AssistantDrawer";
import { ReplyText } from "./ReplyText";
import { parseFrame } from "./stream";

function frame(event: string, data: unknown): string {
  return `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
}

/** A streaming Response whose chunks are released one at a time by calling `next()`. */
function controlledStream(chunks: string[]) {
  const encoder = new TextEncoder();
  let controller: ReadableStreamDefaultController<Uint8Array> | null = null;
  const body = new ReadableStream<Uint8Array>({
    start(c) {
      controller = c;
    },
  });
  let i = 0;
  return {
    response: new Response(body, { status: 200, headers: { "Content-Type": "text/event-stream" } }),
    next() {
      const chunk = chunks[i];
      i += 1;
      if (chunk === undefined) controller?.close();
      else controller?.enqueue(encoder.encode(chunk));
    },
    finish() {
      while (i <= chunks.length) this.next();
    },
  };
}

const done = (reply: string, extra: Record<string, unknown> = {}) => ({
  reply,
  outcome: "answered",
  tool: "get_account_summary",
  engine: "rules",
  link: "/accounts",
  ...extra,
});

function renderDrawer() {
  return render(
    <QueryClientProvider
      client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
    >
      <MemoryRouter>
        <AssistantDrawer />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("AssistantDrawer", () => {
  let streamResponse: () => Response;
  const fetchMock = vi.fn((input: Parameters<typeof fetch>[0], init?: RequestInit) => {
    const url = urlOf(input);
    if (url.startsWith("/api/v1/assistant/messages")) {
      return Promise.resolve(json(200, { items: [] }));
    }
    if (url === "/api/v1/assistant/chat/stream" && init?.method === "POST") {
      return Promise.resolve(streamResponse());
    }
    return Promise.resolve(json(404, {}));
  });

  beforeEach(() => {
    vi.stubGlobal("fetch", fetchMock);
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    fetchMock.mockClear();
  });

  it("opens from the floating button and offers starter prompts", async () => {
    renderDrawer();
    const fab = screen.getByRole("button", { name: "Ask the PBIP assistant" });
    expect(fab).toHaveAttribute("aria-expanded", "false");
    await userEvent.click(fab);
    const dialog = screen.getByRole("dialog", { name: "PBIP assistant" });
    expect(fab).toHaveAttribute("aria-expanded", "true");
    for (const prompt of STARTER_PROMPTS) {
      expect(await within(dialog).findByRole("button", { name: prompt })).toBeInTheDocument();
    }
    expect(within(dialog).getByText(/can't move money/)).toBeInTheDocument();
    await userEvent.click(within(dialog).getByRole("button", { name: "Close assistant" }));
    expect(dialog).not.toHaveAttribute("open");
  });

  it("shows the tool indicator, then renders tokens as they stream in", async () => {
    const stream = controlledStream([
      frame("tool_call", { tool: "get_account_summary", engine: "rules" }),
      frame("tool_result", { tool: "get_account_summary", outcome: "ok" }),
      frame("token", { text: "Your " }),
      frame("token", { text: "accounts:\n" }) + frame("token", { text: "1. Savings ••1234" }),
      frame("done", done("Your accounts:\n1. Savings ••1234")),
    ]);
    streamResponse = () => stream.response;
    renderDrawer();
    await userEvent.click(screen.getByRole("button", { name: "Ask the PBIP assistant" }));
    const dialog = screen.getByRole("dialog");
    await userEvent.click(
      await within(dialog).findByRole("button", { name: "What's my balance?" }),
    );

    stream.next();
    expect(await within(dialog).findByText(TOOL_LABELS.get_account_summary)).toBeInTheDocument();
    stream.next();
    stream.next();
    expect(await within(dialog).findByText("Your")).toBeInTheDocument();
    expect(screen.queryByText(TOOL_LABELS.get_account_summary)).not.toBeInTheDocument();
    stream.next();
    expect(await within(dialog).findByRole("listitem")).toHaveTextContent("Savings ••1234");
    stream.finish();
    expect(await within(dialog).findByRole("link", { name: "Open accounts" })).toBeInTheDocument();
    const sentBody = fetchMock.mock.calls.at(-1)?.[1]?.body;
    expect(typeof sentBody).toBe("string");
    expect(JSON.parse(sentBody as string)).toEqual({ message: "What's my balance?" });
  });

  it("explains a rate limit instead of failing silently", async () => {
    streamResponse = () =>
      new Response(JSON.stringify({ code: "rate_limited", title: "too many requests" }), {
        status: 429,
        headers: { "Content-Type": "application/problem+json" },
      });
    renderDrawer();
    await userEvent.click(screen.getByRole("button", { name: "Ask the PBIP assistant" }));
    const input = screen.getByRole("textbox", { name: "Message" });
    await userEvent.type(input, "hello{Enter}");
    expect(await screen.findByRole("alert")).toHaveTextContent(/wait a minute/);
    expect(screen.getByRole("button", { name: "Send" })).toBeDisabled(); // empty draft again
  });

  it("does not link to pages the app does not have", async () => {
    const stream = controlledStream([
      frame(
        "done",
        done("I can't move money.", { outcome: "refused", tool: null, link: "/transfers" }),
      ),
    ]);
    streamResponse = () => stream.response;
    renderDrawer();
    await userEvent.click(screen.getByRole("button", { name: "Ask the PBIP assistant" }));
    await userEvent.type(
      screen.getByRole("textbox", { name: "Message" }),
      "send 500 to Bob{Enter}",
    );
    stream.finish();
    expect(await screen.findByText("I can't move money.")).toBeInTheDocument();
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
  });

  it("shows a generic error event from the stream", async () => {
    const stream = controlledStream([frame("error", { detail: "Please try again shortly." })]);
    streamResponse = () => stream.response;
    renderDrawer();
    await userEvent.click(screen.getByRole("button", { name: "Ask the PBIP assistant" }));
    await userEvent.type(screen.getByRole("textbox", { name: "Message" }), "hi{Enter}");
    stream.finish();
    expect(await screen.findByRole("alert")).toHaveTextContent("Please try again shortly.");
  });
});

describe("ReplyText", () => {
  it("renders lists, paragraphs and bold without interpreting HTML", () => {
    render(
      <ReplyText
        text={
          "Your accounts:\n1. **Savings** ••1234\n2. Current ••9876\n\n<img src=x onerror=alert(1)>"
        }
      />,
    );
    const items = screen.getAllByRole("listitem");
    expect(items).toHaveLength(2);
    expect(within(items[0] as HTMLElement).getByText("Savings").tagName).toBe("STRONG");
    expect(screen.getByText("<img src=x onerror=alert(1)>")).toBeInTheDocument();
    expect(document.querySelector("img")).toBeNull();
  });
});

describe("parseFrame", () => {
  it("parses known events and drops unknown or malformed ones", () => {
    expect(parseFrame('event: token\ndata: {"text":"hi"}')).toEqual({
      type: "token",
      data: { text: "hi" },
    });
    expect(parseFrame('event: weird\ndata: {"x":1}')).toBeNull();
    expect(parseFrame("event: token\ndata: {not json")).toBeNull();
  });
});
