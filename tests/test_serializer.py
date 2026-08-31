from fast_browser_mcp.snapshot.refmap import RefMap
from fast_browser_mcp.snapshot.serializer import serialize


def _node(node_id, role, name, backend, children=None, ignored=False, props=None):
    return {
        "nodeId": node_id,
        "role": {"value": role},
        "name": {"value": name},
        "backendDOMNodeId": backend,
        "ignored": ignored,
        "childIds": children or [],
        "properties": props or [],
    }


def _fixture():
    # root(WebArea) -> [heading, form] ; form -> [email textbox(required), submit button]
    return [
        _node("1", "WebArea", "Login", 100, children=["2", "3"]),
        _node("2", "heading", "Bienvenido", 101),
        _node("3", "form", "", 102, children=["4", "5"]),
        _node(
            "4", "textbox", "Email", 103,
            props=[{"name": "required", "value": {"value": True}}],
        ),
        _node("5", "button", "Entrar", 104),
    ]


def test_serialize_interactive_only_lists_refs_with_attrs():
    rm = RefMap()
    text = serialize(_fixture(), rm, interactive_only=True)
    lines = text.splitlines()
    assert lines == ['@e1 textbox "Email" {required}', '@e2 button "Entrar"']
    assert rm.resolve("@e1").backend_node_id == 103
    assert rm.resolve("@e2").role == "button"


def test_serialize_includes_headings_when_not_interactive_only():
    rm = RefMap()
    text = serialize(_fixture(), rm, interactive_only=False)
    assert '@e1 heading "Bienvenido"' in text
    assert "textbox" in text and "button" in text


def test_ignored_and_missing_backend_nodes_are_skipped():
    rm = RefMap()
    nodes = [
        _node("1", "WebArea", "", 1, children=["2", "3"]),
        _node("2", "button", "Hidden", 2, ignored=True),
        {"nodeId": "3", "role": {"value": "button"}, "name": {"value": "NoBackend"},
         "backendDOMNodeId": None, "ignored": False, "childIds": [], "properties": []},
    ]
    text = serialize(nodes, rm, interactive_only=True)
    assert text == ""
    assert len(rm) == 0


def test_long_static_text_is_truncated():
    rm = RefMap()
    long_name = "x" * 300
    nodes = [_node("1", "heading", long_name, 1)]
    text = serialize(nodes, rm, interactive_only=False)
    assert "…" in text
    assert len(text) < 200


def test_raw_text_shown_when_it_differs_from_accessible_name():
    # A button labelled "CONDUCTOR" by an associated <label>, but whose own
    # visible text is a placeholder — the accessible name (what `name_of`
    # reports) is what shows up in "..."; the raw StaticText child should
    # appear separately since it's genuinely different.
    rm = RefMap()
    nodes = [
        _node("1", "WebArea", "", 1, children=["2"]),
        _node("2", "button", "CONDUCTOR", 2, children=["3"]),
        _node("3", "StaticText", "Selecciona un conductor", 3),
    ]
    text = serialize(nodes, rm, interactive_only=True)
    assert '"CONDUCTOR"' in text
    assert '(txt: "Selecciona un conductor")' in text


def test_raw_text_hidden_when_it_matches_accessible_name():
    rm = RefMap()
    nodes = [
        _node("1", "WebArea", "", 1, children=["2"]),
        _node("2", "button", "Entrar", 2, children=["3"]),
        _node("3", "StaticText", "Entrar", 3),
    ]
    text = serialize(nodes, rm, interactive_only=True)
    assert "(txt:" not in text


def _with_controls(node_id, role, name, backend, controls_backend_ids, expanded=True):
    return _node(
        node_id, role, name, backend,
        props=[
            {"name": "expanded", "value": {"value": expanded}},
            {
                "name": "controls",
                "value": {"relatedNodes": [{"backendDOMNodeId": cid} for cid in controls_backend_ids]},
            },
        ],
    )


def test_open_popup_is_annotated_and_background_lookalike_is_not():
    # A combobox with aria-expanded=true / aria-controls pointing at a real
    # listbox that lives as a DIRECT CHILD OF THE ROOT (the universal "portal
    # to body" pattern) — plus an unrelated background option with the exact
    # same text, which must NOT get the marker.
    rm = RefMap()
    nodes = [
        _node("1", "WebArea", "", 1, children=["2", "3", "6"]),
        _with_controls("2", "combobox", "Conductor", 2, controls_backend_ids=[30]),
        _node("3", "listbox", "", 30, children=["4", "5"]),  # the REAL open popup
        _node("4", "option", "Juan Perez", 31),
        _node("5", "option", "Ana Gomez", 32),
        _node("6", "option", "Juan Perez", 33),  # unrelated background lookalike
    ]
    text = serialize(nodes, rm, interactive_only=True)
    lines = text.splitlines()

    # The marker is placed ONLY on the resolved popup root (the listbox),
    # never on its option children or on the unrelated background lookalike.
    marked_lines = [line for line in lines if "[POPUP-ABIERTO]" in line]
    assert len(marked_lines) == 1
    assert "listbox" in marked_lines[0]

    background_line = next(
        line for line in lines
        if line.strip().startswith("@") and rm.resolve(line.split()[0]).backend_node_id == 33
    )
    assert "[POPUP-ABIERTO]" not in background_line
