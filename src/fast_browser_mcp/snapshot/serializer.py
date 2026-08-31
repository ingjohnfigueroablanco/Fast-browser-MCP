"""Serialize a flat AX node list into compact indented text with @eN refs.

Output example:
    @e1 button "Iniciar sesion"
    @e2 textbox "Email" {required}
      @e3 heading "Bienvenido"
"""

from __future__ import annotations

from typing import Any

from ..config import STATIC_TEXT_TRUNCATE
from .filter import (
    compact_attrs,
    controls_backend_ids,
    is_expanded,
    keep,
    name_of,
    raw_text_display,
    role_of,
)
from .refmap import RefMap

# Roles that plausibly ARE a dropdown/menu/dialog container — forced visible
# (even under interactive_only=True) when they're the resolved target of an
# aria-controls/aria-owns relationship, since that IS the answer to "which
# popup is currently open" regardless of whether the role alone would
# normally earn a line in the snapshot.
_POPUP_CONTAINER_ROLES = frozenset({"listbox", "menu", "dialog", "tooltip", "grid", "tree"})


def _truncate(text: str) -> str:
    if len(text) > STATIC_TEXT_TRUNCATE:
        return text[: STATIC_TEXT_TRUNCATE - 1] + "…"
    return text


def _label(node: dict[str, Any], by_id: dict[str, Any], *, in_popup: bool) -> str:
    role = role_of(node)
    name = _truncate(name_of(node))
    parts = [role]
    if name:
        parts.append(f'"{name}"')
    line = " ".join(parts)
    line += compact_attrs(node)
    raw = raw_text_display(node, by_id)
    if raw:
        line += f' (txt: "{_truncate(raw)}")'
    if in_popup:
        line += " [POPUP-ABIERTO]"
    return line


def resolve_open_popup_ids(ax_nodes: list[dict[str, Any]], by_id: dict[str, Any]) -> set[str]:
    """nodeIds of the subtree(s) that a currently-expanded control's
    aria-controls/aria-owns points at (tier 1: pure ARIA, zero extra CDP
    calls — the AX tree already carries this). Returns nodeIds, not
    backendDOMNodeIds, since that's what the walk below keys off of."""
    by_backend_id = {n["backendDOMNodeId"]: n for n in ax_nodes if n.get("backendDOMNodeId") is not None}
    popup_root_ids: set[str] = set()
    for node in ax_nodes:
        if not is_expanded(node):
            continue
        for backend_id in controls_backend_ids(node):
            target = by_backend_id.get(backend_id)
            if target is not None:
                popup_root_ids.add(target["nodeId"])
    return popup_root_ids


def serialize(
    ax_nodes: list[dict[str, Any]],
    refmap: RefMap,
    interactive_only: bool = True,
    session_id: str | None = None,
) -> str:
    """Walk the AX tree depth-first, assign refs to kept nodes, return text.

    ``ax_nodes`` is the flat list from Accessibility.getFullAXTree. Nodes carry
    ``nodeId`` and ``childIds`` to reconstruct the hierarchy.
    """
    refmap.current_session = session_id
    by_id = {n["nodeId"]: n for n in ax_nodes}
    child_ids = {cid for n in ax_nodes for cid in (n.get("childIds") or [])}
    roots = [n for n in ax_nodes if n["nodeId"] not in child_ids]
    popup_root_ids = resolve_open_popup_ids(ax_nodes, by_id)

    lines: list[str] = []

    def walk(node: dict[str, Any], depth: int, in_popup: bool) -> None:
        next_depth = depth
        node_is_popup_root = node["nodeId"] in popup_root_ids
        node_in_popup = in_popup or node_is_popup_root
        force_visible = node_is_popup_root and role_of(node) in _POPUP_CONTAINER_ROLES
        if keep(node, interactive_only) or force_visible:
            ref = refmap.assign(
                backend_node_id=node["backendDOMNodeId"],
                role=role_of(node),
                name=name_of(node),
                session_id=session_id,
            )
            indent = "  " * depth
            lines.append(f"{indent}{ref} {_label(node, by_id, in_popup=node_is_popup_root)}".rstrip())
            next_depth = depth + 1
        for cid in node.get("childIds") or []:
            child = by_id.get(cid)
            if child is not None:
                walk(child, next_depth, node_in_popup)

    for root in roots:
        walk(root, 0, False)

    return "\n".join(lines)
