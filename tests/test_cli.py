import io

from imbridge import cli
from imbridge.guard import write_allowed


def test_allow_needs_a_person_at_a_terminal(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("y\n"))  # piped input, as a script or an agent would give it
    assert cli.main(["allow", "+15551234567"]) == 1
    assert "must be run by a person" in capsys.readouterr().err
    assert cli.main(["allow", "--any"]) == 1


def test_allowed_lists_nothing_at_first(capsys):
    assert cli.main(["allowed"]) == 0
    assert "read-only" in capsys.readouterr().out


def test_disallow(capsys):
    write_allowed({"*"})
    assert cli.main(["disallow", "--any"]) == 0
    assert cli.main(["allowed"]) == 0
    assert "read-only" in capsys.readouterr().out
