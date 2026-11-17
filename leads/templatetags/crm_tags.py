from django import template

from .. import statuses

register = template.Library()


@register.simple_tag(takes_context=True)
def status_label(context, lead):
    return _info(context, lead).get(lead.status, (lead.status, "#64748B"))[0]


@register.simple_tag(takes_context=True)
def status_color(context, lead):
    return _info(context, lead).get(lead.status, (lead.status, "#64748B"))[1]


def _info(context, lead):
    req = context.get("request")
    cache = getattr(req, "_status_info", None) if req else None
    if cache is None:
        cache = {}
        if req:
            req._status_info = cache
    if lead.workspace not in cache:
        cache[lead.workspace] = statuses.info(lead.workspace)
    return cache[lead.workspace]


@register.filter
def initial(value):
    return (value or "?")[:1].upper()


@register.filter
def get_item(d, key):
    return d.get(key, 0) if d else 0


@register.filter
def money(value):
    try:
        return f"{int(round(float(value))):,}"
    except (TypeError, ValueError):
        return "0"
