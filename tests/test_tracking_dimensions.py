import sys
import types

try:
    import supabase  # noqa: F401
except ImportError:
    supabase_stub = types.ModuleType("supabase")
    sys.modules["supabase"] = supabase_stub
else:
    supabase_stub = supabase

class _ClientOptions:
    def __init__(self, *args, **kwargs):
        pass


supabase_stub.Client = getattr(supabase_stub, "Client", type("Client", (), {}))
supabase_stub.ClientOptions = getattr(supabase_stub, "ClientOptions", _ClientOptions)
supabase_stub.create_client = getattr(supabase_stub, "create_client", lambda url, key, **_kwargs: object())

if "jwt" not in sys.modules:
    jwt_stub = types.ModuleType("jwt")

    class PyJWTError(Exception):
        pass

    class ExpiredSignatureError(PyJWTError):
        pass

    class PyJWKClient:
        def __init__(self, *args, **kwargs):
            pass

        def get_signing_key_from_jwt(self, *_args, **_kwargs):
            return type("SigningKey", (), {"key": "test-key"})()

    jwt_stub.PyJWTError = PyJWTError
    jwt_stub.ExpiredSignatureError = ExpiredSignatureError
    jwt_stub.PyJWKClient = PyJWKClient
    jwt_stub.decode = lambda *_args, **_kwargs: {"sub": "user-1"}
    sys.modules["jwt"] = jwt_stub

if "cachetools" not in sys.modules:
    cachetools_stub = types.ModuleType("cachetools")

    class TTLCache(dict):
        def __init__(self, *args, **kwargs):
            super().__init__()

    cachetools_stub.TTLCache = TTLCache
    sys.modules["cachetools"] = cachetools_stub

helpers_mod = sys.modules.get("app.services.invoice_extraction_service._helpers")
if helpers_mod is None:
    helpers_mod = types.ModuleType("app.services.invoice_extraction_service._helpers")
    helpers_mod.get_organisation_extraction_settings = lambda *_args, **_kwargs: {}
    helpers_mod.update_organisation_extraction_settings = lambda *_args, **_kwargs: {}
    sys.modules["app.services.invoice_extraction_service._helpers"] = helpers_mod

try:
    import fastapi  # noqa: F401
except ImportError:
    fastapi_stub = types.ModuleType("fastapi")

    class HTTPException(Exception):
        def __init__(self, status_code=None, detail=None, *args, **kwargs):
            self.status_code = status_code
            self.detail = detail if detail is not None else (args[0] if args else None)
            super().__init__(self.detail)

    class APIRouter:
        def __init__(self, *args, **kwargs):
            pass

        def get(self, *args, **kwargs):
            def decorator(fn):
                return fn

            return decorator

        def post(self, *args, **kwargs):
            def decorator(fn):
                return fn

            return decorator

        def put(self, *args, **kwargs):
            def decorator(fn):
                return fn

            return decorator

    fastapi_stub.HTTPException = HTTPException
    fastapi_stub.APIRouter = APIRouter
    fastapi_stub.Depends = lambda dependency=None: dependency
    fastapi_stub.Header = lambda default=None, **_kwargs: default
    fastapi_stub.Query = lambda default=None, **_kwargs: default
    sys.modules["fastapi"] = fastapi_stub

from conftest import MemoryDB

import app.routers.organisations as organisations
from app.routers.organisations import (
    CreateTrackingDimensionRequest,
    archive_tracking_dimension,
    create_tracking_dimension,
    list_tracking_dimensions,
)


def _patch_auth(monkeypatch):
    monkeypatch.setattr(organisations, "ensure_org_read", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(organisations, "ensure_org_admin", lambda *_args, **_kwargs: None)


def test_create_tracking_dimension_returns_active_and_visible_with_values(monkeypatch):
    _patch_auth(monkeypatch)
    db = MemoryDB({"tracking_dimensions": [], "tracking_values": []})

    created = create_tracking_dimension(
        "org-1",
        CreateTrackingDimensionRequest(name="Department"),
        ("user-1", db),
    )
    assert created["active"] is True
    assert created["position"] == 1

    db.tables["tracking_values"].append({
        "id": "value-sales",
        "dimension_id": created["id"],
        "code": "SALES",
        "name": "Sales",
        "active": True,
        "sort_order": 10,
    })

    listed = list_tracking_dimensions("org-1", ("user-1", db))
    assert [row["name"] for row in listed] == ["Department"]
    assert listed[0]["values"][0]["name"] == "Sales"


def test_create_tracking_dimension_without_position_assigns_lowest_free(monkeypatch):
    _patch_auth(monkeypatch)
    db = MemoryDB({
        "tracking_dimensions": [
            {"id": "dim-1", "organisation_id": "org-1", "name": "Department", "position": 1, "active": True},
            {"id": "dim-3", "organisation_id": "org-1", "name": "Country", "position": 3, "active": True},
        ],
        "tracking_values": [],
    })

    created = create_tracking_dimension(
        "org-1",
        CreateTrackingDimensionRequest(name="Project"),
        ("user-1", db),
    )

    assert created["position"] == 2


def test_create_tracking_dimension_rejects_duplicate_position(monkeypatch):
    _patch_auth(monkeypatch)
    db = MemoryDB({
        "tracking_dimensions": [
            {"id": "dim-1", "organisation_id": "org-1", "name": "Department", "position": 1, "active": True},
        ],
        "tracking_values": [],
    })

    try:
        create_tracking_dimension(
            "org-1",
            CreateTrackingDimensionRequest(name="Project", position=1),
            ("user-1", db),
        )
    except Exception as exc:
        assert getattr(exc, "status_code", None) == 400
        assert "position 1 is already in use" in getattr(exc, "detail", str(exc))
    else:
        raise AssertionError("Expected duplicate position to be rejected")


def test_archive_hides_dimension_unless_archived_included(monkeypatch):
    _patch_auth(monkeypatch)
    db = MemoryDB({
        "tracking_dimensions": [
            {"id": "dim-1", "organisation_id": "org-1", "name": "Department", "position": 1, "active": True},
        ],
        "tracking_values": [],
    })

    archived = archive_tracking_dimension("org-1", "dim-1", ("user-1", db))
    assert archived["active"] is False

    assert list_tracking_dimensions("org-1", ("user-1", db)) == []
    included = list_tracking_dimensions("org-1", ("user-1", db), include_archived=True)
    assert [row["name"] for row in included] == ["Department"]
