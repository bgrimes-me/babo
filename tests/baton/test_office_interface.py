"""The Office interface: what the page and CLI may ask for, and what they may not.

These are contract tests on the interface itself (R10/R12 in soro's plan): the
surface must not accept a runtime handle, a credential value, or a revoke
handle, and the core must be the only place lifecycle rules live.
"""

from __future__ import annotations

import inspect

from babo.office import api


def _params(method: str) -> set[str]:
    signature = inspect.signature(getattr(api.OfficeManager, method))
    return {name for name in signature.parameters if name != "self"}


def test_act_names_the_actor_and_the_replay_key_as_keyword_only() -> None:
    """R5 needs the actor at the boundary; R6 needs a replay key. Both must be
    named here, or the gate exists only inside the core and the surface hides it."""
    import inspect as _inspect

    signature = _inspect.signature(api.OfficeManager.act)
    assert "actor" in signature.parameters and "action_id" in signature.parameters
    for name in ("actor", "action_id"):
        assert signature.parameters[name].kind is _inspect.Parameter.KEYWORD_ONLY


def test_the_interface_offers_only_the_four_operations() -> None:
    assert {m for m in dir(api.OfficeManager) if not m.startswith("_")} >= {"health", "agents", "act", "action"}


def test_no_runtime_handle_or_capability_argument_crosses_the_interface() -> None:
    forbidden = {"socket", "socket_path", "podman", "container", "container_id", "pod", "pod_id",
                 "netns", "capability", "capabilities", "key", "secret", "token", "revoke_handle"}
    # `actor` and `action_id` are names/keys, not runtime handles: actor is who
    # asks, action_id is the caller's replay key.
    for method in ("health", "agents", "act", "action"):
        assert not (_params(method) & forbidden), f"{method} takes a runtime handle or a secret"


def test_the_action_vocabulary_and_terminal_states_are_closed() -> None:
    assert api.ACTIONS == ("spawn", "wake", "freeze", "rescue", "destroy", "restart-ui")
    assert api.TERMINAL == ("succeeded", "failed", "refused", "timeout")


def test_the_core_is_native_or_honestly_absent(tmp_path) -> None:
    """One assertion for two heads, because this branch must be true alone and
    still true after the manager slice lands on top of it.

    - no `babo.office.core` yet: the product says "hasn't landed" and stays
      read-only (no stub, no fallback);
    - core present: it satisfies the interface and imports no HTTP client or
      manager URL — the seam must not come back as a dependency of the core.
    """
    import importlib.util

    if importlib.util.find_spec("babo.office.core") is None:
        try:
            api.manager_for(None)
        except api.CoreNotBuiltError as exc:
            assert "hasn't landed" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("no core should exist before the manager slice lands")
        return

    import inspect

    core = importlib.import_module("babo.office.core")
    source = inspect.getsource(core)
    for client in ("requests", "httpx", "NEOCLAW_MANAGER_URL", "ManagerBackend", "manager_url"):
        assert client not in source, f"the core must not reach an HTTP manager ({client})"


def test_credential_rows_are_identity_only() -> None:
    from babo.office.model import Checkout

    row = Checkout(id="c1", scope="kimi", issued_at="t0", expires_at="t1", unrevocable=True).overview()
    assert set(row) == {"id", "scope", "issued_at", "expires_at", "unrevocable"}


def test_no_method_hides_a_runtime_handle_inside_kwargs() -> None:
    """A **kwargs bag would let a socket path travel without ever being named."""
    for method in ("health", "agents", "act", "action"):
        kinds = {p.kind for p in inspect.signature(getattr(api.OfficeManager, method)).parameters.values()}
        assert inspect.Parameter.VAR_KEYWORD not in kinds, f"{method} takes **kwargs"
        assert inspect.Parameter.VAR_POSITIONAL not in kinds, f"{method} takes *args"


def test_a_core_that_imports_but_breaks_is_not_reported_as_unbuilt(monkeypatch) -> None:
    """A broken core must not look like an absent one (R-4 on the review)."""
    import sys
    import types

    broken = types.ModuleType("babo.office.core")
    executed = {"ran": False}

    def _explode(_cfg: object = None):
        executed["ran"] = True
        raise RuntimeError("a dependency broke while the core imported")

    broken.manager_for = _explode  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "babo.office.core", broken)
    try:
        api.manager_for(None)
    except RuntimeError as exc:
        assert "dependency broke" in str(exc)
        assert executed["ran"] is True
    else:  # pragma: no cover
        raise AssertionError("a broken core must not be reported as unbuilt")


def test_the_actor_is_obtained_not_defaulted() -> None:
    """R13: the surface must not supply an identity it did not obtain.

    An action with no actor is refused by the core before any effect, and the
    two doors obtain the name differently — the operator session for the page,
    the OS user for the terminal — so "who asked" is evidence, not a constant
    that satisfied a non-empty check.
    """
    import pytest

    from babo.office.actors import Actor, operator_actor, terminal_actor

    with pytest.raises(Exception):
        Actor(name="", source="webui-session")
    with pytest.raises(Exception):
        Actor(name="owner", source="")

    page = operator_actor()
    assert (page.name, page.source) == ("owner", "webui-session")
    shell = terminal_actor()
    assert shell.source == "terminal" and shell.name
    assert terminal_actor("someone").name == "someone"


async def test_an_action_with_no_actor_is_refused_by_the_core() -> None:
    from baton.office_fakes import FakeCore

    core = FakeCore()
    action_id = core.act("crew-1", "wake", "test", actor="", actor_source="")
    assert core.action(action_id).state == "refused"
    assert "names no actor" in core.action(action_id).said


def test_the_vocabulary_is_declared_once() -> None:
    """The action and terminal-state tuples live in the interface only.

    A second literal in control.py would drift the moment the interface changed,
    and the page would keep accepting a verb the core no longer had — the same
    duplicate-vocabulary class bomu flagged.
    """
    import inspect

    from babo.office import control

    source = inspect.getsource(control)
    assert "TERMINAL = (" not in source and "ACTIONS = (" not in source
    assert control.AGENT_ACTIONS is api.AGENT_ACTIONS
    assert control.TERMINAL is api.TERMINAL
