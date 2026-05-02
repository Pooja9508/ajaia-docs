# Architecture Note — Ajaia Docs

## System Overview

```
Browser
  │
  ├── GET /              → index.html  (login + registration)
  ├── GET /app           → app.html    (main editor shell)
  ├── GET /p/<token>     → public.html (public viewer, no auth)
  ├── REST /api/...      → FastAPI routes
  ├── WS  /ws/documents/<id>  → WebSocket presence channel
  └── GET /uploads/<file>     → StaticFiles (uploaded images)

FastAPI (main.py)
  ├── Auth: JWT (python-jose) + bcrypt (passlib)
  ├── ORM: SQLAlchemy 2.0, models in models.py
  ├── DB: SQLite (local) / PostgreSQL (Render)
  ├── Email: smtplib SMTP (email_service.py)
  └── Grammar proxy: httpx → api.languagetool.org
```

---

## Key Architectural Decisions

### 1. Single-server full-stack (FastAPI serves frontend + API)

FastAPI serves HTML files directly rather than running a separate frontend server. This eliminates CORS complexity, produces a single `uvicorn main:app` startup, and is easy to review and deploy.

**Trade-off:** A React/Vite SPA would give snappier UX for large teams and cleaner bundle splitting, but adds a build step and separate deployment — impractical within the project timebox.

### 2. SQLite for local dev, PostgreSQL for production

`database.py` reads `DATABASE_URL` from the environment. SQLite requires zero setup locally. `render.yaml` provisions a free Render PostgreSQL and injects the URL automatically. The only code difference is the `check_same_thread` flag for SQLite.

**Trade-off:** No Alembic migrations. `Base.metadata.create_all()` handles new tables at startup. New columns on existing tables are handled by `migrate.py` (plain `ALTER TABLE`). For a team project Alembic would be mandatory.

### 3. Quill.js Delta format for rich-text persistence

Quill's Delta is a compact, JSON-serialisable sequence of retain/insert/delete operations with inline attribute maps. Storing the full Delta as a TEXT column means:
- No lossy HTML round-trips on save
- Exact fidelity on load — same Delta re-set into Quill
- Version snapshots are full Delta copies, enabling exact restore

**Trade-off:** Delta is Quill-specific. A future migration to ProseMirror/Tiptap would require a converter.

### 4. Word-style two-tab toolbar (Home / Insert)

Quill requires a `#quill-toolbar` element to register its built-in modules (image handler, link handler, color picker). Rather than fighting Quill's toolbar API, the `#quill-toolbar` is kept hidden and used only as Quill's module container. A fully custom visible toolbar with two panels (Home, Insert) sits above the editor. Custom buttons call `quill.format()` directly; actions needing Quill's internal handlers (link, color) proxy a click to the corresponding hidden button.

This approach allows total UI freedom without forking Quill.

### 5. Optimistic last-write-wins for real-time sync

True OT/CRDT (e.g. Yjs) is engineering-week work. Instead:
- **Presence** via WebSocket: broadcast join/leave events, render colored avatars
- **Content broadcast**: when a user saves, the full Delta is broadcast to connected peers who accept it if they are not actively typing (checked via a `_typing` flag)
- **Auto-save** debounced to 1.8 s ensures frequent server-side snapshots

The WebSocket infrastructure is in place — adding Yjs would be a well-defined upgrade: replace the save-and-broadcast loop with a Yjs awareness channel.

### 6. Email invitation system

Sharing works in two modes:
- **Username share**: target email belongs to an existing user → immediate `DocumentShare` + notification email
- **Email invitation**: target not found → create a `DocumentInvitation` row with a `secrets.token_urlsafe(128)` token + 7-day expiry. Email contains `APP_URL/app?invite=<token>`. On visit, JS calls `POST /api/invitations/accept?token=` before redirecting to the app.

Email sending uses Python's built-in `smtplib`. If SMTP is not configured the app logs the failure and falls back gracefully — the invite link is still shown in the UI so sharing works without an SMTP server.

### 7. Public link sharing

Documents can be shared via a public URL at `/p/<token>`. The token is a `secrets.token_urlsafe(32)` value stored on the `Document` row. `public_permission` is either `"viewer"` or `"editor"`. The `/p/<token>` route serves `public.html` which calls `GET /api/public/{token}` — the only unauthenticated document API endpoint. This pattern is the same as Google Docs "anyone with the link" sharing.

### 8. Access request flow

When a viewer wants to edit a document they do not own, they click "Request Edit Access". A `POST /api/documents/{id}/access-requests` creates a pending `AccessRequest` row and emails the owner. The owner sees pending requests in the Share modal's Requests tab and approves or denies with `PUT /api/access-requests/{id}`. Approval creates a `DocumentShare` with `"editor"` permission and sends a decision email to the requester.

### 9. Per-section sharing via heading detection

Sections are identified by parsing the Quill Delta for `{attributes: {header: N}}` newline operations. Each heading text and its op-index are stored in `SectionShare`. When a user with section-only access loads a document, `section_shares` are returned alongside the full Delta so the frontend can highlight the accessible section.

**Trade-off:** heading op-index is fragile if heavy edits shift positions after the share is granted. A production system would embed stable section UUIDs as custom Quill blots.

### 10. Auto inline grammar check

Grammar checks run on a 4-second debounce after typing stops (independent of the 1.8 s save debounce). Results are applied as background-color formats (`#fff0f0` for spelling, `#f0f6ff` for grammar) via `quill.format()`. Before saving to the server, `_stripGrammarBackground()` removes these ephemeral marks so they are never persisted. The LanguageTool call is proxied server-side to avoid CORS and to enable future rate-limiting.

### 11. Security choices

| Concern | Decision |
|---|---|
| JWT secret | Read from `SECRET_KEY` env var; falls back to `secrets.token_urlsafe(32)` in dev (tokens invalidate on restart — intentional) |
| Token storage | `sessionStorage` — cleared on tab close, never accessible cross-tab |
| Token transport | `Authorization: Bearer` header only — never in URL params |
| Password hashing | bcrypt via passlib, cost factor 12 |
| Auth error messages | Generic "Invalid username or password" — no user enumeration |
| File upload naming | `uuid4().hex + original_ext` — prevents path traversal and overwrites |
| Upload type validation | Extension allow-list + MIME-type check |
| CORS | `allow_origins=["*"]` acceptable for an assessment demo; tighten to the Render domain in production |

---

## What I Would Build Next (2–4 more hours)

1. **Yjs CRDT** — drop into existing WebSocket infrastructure for true collaborative editing
2. **PDF export** — `weasyprint` renders Quill's HTML to a paginated PDF server-side
3. **Server-side section redaction** — filter Delta ops before delivery so inaccessible content never reaches the client
4. **S3/R2 storage** — swap local disk uploads for Cloudflare R2 (free, S3-compatible) for durable attachment storage on Render
5. **Alembic migrations** — replace `migrate.py` with proper versioned migrations for team development
6. **Rate limiting** on grammar-check proxy — leaky-bucket per IP to stay within LanguageTool's free tier
