import pytest

from fast_browser_mcp.snapshot.refmap import RefMap, StaleRefError


def test_assign_returns_sequential_refs():
    rm = RefMap()
    rm.begin()
    r1 = rm.assign(backend_node_id=10, role="button", name="OK")
    r2 = rm.assign(backend_node_id=11, role="link", name="Home")
    assert r1 == "@e1"
    assert r2 == "@e2"
    assert rm.resolve("@e1").backend_node_id == 10
    assert rm.resolve("@e2").role == "link"


def test_begin_bumps_generation_and_invalidates_old_refs():
    rm = RefMap()
    rm.begin()
    rm.assign(backend_node_id=1, role="button", name="A")
    assert rm.snapshot_id == 1
    rm.begin()
    assert rm.snapshot_id == 2
    with pytest.raises(StaleRefError):
        rm.resolve("@e1")


def test_resolve_unknown_ref_raises():
    rm = RefMap()
    rm.begin()
    with pytest.raises(StaleRefError):
        rm.resolve("@e99")


def test_stale_ref_from_previous_generation_does_not_resolve_to_wrong_node():
    """A ref valid in generation N must not silently match a same-named ref
    assigned in generation N+1 — it should raise StaleRefError instead of
    resolving to whatever node happens to get that ref string next."""
    rm = RefMap()
    rm.begin()
    old_ref = rm.assign(backend_node_id=1, role="button", name="Old target")
    rm.begin()
    # New generation reuses the same DOM but the LLM held on to `old_ref`
    # from before the click that re-rendered the page.
    rm.assign(backend_node_id=2, role="button", name="New target")
    rm.assign(backend_node_id=3, role="button", name="Another target")
    with pytest.raises(StaleRefError):
        rm.resolve(old_ref)


def test_current_session_applied_when_not_overridden():
    rm = RefMap()
    rm.begin()
    rm.current_session = "SESS"
    rm.assign(backend_node_id=5, role="textbox", name="Email")
    assert rm.resolve("@e1").session_id == "SESS"
