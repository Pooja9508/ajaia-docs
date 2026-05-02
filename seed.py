"""Seed demo users and a welcome document.

Called automatically by main.py on startup (only if DB is empty).
Can also be run directly: python seed.py
"""

import json
from auth import hash_password
import models

DEMO_USERS = [
    {"username": "alice", "email": "alice@ajaia.com", "password": "password123"},
    {"username": "bob", "email": "bob@ajaia.com", "password": "password123"},
    {"username": "carol", "email": "carol@ajaia.com", "password": "password123"},
]

WELCOME_DELTA = json.dumps({
    "ops": [
        {"insert": "Welcome to Ajaia Docs", "attributes": {}},
        {"insert": "\n", "attributes": {"header": 1}},
        {"insert": "\n"},
        {"insert": "This is your collaborative document editor. Here is what you can do:", "attributes": {}},
        {"insert": "\n"},
        {"insert": "Rich-text editing", "attributes": {"bold": True}},
        {"insert": " — bold, italic, underline, headings, lists, code blocks"},
        {"insert": "\n", "attributes": {"list": "bullet"}},
        {"insert": "Real-time presence", "attributes": {"bold": True}},
        {"insert": " — see who is editing alongside you"},
        {"insert": "\n", "attributes": {"list": "bullet"}},
        {"insert": "Document & section sharing", "attributes": {"bold": True}},
        {"insert": " — share the full doc or individual sections by heading"},
        {"insert": "\n", "attributes": {"list": "bullet"}},
        {"insert": "File import", "attributes": {"bold": True}},
        {"insert": " — upload .txt, .md, or .docx to create a new document"},
        {"insert": "\n", "attributes": {"list": "bullet"}},
        {"insert": "Grammar & spell check", "attributes": {"bold": True}},
        {"insert": " — powered by LanguageTool (no API key required)"},
        {"insert": "\n", "attributes": {"list": "bullet"}},
        {"insert": "Word-count goal", "attributes": {"bold": True}},
        {"insert": " — set a target and track progress with a live progress bar"},
        {"insert": "\n", "attributes": {"list": "bullet"}},
        {"insert": "Version history", "attributes": {"bold": True}},
        {"insert": " — last 10 auto-saved snapshots, restore any version"},
        {"insert": "\n", "attributes": {"list": "bullet"}},
        {"insert": "Export to Markdown", "attributes": {"bold": True}},
        {"insert": " — download your document as a .md file"},
        {"insert": "\n", "attributes": {"list": "bullet"}},
        {"insert": "\n"},
        {"insert": "Getting Started", "attributes": {}},
        {"insert": "\n", "attributes": {"header": 2}},
        {"insert": "Log in with one of the demo accounts below and explore. Try sharing this document with bob or carol."},
        {"insert": "\n"},
        {"insert": "\n"},
        {"insert": "Demo accounts", "attributes": {}},
        {"insert": "\n", "attributes": {"header": 3}},
        {"insert": "alice / password123"},
        {"insert": "\n", "attributes": {"list": "bullet"}},
        {"insert": "bob / password123"},
        {"insert": "\n", "attributes": {"list": "bullet"}},
        {"insert": "carol / password123"},
        {"insert": "\n", "attributes": {"list": "bullet"}},
    ]
})


def do_seed(db) -> None:
    created = []
    for u in DEMO_USERS:
        user = models.User(
            username=u["username"],
            email=u["email"],
            hashed_password=hash_password(u["password"]),
        )
        db.add(user)
        created.append(user)
    db.commit()
    for u in created:
        db.refresh(u)

    welcome = models.Document(
        title="Welcome to Ajaia Docs",
        content_delta=WELCOME_DELTA,
        owner_id=created[0].id,
    )
    db.add(welcome)
    db.commit()
    print("DB seeded — demo users: alice, bob, carol (password: password123)")


if __name__ == "__main__":
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from database import SessionLocal, Base, engine
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        if db.query(models.User).count() == 0:
            do_seed(db)
        else:
            print("DB already seeded — skipping.")
    finally:
        db.close()
