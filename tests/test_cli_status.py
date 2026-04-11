"""Day 15 CLI smoke tests for the status surface.

End-to-end argparse wiring for `cortex list --status`, `cortex list --all`,
`cortex show` (displays Status: line), `cortex add --status`, and
`cortex status <id> <new_status>`. Pipes stdout through `capsys` so we
can assert on the rendered text.
"""
from __future__ import annotations

from pathlib import Path

from cortex.cli import main as cli_main
from cortex.store import CortexStore


def _seed(tmp_path: Path) -> str:
    db = str(tmp_path / "store.db")
    s = CortexStore(db)
    s.add_tripwire(
        id="act_one", title="active one", severity="high",
        domain="d", triggers=["x"], body="b",
    )
    s.add_tripwire(
        id="shd_one", title="shadow one", severity="high",
        domain="d", triggers=["x"], body="b", status="shadow",
    )
    s.add_tripwire(
        id="arc_one", title="archived one", severity="high",
        domain="d", triggers=["x"], body="b", status="archived",
    )
    s.close()
    return db


def test_cli_list_defaults_to_active(tmp_path, capsys):
    db = _seed(tmp_path)
    assert cli_main(["--db", db, "list"]) == 0
    out = capsys.readouterr().out
    assert "act_one" in out
    assert "shd_one" not in out
    assert "arc_one" not in out


def test_cli_list_status_shadow(tmp_path, capsys):
    db = _seed(tmp_path)
    assert cli_main(["--db", db, "list", "--status", "shadow"]) == 0
    out = capsys.readouterr().out
    assert "shd_one" in out
    assert "act_one" not in out


def test_cli_list_all_shows_every_status(tmp_path, capsys):
    db = _seed(tmp_path)
    assert cli_main(["--db", db, "list", "--all"]) == 0
    out = capsys.readouterr().out
    assert "act_one" in out
    assert "shd_one" in out
    assert "arc_one" in out


def test_cli_show_displays_status(tmp_path, capsys):
    db = _seed(tmp_path)
    assert cli_main(["--db", db, "show", "shd_one"]) == 0
    out = capsys.readouterr().out
    assert "Status:" in out
    assert "shadow" in out


def test_cli_add_with_status_shadow(tmp_path, capsys):
    db = str(tmp_path / "store.db")
    CortexStore(db).close()
    ret = cli_main([
        "--db", db, "add",
        "--id", "new_sh",
        "--title", "new shadow rule",
        "--severity", "high",
        "--domain", "d",
        "--triggers", "a,b",
        "--body", "body text",
        "--status", "shadow",
    ])
    assert ret == 0
    s = CortexStore(db)
    try:
        assert s.get_tripwire("new_sh")["status"] == "shadow"
    finally:
        s.close()
    out = capsys.readouterr().out
    assert "status=shadow" in out


def test_cli_status_transitions(tmp_path, capsys):
    db = _seed(tmp_path)
    # shadow -> active
    assert cli_main(["--db", db, "status", "shd_one", "active"]) == 0
    s = CortexStore(db)
    try:
        assert s.get_tripwire("shd_one")["status"] == "active"
    finally:
        s.close()


def test_cli_status_unknown_id_fails(tmp_path, capsys):
    db = _seed(tmp_path)
    assert cli_main(["--db", db, "status", "nope", "shadow"]) == 1


def test_cli_inbox_approve_with_shadow_flag(tmp_path, capsys, monkeypatch):
    """`cortex inbox approve --shadow` must promote the draft as shadow."""
    from cortex.inbox import write_draft

    monkeypatch.setenv("CORTEX_INBOX_DIR", str(tmp_path / "inbox"))
    draft_id = write_draft(
        {
            "id": "promoted_rule",
            "title": "promoted",
            "severity": "high",
            "domain": "d",
            "triggers": ["t1", "t2"],
            "body": "body body body",
        },
        source="manual",
    )
    assert draft_id

    db = str(tmp_path / "store.db")
    CortexStore(db).close()
    ret = cli_main(["--db", db, "inbox", "approve", "--shadow", draft_id])
    assert ret == 0

    s = CortexStore(db)
    try:
        tw = s.get_tripwire("promoted_rule")
        assert tw is not None
        assert tw["status"] == "shadow"
    finally:
        s.close()
    out = capsys.readouterr().out
    assert "status=shadow" in out
