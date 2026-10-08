import {
  BadgeCheck,
  Ban,
  Bot,
  Boxes,
  CalendarCheck,
  CalendarClock,
  CircleHelp,
  Cog,
  Cpu,
  Disc,
  Filter,
  Flame,
  Gauge,
  Grip,
  Hourglass,
  Link2Off,
  OctagonAlert,
  Package,
  PackageSearch,
  PackageX,
  ScanLine,
  ShieldAlert,
  SprayCan,
  UserX,
  Users,
  Warehouse,
  Workflow,
  Wrench,
  Zap,
  type LucideIcon,
} from "lucide-react";

/** Presentation only: icon of an equipment type (types come from plant.yaml; unknown → Cog). */
const TYPE_ICONS: Record<string, LucideIcon> = {
  robot: Bot,
  fixture: Grip,
  booth: SprayCan,
  oven: Flame,
  conveyor: Workflow,
  test_stand: Gauge,
};
export function equipmentIcon(type: string | undefined): LucideIcon {
  return (type && TYPE_ICONS[type]) || Cog;
}

export function storageIcon(code: string): LucideIcon {
  return code === "FG" ? Warehouse : Boxes;
}

/** Icons of reason categories / reasons (reason_codes.yaml); unknown codes fall back. */
const CATEGORY_ICONS: Record<string, LucideIcon> = {
  PM: CalendarClock,
  MT: Filter,
  ME: Cog,
  EL: Zap,
  RB: Bot,
  MAT: PackageX,
  ORG: Users,
  QLT: ShieldAlert,
  UNK: CircleHelp,
};
const REASON_ICONS: Record<string, LucideIcon> = {
  "PM-SCHEDULED": CalendarCheck,
  "PM-CLEANING": SprayCan,
  "MT-FILTER": Filter,
  "MT-CONSUMABLE": Package,
  "ME-CHAIN": Link2Off,
  "ME-BEARING": Disc,
  "ME-JAM": Ban,
  "EL-SENSOR": ScanLine,
  "EL-DRIVE": Zap,
  "EL-PLC": Cpu,
  "RB-COLLISION": OctagonAlert,
  "RB-TOOL": Wrench,
  "MAT-SHORTAGE": PackageX,
  "MAT-QUALITY": PackageSearch,
  "ORG-NO-OPERATOR": UserX,
  "ORG-WAIT-QC": Hourglass,
  "QLT-REWORK-STOP": BadgeCheck,
  UNK: CircleHelp,
};
export function categoryIcon(code: string): LucideIcon {
  return CATEGORY_ICONS[code] ?? CircleHelp;
}
export function reasonIcon(code: string, category?: string): LucideIcon {
  return REASON_ICONS[code] ?? (category ? categoryIcon(category) : CircleHelp);
}
