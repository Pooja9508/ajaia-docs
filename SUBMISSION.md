# Submission — Ajaia Docs

**Candidate:** Pooja Bylaplar Jayanna  
**Email:** pooja.jayanna095@gmail.com  
**Role:** Technical Program and Project Manager, AI Delivery

---

## Live URL

_(Paste Render URL here after deployment)_

## Test Credentials

| Username | Password | Notes |
|---|---|---|
| alice | password123 | Owner of the seeded welcome document |
| bob | password123 | Demo collaborator |
| carol | password123 | Demo collaborator |

You can also register a new account from the Sign In page → **Create Account** tab.

## Walkthrough Video

See `WALKTHROUGH_VIDEO.txt`

---

## Contents of This Folder

| File / Folder | Description |
|---|---|
| `main.py` | FastAPI application — all 40+ API routes, WebSocket presence, grammar proxy |
| `models.py` | SQLAlchemy ORM models (User, Document, DocumentVersion, DocumentShare, SectionShare, Comment, FileAttachment, DocumentInvitation, AccessRequest) |
| `database.py` | Database engine and session factory (SQLite locally, PostgreSQL on Render) |
| `auth.py` | JWT creation/validation, bcrypt password hashing |
| `email_service.py` | SMTP email sender + HTML templates (share notifications, invitations, access requests) |
| `file_parser.py` | .txt / .md / .docx → Quill Delta conversion |
| `seed.py` | Auto-seeds 3 demo users on first boot |
| `migrate.py` | One-shot schema migration for existing databases |
| `requirements.txt` | Python dependencies |
| `.env.example` | Environment variable template |
| `render.yaml` | Render.com Blueprint for one-click deployment |
| `frontend/index.html` | Login page with Sign In and Create Account tabs |
| `frontend/app.html` | Main editor application shell |
| `frontend/public.html` | Public document viewer (no login required) |
| `frontend/static/css/styles.css` | Full design system — pastel palette, dark mode, responsive layout |
| `frontend/static/js/auth.js` | Auth helpers (sessionStorage token management, API fetch wrapper) |
| `frontend/static/js/app.js` | Sidebar, document list, file upload, timestamp refresh |
| `frontend/static/js/editor.js` | Quill editor, autosave, grammar, sharing, WS presence, find/replace, tables, images |
| `tests/test_documents.py` | pytest integration test suite (18 tests, all passing) |
| `README.md` | Local setup, environment variables, deployment instructions |
| `ARCHITECTURE.md` | Architectural decisions and trade-offs |
| `AI_WORKFLOW.md` | AI tools used, what was changed/rejected, verification approach |
| `SUBMISSION.md` | This file |
| `WALKTHROUGH_VIDEO.txt` | Link to walkthrough video |

---

## What Is Working

### Editor
- Full rich-text editing: bold, italic, underline, strikethrough, H1–H3 headings, ordered/unordered lists, code block, blockquote, links, text color, highlight color, text alignment
- Word-style two-tab toolbar (Home tab and Insert tab)
- Font family and font size selectors
- Subscript and superscript
- Change case: UPPERCASE, lowercase, Title Case, Sentence case
- Find and Replace (Ctrl+H) — prev/next navigation, replace one, replace all
- Table insert via visual 8×8 grid picker
- Image insert from file (JPEG, PNG, GIF, WebP) — uploaded to server and embedded
- Page break and horizontal rule

### Documents
- Create, rename (inline), delete
- Auto-save (1.8 s debounce) with version history — last 10 snapshots, any version restorable
- Version restore rolls back comments to the snapshot's timestamp
- File import: .txt, .md, .docx → creates new document or inserts into existing
- Export to Markdown

### Grammar & Writing Assistance
- Auto inline grammar and spell check via LanguageTool (no API key required)
- 4-second debounce, results shown as colored background highlights in the editor
- Word count goal with live progress bar in status bar
- Warning popup at 90% of goal with "Increase Goal" and "Ignore" actions

### Sharing & Collaboration
- Share by username — viewer or editor permission
- Email invitation sharing — invite anyone by email, token-based acceptance link (7-day expiry)
- Public link sharing — "anyone with this link" as viewer or editor
- Access request flow — viewers can request editor access; owner approves or denies from Share modal; email notifications sent
- Per-section sharing — share only a specific H1/H2/H3 section
- Real-time presence via WebSocket — see who else is currently in the document

### Comments
- Inline comments anchored to selected text ranges
- Resolve and delete
- Comment list in right sidebar

### Auth & Users
- Sign in with username/password
- Create Account — new user registration
- Dark mode (preference stored in localStorage)
- System timezone used for all timestamps
- Usernames capitalized throughout the UI
- Sidebar timestamps refresh every 60 seconds

### Public Viewer
- `/p/<token>` — clean public viewer for documents shared via public link
- No login required
- Shows document title, permission badge, and full formatted content
- Signed-in users can request edit access directly from the public viewer

---

## What Is Incomplete

| Feature | Status |
|---|---|
| Real-time collaborative editing (OT/CRDT) | Presence and content broadcast implemented; full operational transform not. Two simultaneous editors use last-write-wins. |
| PDF export | Not implemented. Would require `weasyprint` or headless browser — deferred in favour of depth elsewhere. |
| Section content redaction | Section-share metadata returned by API; frontend does not yet visually grey out inaccessible sections for shared users. |
| Persistent file storage | Uploaded images live on local disk. On Render free tier the disk is ephemeral — a production deploy would use S3/Cloudflare R2. |

---

## What I Would Build Next (2–4 more hours)

1. **Yjs CRDT integration** — drop Yjs into the existing WebSocket infrastructure for true conflict-free real-time collaborative editing
2. **PDF export** — `weasyprint` renders the Quill HTML to a paginated PDF server-side
3. **Server-side section content redaction** — filter Delta ops before delivery based on `section_shares` so inaccessible content never reaches the client
4. **S3/R2 storage adapter** — swap local disk writes for Cloudflare R2 (free tier, S3-compatible) so uploaded images survive Render redeploys
5. **Rate limiting** on the grammar-check proxy — a leaky-bucket limiter per IP to stay within LanguageTool's free tier under load
