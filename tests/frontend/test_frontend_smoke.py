from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "frontend"


def test_frontend_package_json_exists() -> None:
    assert (FRONTEND / "package.json").exists()


def test_app_contains_investigation_list_and_detail_ui_strings() -> None:
    app = (FRONTEND / "src" / "App.tsx").read_text(encoding="utf-8")

    assert "Investigation List" in app
    assert "Investigation Detail" in app
    assert "Evidence List" in app
    assert "Recommended Actions" in app
    assert "·" not in app


def test_frontend_uses_vite_proxy_by_default() -> None:
    api = (FRONTEND / "src" / "api.ts").read_text(encoding="utf-8")
    vite_config = (FRONTEND / "vite.config.ts").read_text(encoding="utf-8")

    assert 'import.meta.env.VITE_API_BASE_URL ?? ""' in api
    assert '"/investigations": "http://127.0.0.1:8000"' in vite_config


def test_ui_does_not_claim_production_changes_are_automatic() -> None:
    app = (FRONTEND / "src" / "App.tsx").read_text(encoding="utf-8").lower()

    forbidden_claims = [
        "automatically execute rollback",
        "automatically executes rollback",
        "automatically executed rollback",
        "automatic rollback",
        "automatically execute restart",
        "automatically executes restart",
        "automatically executed restart",
        "automatic restart",
        "automatically execute scale",
        "automatically executes scale",
        "automatically executed scale",
        "automatic scale",
        "automatically execute config change",
        "automatically executes config change",
        "automatically executed config change",
        "automatic config change",
    ]

    for claim in forbidden_claims:
        assert claim not in app
