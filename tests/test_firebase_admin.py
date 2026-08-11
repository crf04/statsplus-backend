from app.config.settings import AuthenticationSettings, RuntimeSettings
from app.utils import firebase_admin as firebase_admin_utils


def test_invalid_credential_file_falls_through_to_hosted_json(monkeypatch, tmp_path):
    credential_path = tmp_path / "service-account.json"
    credential_path.write_text("not a service account", encoding="utf-8")
    hosted_credentials = {
        "project_id": "courtai-test",
        "private_key": "test-key",
        "client_email": "firebase@example.com",
    }
    settings = RuntimeSettings(
        auth=AuthenticationSettings(
            firebase_service_account_path=str(credential_path),
            firebase_service_account_json=(
                '{"project_id":"courtai-test","private_key":"test-key",'
                '"client_email":"firebase@example.com"}'
            ),
        )
    )
    initialized_app = object()

    def certificate(source):
        if source == str(credential_path):
            raise ValueError("invalid credential file")
        assert source == hosted_credentials
        return "hosted-credential"

    monkeypatch.setattr(firebase_admin_utils, "_firebase_app", None)
    monkeypatch.setattr(firebase_admin_utils.credentials, "Certificate", certificate)
    monkeypatch.setattr(
        firebase_admin_utils.firebase_admin,
        "initialize_app",
        lambda credential: initialized_app
        if credential == "hosted-credential"
        else None,
    )

    assert firebase_admin_utils.initialize_firebase_admin(settings) is initialized_app


def test_invalid_hosted_json_falls_through_to_individual_fields(monkeypatch):
    settings = RuntimeSettings(
        auth=AuthenticationSettings(
            firebase_service_account_json="not valid JSON",
            firebase_project_id="courtai-test",
            firebase_private_key="test-key",
            firebase_client_email="firebase@example.com",
        )
    )
    initialized_app = object()

    monkeypatch.setattr(firebase_admin_utils, "_firebase_app", None)
    monkeypatch.setattr(
        firebase_admin_utils.credentials,
        "Certificate",
        lambda source: "individual-credential",
    )
    monkeypatch.setattr(
        firebase_admin_utils.firebase_admin,
        "initialize_app",
        lambda credential: initialized_app,
    )

    assert firebase_admin_utils.initialize_firebase_admin(settings) is initialized_app


def test_invalid_individual_fields_fall_through_to_application_default(monkeypatch):
    settings = RuntimeSettings(
        auth=AuthenticationSettings(
            firebase_project_id="courtai-test",
            firebase_private_key="test-key",
            firebase_client_email="firebase@example.com",
        )
    )
    initialized_app = object()

    def invalid_individual_credentials(source):
        raise ValueError("invalid individual fields")

    monkeypatch.setattr(firebase_admin_utils, "_firebase_app", None)
    monkeypatch.setattr(
        firebase_admin_utils.credentials,
        "Certificate",
        invalid_individual_credentials,
    )
    monkeypatch.setattr(
        firebase_admin_utils.credentials,
        "ApplicationDefault",
        lambda: "application-default-credential",
    )
    monkeypatch.setattr(firebase_admin_utils.os.path, "exists", lambda path: True)
    monkeypatch.setattr(
        firebase_admin_utils.firebase_admin,
        "initialize_app",
        lambda credential: initialized_app,
    )

    assert firebase_admin_utils.initialize_firebase_admin(settings) is initialized_app


def test_all_invalid_credential_sources_return_none(monkeypatch):
    settings = RuntimeSettings(
        auth=AuthenticationSettings(firebase_service_account_json="not valid JSON")
    )

    monkeypatch.setattr(firebase_admin_utils, "_firebase_app", None)
    monkeypatch.setattr(firebase_admin_utils.os.path, "exists", lambda path: False)

    assert firebase_admin_utils.initialize_firebase_admin(settings) is None
