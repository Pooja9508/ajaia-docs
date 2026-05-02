# AI Workflow Note — Ajaia Docs

## Tools Used

| Tool | Role |
|---|---|
| **Claude (claude-sonnet-4-6)** | Primary coding agent — architecture planning, all code generation, debugging, documentation |
| **LanguageTool API** | Runtime grammar/spell check integrated into the product itself |

---

## How AI Was Used in This Project

This project was built in two phases. Phase 1 produced a working collaborative editor. Phase 2 added 20 enhancements including email sharing, public links, access requests, user registration, a Word-style toolbar, find/replace, table and image insert, auto grammar, and dark mode fixes. AI was the primary code generation tool throughout both phases.

### What AI did well

**Full-stack scaffolding in one pass**  
The entire backend structure — FastAPI app, SQLAlchemy models, JWT auth, WebSocket presence manager, file parsers, seed script, and test suite — was generated in a coordinated first pass. Writing this from scratch would have consumed 90+ minutes of boilerplate.

**CSS design system**  
The complete `styles.css` — CSS variables, dark mode overrides, CSS Grid layout, modal animations, toast system, Quill customization, responsive breakpoints, and all new component styles for the two-tab toolbar, find/replace bar, table picker, word limit popup, public viewer, and auth registration tabs — was generated without multiple iteration cycles.

**Quill Delta conversion**  
`file_parser.py` handles `.txt`, `.md` (inline bold/italic via regex), and `.docx` (via python-docx runs) conversion into Quill Delta format, correctly handling nested inline formatting and block-level attributes.

**Test suite fixture design**  
The pytest integration tests use session-level DB setup/teardown with correct `sys.path.insert` placement and `env` var injection before module import — details that are easy to get wrong.

**Two-tab toolbar architecture**  
The `#quill-toolbar` hidden-but-functional pattern (keeping Quill's module container while building a fully custom visible toolbar on top) was a non-trivial design suggestion that solved the conflict between Quill's internal handler requirements and the need for a Word-style UI.

---

## What I Changed or Rejected from AI Output

### Changed: Token storage
Initial AI suggestion used `localStorage`. Changed to `sessionStorage` so tokens expire on tab close — a meaningful security improvement for a shared/demo environment.

### Changed: Grammar check architecture
First iteration called LanguageTool from the frontend (CORS failure). Changed to a server-side proxy endpoint which also enables future rate-limiting and caching without frontend changes.

### Changed: Registration flow session handling
After registration, the AI stored the token in `localStorage` before redirecting to `/app`. The app reads from `sessionStorage`, so the new user was silently redirected back to the login page. Fixed by calling `Auth.login()` after registration so the token lands in `sessionStorage` via the same path as normal login.

### Changed: Missing DB columns
The AI used `Base.metadata.create_all()` which creates missing tables but not missing columns on existing tables. When `public_token` and `public_permission` were added to the model, the existing development database did not get them, causing all document operations to fail at runtime. Fixed by writing `migrate.py` and adding a `create_all()` guard that applies `ALTER TABLE` for new columns. This was caught by testing against the live database rather than a fresh one.

### Changed: WebSocket token validation error codes
AI used arbitrary integers for WebSocket close codes. Changed to standard codes (4001 = unauthorized, 4003 = forbidden, 4004 = not found) that are meaningful to client-side error handling.

### Rejected: React/Vite frontend
AI suggested scaffolding a Vite + React frontend. Kept vanilla JS + CDN libraries to eliminate the build step, stay within the timebox, and make the project reviewable without `npm install`.

### Rejected: JWT payload for UI preferences
AI suggested storing the dark mode preference in the JWT payload. Changed to `localStorage` — appropriate for a UI preference, not auth state.

### Rejected: `localStorage` for dark mode in auth.js  
AI initially put dark mode restore logic inside `auth.js`. Kept it in `app.js` where UI initialization belongs.

---

## How I Verified Correctness, UX Quality, and Reliability

1. **API contract review** — Read every route in `main.py` and cross-checked path names, request bodies, and response shapes against every `fetch()` call in `app.js` and `editor.js`

2. **Auth flow trace** — Traced the register → login → `sessionStorage` → `authHeaders()` → API call chain, caught the `localStorage` vs `sessionStorage` mismatch before it became a bug report

3. **Database schema verification** — Ran `PRAGMA table_info(documents)` against the live `ajaia_docs.db` after adding new model columns; found and fixed the missing `public_token` / `public_permission` columns

4. **Security audit of endpoints** — Confirmed every route that reads or modifies a document calls `_require_doc_access()`, and that share/delete endpoints additionally verify `owner_id == current_user.id`

5. **Quill toolbar wiring audit** — Verified that every `.tb-btn[data-fmt]` in `app.html` has a corresponding handler in `_wireCustomToolbar()` in `editor.js`, and that proxy buttons (color, image) correctly target the hidden `#quill-toolbar` counterparts

6. **Grammar mark persistence check** — Confirmed `_stripGrammarBackground()` is called inside `save()` before the Delta is serialized, so background-color grammar marks are never written to the database

7. **Test execution** — Ran `pytest tests/ -v` after every major backend change; all 18 tests pass throughout

8. **Browser preview** — Opened `index.html`, `app.html`, and `public.html` in the preview panel after each change to verify layout renders as designed

---

## Practical AI Usage Assessment

AI usage here was **functional, not ceremonial**. Value came from eliminating boilerplate and accelerating first-draft quality — not from outsourcing engineering judgment. Every security-sensitive decision (auth model, token storage, access control logic, upload path sanitization) was reviewed and in several cases corrected. The final codebase reflects deliberate human choices with AI as a force multiplier.

The most valuable AI contribution was maintaining coherence across a large, multi-file codebase — keeping API route names, Pydantic model field names, and frontend `fetch()` call paths consistent across `main.py`, `editor.js`, and `app.js` simultaneously, something that's error-prone to coordinate manually.
