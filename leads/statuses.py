"""Lead statuses with colors (board view). Built-in keys keep their meaning in the call queue; managers can add custom ones."""
import re

from .models import Lead, StatusDef

# key -> (color, sort). Labels come from Lead.Status.
BUILTIN = {
    "new": ("#3B82F6", 10), "interested": ("#F97316", 20), "callback": ("#8B5CF6", 30), "no_answer": ("#F59E0B", 40),
    "in_progress": ("#0EA5E9", 50), "closed": ("#10B981", 60), "not_relevant": ("#64748B", 70), "do_not_call": ("#DC2626", 80),
}
BUILTIN_KIND = {"closed": "won", "not_relevant": "lost", "do_not_call": "lost"}
PALETTE = ["#F59E0B", "#B91C1C", "#6366F1", "#0D9488", "#EC4899", "#F97316", "#A855F7", "#2563EB",
           "#0EA5E9", "#65A30D", "#D946EF", "#7C3AED", "#059669", "#0F766E", "#16A34A", "#EF4444",
           "#111827", "#334155", "#C2410C", "#92400E", "#3F6212", "#166534", "#6D28D9", "#1E3A8A"]


def ensure(workspace):
    have = set(StatusDef.objects.filter(workspace=workspace).values_list("key", flat=True))
    for key, (color, sort) in BUILTIN.items():
        if key not in have:
            StatusDef.objects.create(workspace=workspace, key=key, label=Lead.Status(key).label, color=color, sort=sort,
                                     builtin=True, kind=BUILTIN_KIND.get(key, "normal"))


def defs(workspace):
    ensure(workspace)
    for key, kind in BUILTIN_KIND.items():  # statuses created before kinds existed
        StatusDef.objects.filter(workspace=workspace, key=key, kind="normal").update(kind=kind)
    return list(StatusDef.objects.filter(workspace=workspace))


def info(workspace):
    """key -> (label, color)"""
    return {d.key: (d.label, d.color) for d in defs(workspace)}


def keys(workspace):
    return {d.key for d in defs(workspace)}


def label(workspace, key):
    return info(workspace).get(key, (Lead.Status(key).label if key in Lead.Status.values else key, "#64748B"))[0]


def closed_keys(workspace):
    """Statuses that end the chase (closed / not relevant), built-in or custom: they leave the call queue."""
    return {d.key for d in defs(workspace) if d.kind in ("won", "lost")}


def create(workspace, name, color, kind="normal"):
    name = re.sub(r"\s+", " ", (name or "").strip())[:40]
    if not name:
        return None, "חובה לתת שם לסטטוס."
    if color not in PALETTE:
        color = PALETTE[7]
    existing = defs(workspace)
    if any(d.label == name for d in existing):
        return None, "כבר יש סטטוס בשם הזה."
    key = "c%d" % (max([int(d.key[1:]) for d in existing if re.fullmatch(r"c\d+", d.key)] + [0]) + 1)
    if kind not in ("normal", "won", "lost"):
        kind = "normal"
    return StatusDef.objects.create(workspace=workspace, key=key, label=name, color=color, kind=kind,
                                    sort=max(d.sort for d in existing) + 10), ""
