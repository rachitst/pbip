import { Bell, MessageCircle, Menu, Moon, Sun, X } from "lucide-react";
import type { LucideIcon } from "lucide-react";

/** The shell's named icons, drawn from the same set as the rest of the app (lucide, ADR-0063). */
const ICONS = {
  sun: Sun,
  moon: Moon,
  menu: Menu,
  close: X,
  chat: MessageCircle,
  bell: Bell,
} as const satisfies Record<string, LucideIcon>;

export type IconName = keyof typeof ICONS;

/** Decorative only: the control that contains an icon carries the accessible name. */
export function Icon({ name, size = 16 }: { name: IconName; size?: number }) {
  const Glyph = ICONS[name];
  return (
    <Glyph className="icon" size={size} strokeWidth={2} aria-hidden="true" focusable="false" />
  );
}
