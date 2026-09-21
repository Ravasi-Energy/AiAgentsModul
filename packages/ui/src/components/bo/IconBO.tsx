"use client";

// Lucide icon registry for the BO surfaces (DESIGN.md: "Lucide prin
// registru" — pages name entries from this map, they never import icons
// directly). Keep the map small; add entries as surfaces need them.
import {
  Activity,
  ArrowLeft,
  Bot,
  CheckCircle2,
  ChevronRight,
  CircleAlert,
  Clock3,
  FileJson2,
  History,
  Info,
  ListChecks,
  Play,
  Plus,
  RefreshCw,
  Settings2,
  ShieldAlert,
  ShieldCheck,
  XCircle,
  type LucideIcon,
} from "lucide-react";

export const BO_ICONS = {
  activity: Activity,
  "arrow-left": ArrowLeft,
  bot: Bot,
  check: CheckCircle2,
  "chevron-right": ChevronRight,
  alert: CircleAlert,
  clock: Clock3,
  "file-json": FileJson2,
  history: History,
  info: Info,
  list: ListChecks,
  play: Play,
  plus: Plus,
  refresh: RefreshCw,
  settings: Settings2,
  "shield-alert": ShieldAlert,
  "shield-check": ShieldCheck,
  close: XCircle,
} as const;

export type BoIconName = keyof typeof BO_ICONS;

export default function IconBO({
  name,
  size = 16,
  className,
}: {
  name: BoIconName;
  size?: number;
  className?: string;
}) {
  const Cmp: LucideIcon = BO_ICONS[name];
  return (
    <Cmp
      size={size}
      strokeWidth={1.75}
      aria-hidden="true"
      className={className}
    />
  );
}
