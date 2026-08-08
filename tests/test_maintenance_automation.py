from pathlib import Path

import yaml


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _load_yaml(relative_path: str) -> dict:
    with (REPOSITORY_ROOT / relative_path).open(encoding="utf-8") as config_file:
        return yaml.safe_load(config_file)


def test_dependabot_updates_python_and_github_actions_dependencies():
    config = _load_yaml(".github/dependabot.yml")
    ecosystems = {update["package-ecosystem"] for update in config["updates"]}

    assert ecosystems == {"github-actions", "pip"}
    assert all(update["schedule"]["interval"] for update in config["updates"])


def test_ci_uses_node_24_github_actions():
    workflow = (REPOSITORY_ROOT / ".github/workflows/tests.yml").read_text(
        encoding="utf-8"
    )

    assert "actions/checkout@" in workflow and "# v7." in workflow
    assert "actions/setup-python@" in workflow and "# v7." in workflow


def test_security_workflow_audits_lock_and_scans_secrets():
    workflow = (REPOSITORY_ROOT / ".github/workflows/security.yml").read_text(
        encoding="utf-8"
    )

    assert "pull_request:" in workflow
    assert "pip_audit" in workflow
    assert "--local" in workflow
    assert "requirements-lock.txt" in workflow
    assert "gitleaks/gitleaks-action@" in workflow and "# v3." in workflow
