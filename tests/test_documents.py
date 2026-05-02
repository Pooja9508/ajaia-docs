"""
Integration tests for Ajaia Docs API.

Uses FastAPI's TestClient against an in-memory SQLite database.
Run with: pytest tests/ -v
"""

import os
import sys

# Must set env vars BEFORE importing the app modules so database.py picks them up
os.environ["DATABASE_URL"]  = "sqlite:///./test_ajaia_docs.db"
os.environ["SECRET_KEY"]    = "test-only-secret-key-do-not-use-in-production"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient

from database import Base, engine, SessionLocal
import models
from main import app

# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope="session", autouse=True)
def setup_test_db():
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)
    if os.path.exists("test_ajaia_docs.db"):
        os.remove("test_ajaia_docs.db")


@pytest.fixture(scope="session")
def client():
    return TestClient(app)


def _login(client, username="alice", password="password123"):
    res = client.post("/api/auth/login", json={"username": username, "password": password})
    assert res.status_code == 200, res.text
    return res.json()["access_token"]


def _headers(token):
    return {"Authorization": f"Bearer {token}"}


# ── Auth ──────────────────────────────────────────────────────────────────────

def test_login_success(client):
    res = client.post("/api/auth/login", json={"username": "alice", "password": "password123"})
    assert res.status_code == 200
    data = res.json()
    assert "access_token" in data
    assert data["user"]["username"] == "alice"


def test_login_wrong_password(client):
    res = client.post("/api/auth/login", json={"username": "alice", "password": "wrong"})
    assert res.status_code == 401


def test_login_unknown_user(client):
    res = client.post("/api/auth/login", json={"username": "nobody", "password": "x"})
    assert res.status_code == 401


def test_get_me(client):
    token = _login(client)
    res = client.get("/api/auth/me", headers=_headers(token))
    assert res.status_code == 200
    assert res.json()["username"] == "alice"


def test_unauthenticated_request_rejected(client):
    res = client.get("/api/documents")
    assert res.status_code == 403


# ── Document CRUD ─────────────────────────────────────────────────────────────

def test_create_document(client):
    token = _login(client)
    res = client.post(
        "/api/documents",
        json={"title": "My Test Doc", "content_delta": '{"ops":[{"insert":"Hello\\n"}]}'},
        headers=_headers(token),
    )
    assert res.status_code == 200
    doc = res.json()
    assert doc["title"] == "My Test Doc"
    assert doc["is_owner"] is True
    assert doc["permission"] == "owner"


def test_list_documents(client):
    token = _login(client)
    res = client.get("/api/documents", headers=_headers(token))
    assert res.status_code == 200
    data = res.json()
    assert "owned" in data
    assert "shared" in data
    assert isinstance(data["owned"], list)


def test_get_document(client):
    token = _login(client)
    # Create
    create_res = client.post("/api/documents", json={"title": "Get Test"}, headers=_headers(token))
    doc_id = create_res.json()["id"]
    # Get
    res = client.get(f"/api/documents/{doc_id}", headers=_headers(token))
    assert res.status_code == 200
    assert res.json()["title"] == "Get Test"


def test_update_document(client):
    token = _login(client)
    doc_id = client.post("/api/documents", json={"title": "Old Title"}, headers=_headers(token)).json()["id"]
    res = client.put(
        f"/api/documents/{doc_id}",
        json={"title": "New Title", "word_target": 200},
        headers=_headers(token),
    )
    assert res.status_code == 200
    updated = res.json()
    assert updated["title"] == "New Title"
    assert updated["word_target"] == 200


def test_delete_document(client):
    token = _login(client)
    doc_id = client.post("/api/documents", json={"title": "To Delete"}, headers=_headers(token)).json()["id"]
    del_res = client.delete(f"/api/documents/{doc_id}", headers=_headers(token))
    assert del_res.status_code == 200
    get_res = client.get(f"/api/documents/{doc_id}", headers=_headers(token))
    assert get_res.status_code == 404


def test_get_nonexistent_document(client):
    token = _login(client)
    res = client.get("/api/documents/999999", headers=_headers(token))
    assert res.status_code == 404


# ── Sharing ───────────────────────────────────────────────────────────────────

def test_share_and_access_document(client):
    alice_token = _login(client, "alice")
    bob_token   = _login(client, "bob")

    # Alice creates a doc
    doc_id = client.post(
        "/api/documents",
        json={"title": "Shared Doc"},
        headers=_headers(alice_token),
    ).json()["id"]

    # Bob cannot access it yet
    res = client.get(f"/api/documents/{doc_id}", headers=_headers(bob_token))
    assert res.status_code == 403

    # Alice shares with bob as editor
    share_res = client.post(
        f"/api/documents/{doc_id}/shares",
        json={"username": "bob", "permission": "editor"},
        headers=_headers(alice_token),
    )
    assert share_res.status_code == 200

    # Now bob can access it
    res = client.get(f"/api/documents/{doc_id}", headers=_headers(bob_token))
    assert res.status_code == 200
    assert res.json()["permission"] == "editor"

    # Bob appears in shared list
    bob_list = client.get("/api/documents", headers=_headers(bob_token)).json()
    shared_ids = [d["id"] for d in bob_list["shared"]]
    assert doc_id in shared_ids


def test_viewer_cannot_edit(client):
    alice_token = _login(client, "alice")
    carol_token = _login(client, "carol")

    doc_id = client.post(
        "/api/documents", json={"title": "Viewer Only"}, headers=_headers(alice_token)
    ).json()["id"]

    client.post(
        f"/api/documents/{doc_id}/shares",
        json={"username": "carol", "permission": "viewer"},
        headers=_headers(alice_token),
    )

    edit_res = client.put(
        f"/api/documents/{doc_id}",
        json={"title": "Hacked Title"},
        headers=_headers(carol_token),
    )
    assert edit_res.status_code == 403


def test_revoke_share(client):
    alice_token = _login(client, "alice")
    bob_token   = _login(client, "bob")

    doc_id = client.post(
        "/api/documents", json={"title": "Revoke Test"}, headers=_headers(alice_token)
    ).json()["id"]

    share_id = client.post(
        f"/api/documents/{doc_id}/shares",
        json={"username": "bob", "permission": "viewer"},
        headers=_headers(alice_token),
    ).json()   # returns {"message": ..., "permission": ...}

    # Get share id
    shares = client.get(f"/api/documents/{doc_id}/shares", headers=_headers(alice_token)).json()
    bob_share = next(s for s in shares if s["username"] == "bob")

    client.delete(
        f"/api/documents/{doc_id}/shares/{bob_share['id']}",
        headers=_headers(alice_token),
    )

    # Bob no longer has access
    res = client.get(f"/api/documents/{doc_id}", headers=_headers(bob_token))
    assert res.status_code == 403


# ── Section sharing ───────────────────────────────────────────────────────────

def test_section_share(client):
    alice_token = _login(client, "alice")
    bob_token   = _login(client, "bob")

    doc_id = client.post(
        "/api/documents",
        json={"title": "Section Doc", "content_delta": '{"ops":[{"insert":"Intro"},{"insert":"\\n","attributes":{"header":1}},{"insert":"Body text\\n"}]}'},
        headers=_headers(alice_token),
    ).json()["id"]

    res = client.post(
        f"/api/documents/{doc_id}/section-shares",
        json={"username": "bob", "section_heading": "Intro", "section_index": 1, "permission": "viewer"},
        headers=_headers(alice_token),
    )
    assert res.status_code == 200

    ss_list = client.get(f"/api/documents/{doc_id}/section-shares", headers=_headers(alice_token)).json()
    assert any(s["section_heading"] == "Intro" and s["username"] == "bob" for s in ss_list)


# ── Comments ──────────────────────────────────────────────────────────────────

def test_add_and_list_comments(client):
    token = _login(client, "alice")
    doc_id = client.post("/api/documents", json={"title": "Comment Doc"}, headers=_headers(token)).json()["id"]

    add_res = client.post(
        f"/api/documents/{doc_id}/comments",
        json={"content": "This is a comment", "selection_index": 0, "selection_length": 5},
        headers=_headers(token),
    )
    assert add_res.status_code == 200
    comment_id = add_res.json()["id"]

    list_res = client.get(f"/api/documents/{doc_id}/comments", headers=_headers(token))
    assert list_res.status_code == 200
    assert any(c["id"] == comment_id for c in list_res.json())


def test_resolve_comment(client):
    token = _login(client, "alice")
    doc_id = client.post("/api/documents", json={"title": "Resolve Test"}, headers=_headers(token)).json()["id"]
    comment_id = client.post(
        f"/api/documents/{doc_id}/comments",
        json={"content": "resolve me"},
        headers=_headers(token),
    ).json()["id"]

    res = client.put(f"/api/comments/{comment_id}/resolve", headers=_headers(token))
    assert res.status_code == 200
    assert res.json()["resolved"] is True


# ── Version history ───────────────────────────────────────────────────────────

def test_version_history(client):
    token = _login(client, "alice")
    doc_id = client.post("/api/documents", json={"title": "Version Doc"}, headers=_headers(token)).json()["id"]

    # First update creates a snapshot
    client.put(
        f"/api/documents/{doc_id}",
        json={"content_delta": '{"ops":[{"insert":"v1\\n"}]}'},
        headers=_headers(token),
    )
    client.put(
        f"/api/documents/{doc_id}",
        json={"content_delta": '{"ops":[{"insert":"v2\\n"}]}'},
        headers=_headers(token),
    )

    versions = client.get(f"/api/documents/{doc_id}/versions", headers=_headers(token)).json()
    assert len(versions) >= 1
