"""The injury-refresh cron entry point reports each outcome as its exit status."""

import json
from types import SimpleNamespace

from app.errors import ProviderUnavailableError
from scripts import injury_refresh


class _Engine:
    def dispose(self):
        pass


def _run(monkeypatch, capsys, service):
    import app.dependencies as dependencies

    monkeypatch.setattr(
        dependencies,
        "build_dependencies",
        lambda settings: SimpleNamespace(injury_refresh_service=service, engine=_Engine()),
    )
    code = injury_refresh.main()
    return code, json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def test_a_refresh_prints_its_result_and_exits_zero(monkeypatch, capsys):
    service = SimpleNamespace(run=lambda: {"state": "no_games", "games": 0})

    assert _run(monkeypatch, capsys, service) == (0, {"games": 0, "state": "no_games"})


def test_a_provider_outage_exits_nonzero_so_the_run_is_flagged(monkeypatch, capsys):
    def run():
        raise ProviderUnavailableError("The injury provider could not be reached.")

    code, printed = _run(monkeypatch, capsys, SimpleNamespace(run=run))

    assert code == 1
    assert printed["state"] == "provider_unavailable"
