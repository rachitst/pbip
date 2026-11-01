import { apiStream } from "../api/client";
import type {
  ChatResponse,
  StreamError,
  StreamToken,
  StreamToolCall,
  StreamToolResult,
} from "../api/client";

/** One parsed Server-Sent Event from POST /api/v1/assistant/chat/stream. */
export type AssistantEvent =
  | { type: "tool_call"; data: StreamToolCall }
  | { type: "tool_result"; data: StreamToolResult }
  | { type: "token"; data: StreamToken }
  | { type: "done"; data: ChatResponse }
  | { type: "error"; data: StreamError };

const EVENT_TYPES = new Set(["tool_call", "tool_result", "token", "done", "error"]);

/** Parse one SSE frame ("event: x\ndata: {...}"). Unknown or malformed frames are dropped. */
export function parseFrame(frame: string): AssistantEvent | null {
  let name = "";
  let data = "";
  for (const line of frame.split("\n")) {
    if (line.startsWith("event: ")) name = line.slice(7).trim();
    else if (line.startsWith("data: ")) data += line.slice(6);
  }
  if (!EVENT_TYPES.has(name) || data === "") return null;
  try {
    // The server builds every payload from the models in schema.d.ts (AssistantStreamFrames).
    return { type: name, data: JSON.parse(data) as unknown } as AssistantEvent;
  } catch {
    return null;
  }
}

/** Send one message and call `onEvent` for each frame as it arrives. */
export async function streamChat(
  message: string,
  onEvent: (event: AssistantEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const response = await apiStream("/api/v1/assistant/chat/stream", { message }, signal);
  if (!response.body) throw new Error("streaming is not supported by this browser");
  const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += value;
    let boundary = buffer.indexOf("\n\n");
    while (boundary !== -1) {
      const event = parseFrame(buffer.slice(0, boundary));
      buffer = buffer.slice(boundary + 2);
      if (event) onEvent(event);
      boundary = buffer.indexOf("\n\n");
    }
  }
}
