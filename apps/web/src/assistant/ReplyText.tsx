import type { ReactNode } from "react";

/**
 * Minimal, safe markdown for assistant replies: paragraphs, line breaks, numbered and bulleted
 * lists, and **bold**. Builds React elements only (no HTML injection): the reply quotes text
 * written by other people (merchant references), so it is never parsed as markup.
 */
export function ReplyText({ text }: { text: string }) {
  const blocks: ReactNode[] = [];
  let list: { ordered: boolean; items: string[] } | null = null;
  let paragraph: string[] = [];

  const flushParagraph = () => {
    if (paragraph.length > 0) {
      blocks.push(<p key={blocks.length}>{withBreaks(paragraph)}</p>);
      paragraph = [];
    }
  };
  const flushList = () => {
    if (list) {
      const items = list.items.map((item, i) => <li key={i}>{inline(item)}</li>);
      blocks.push(
        list.ordered ? <ol key={blocks.length}>{items}</ol> : <ul key={blocks.length}>{items}</ul>,
      );
      list = null;
    }
  };

  for (const line of text.split("\n")) {
    const ordered = /^\s*\d+\.\s+(.*)$/.exec(line);
    const bullet = /^\s*[-*]\s+(.*)$/.exec(line);
    const item = ordered?.[1] ?? bullet?.[1];
    if (item !== undefined) {
      flushParagraph();
      const isOrdered = ordered !== null;
      if (list && list.ordered !== isOrdered) flushList();
      list ??= { ordered: isOrdered, items: [] };
      list.items.push(item);
    } else if (line.trim() === "") {
      flushList();
      flushParagraph();
    } else {
      flushList();
      paragraph.push(line);
    }
  }
  flushList();
  flushParagraph();
  return <div className="assistant-reply">{blocks}</div>;
}

function withBreaks(lines: string[]): ReactNode[] {
  return lines.flatMap((line, i) =>
    i === 0 ? [inline(line)] : [<br key={`b${String(i)}`} />, inline(line)],
  );
}

function inline(text: string): ReactNode {
  const parts = text.split(/(\*\*[^*]+\*\*)/g);
  return parts.map((part, i) =>
    part.startsWith("**") && part.endsWith("**") && part.length > 4 ? (
      <strong key={i}>{part.slice(2, -2)}</strong>
    ) : (
      part
    ),
  );
}
