"""Decide which accessibility-tree nodes are worth showing to the agent."""

from __future__ import annotations

from typing import Any

# Roles that are interactive (always kept if they carry a backend node).
INTERACTIVE_ROLES = frozenset(
    {
        "button",
        "link",
        "textbox",
        "searchbox",
        "combobox",
        "checkbox",
        "radio",
        "switch",
        "menuitem",
        "menuitemcheckbox",
        "menuitemradio",
        "tab",
        "option",
        "slider",
        "spinbutton",
        "listbox",
        "textarea",
        "iframe",
        "Iframe",
    }
)

# Roles kept only when they provide a non-empty accessible name (context anchors).
CONTEXT_ROLES = frozenset({"heading", "image", "StaticText", "text"})


def role_of(node: dict[str, Any]) -> str:
    return (node.get("role") or {}).get("value", "") or ""


def name_of(node: dict[str, Any]) -> str:
    return ((node.get("name") or {}).get("value", "") or "").strip()


def _prop(node: dict[str, Any], prop_name: str) -> Any:
    for prop in node.get("properties", []) or []:
        if prop.get("name") == prop_name:
            return (prop.get("value") or {}).get("value")
    return None


def keep(node: dict[str, Any], interactive_only: bool) -> bool:
    """Whether a node should appear in the serialized snapshot."""
    if node.get("ignored"):
        return False
    if node.get("backendDOMNodeId") is None:
        return False
    role = role_of(node)
    if role in INTERACTIVE_ROLES:
        return True
    if not interactive_only and role in CONTEXT_ROLES and name_of(node):
        return True
    return False


def is_interactive(node: dict[str, Any]) -> bool:
    return role_of(node) in INTERACTIVE_ROLES


def _normalize(text: str) -> str:
    return " ".join(text.lower().split())


def own_text_of(node: dict[str, Any], by_id: dict[str, dict[str, Any]]) -> str:
    """Concatenate the visible text under `node` (its StaticText descendants),
    the way `textContent`/`innerText` would read it.

    The accessible name Chrome computes for a control (used by name_of/
    snapshot labels) is often the fused <label>/aria-label text, NOT what a
    plain DOM read of the element would show — e.g. a button whose <label>
    text is "CONDUCTOR" but whose own visible text is a placeholder like
    "Selecciona un conductor". Recursion stops at any OTHER interactive
    descendant so a button containing a nested control doesn't pull in that
    control's own label.
    """
    parts: list[str] = []

    def walk(n: dict[str, Any], is_root: bool) -> None:
        role = role_of(n)
        if role == "StaticText":
            parts.append(name_of(n))
            return
        if not is_root and role in INTERACTIVE_ROLES:
            return
        for cid in n.get("childIds") or []:
            child = by_id.get(cid)
            if child is not None:
                walk(child, False)

    walk(node, True)
    return " ".join(p for p in parts if p)


def raw_text_display(node: dict[str, Any], by_id: dict[str, dict[str, Any]]) -> str | None:
    """Return the raw own-text of `node` (original casing/spacing) ONLY if it
    differs meaningfully from the computed accessible name — None otherwise,
    so the serializer doesn't clutter every line with a redundant duplicate."""
    raw = own_text_of(node, by_id)
    if raw and _normalize(raw) != _normalize(name_of(node)):
        return raw
    return None


def controls_backend_ids(node: dict[str, Any]) -> list[int]:
    """backendDOMNodeId(s) an expanded control's aria-controls/aria-owns
    points at, per the AX tree's `controls`/`owns` relationship properties."""
    ids: list[int] = []
    for prop_name in ("controls", "owns"):
        value = (_prop_raw(node, prop_name) or {}).get("relatedNodes") or []
        for related in value:
            backend_id = related.get("backendDOMNodeId")
            if backend_id is not None:
                ids.append(backend_id)
    return ids


def _prop_raw(node: dict[str, Any], prop_name: str) -> dict[str, Any] | None:
    for prop in node.get("properties", []) or []:
        if prop.get("name") == prop_name:
            return prop.get("value")
    return None


def is_expanded(node: dict[str, Any]) -> bool:
    return _prop(node, "expanded") is True


def compact_attrs(node: dict[str, Any]) -> str:
    """Render the few state attributes the agent needs, e.g. {required,checked}."""
    flags: list[str] = []
    for name in ("required", "disabled", "expanded"):
        if _prop(node, name) is True:
            flags.append(name)
    checked = _prop(node, "checked")
    if checked in ("true", True):
        flags.append("checked")
    elif checked == "mixed":
        flags.append("mixed")
    return " {" + ",".join(flags) + "}" if flags else ""
