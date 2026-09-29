# Cangzhi (藏知)

[简体中文](README.md) | [English](README.en.md)

> A user-controlled AI knowledge hub · **Keep what you know. Ground every answer.**

Cangzhi turns web pages, files, quick notes, external directories, and structured
data into durable knowledge assets that both people and AI agents can search,
verify, and reuse. It is more than a note-taking app or a RAG chat interface: it
manages the complete knowledge lifecycle, from ingestion and preservation to
understanding, organization, indexing, evidence retrieval, and reuse.

[Quick start](#quick-start) · [First-time workflow](#first-time-workflow) ·
[UI guide](#ui-guide) · [Scope and limits](#current-scope) · [Documentation](#documentation)

## What “AI-native” means in Cangzhi

AI is not just a chat box attached to a conventional knowledge base. It helps
both use and maintain the knowledge stored in Cangzhi:

- **Composable exploration for external agents.** REST APIs, a JSON CLI, MCP,
  and a reusable Skill expose discovery, search, context expansion, document
  reading, controlled dataset analysis, and evidence retrieval as composable
  tools. External agents keep control of their own reasoning and orchestration,
  while Cangzhi provides a reliable path to verifiable source material.
- **AI-assisted organization.** After ingestion, AI can summarize, classify,
  and tag content using the document type, source structure, and the existing
  taxonomy. Users can correct the result without having to define rules for
  every document in advance.
- **Verifiable, controllable, and rebuildable.** Answers resolve to document
  versions, source passages, or original dataset rows. Summaries, categories,
  prompts, chunks, and vector indexes remain replaceable derived data; they do
  not supersede the original source.

## Why Cangzhi

- **Save first, organize later.** Content is preserved before background AI
  processing begins. Failures remain visible and retryable.
- **Files are not yet knowledge.** Cangzhi preserves headings, clauses, pages,
  paragraphs, tables, and version identity before creating rebuildable chunks.
- **Search should not depend on vectors alone.** PostgreSQL full-text search,
  pgvector retrieval, and metadata filters are fused with RRF. Search falls back
  gracefully when the vector provider is unavailable.
- **Answers must be auditable.** Knowledge answers cite concrete document
  versions and source passages. Missing evidence is reported instead of being
  replaced with model knowledge.
- **Knowledge should not be locked into one UI.** The web app, REST API, CLI,
  MCP endpoint, and agent Skill all share the same scopes, retrieval logic, and
  citation model.
- **Delegated exploration needs a hard boundary.** A normal personal access
  token can grant workspace-level MCP access. Short-lived, non-expandable
  exploration grants can restrict an external agent to a selected document set.
- **Long-term knowledge requires data ownership.** Originals can be downloaded,
  knowledge can be exported, and both the database and file storage can be
  backed up and restored independently of any model provider.

See [Product](docs/PRODUCT.md) for the full problem statement and product
boundaries.

## Version and development status

- **The current version tag is `v1.2`**, pointing to `2a9ac16` and synchronized
  to GitHub, Gitee, and Gitea. It includes dataset-aware quick Q&A, AI field
  descriptions, database snapshot freshness, incremental reuse of field
  descriptions, and native account management.
- `v1.0` (`8cccfb9`) remains available. Existing tags are not moved; use the
  documentation at the selected tag when reproducing a release.
- `main` is the ongoing development branch. Cangzhi remains in its personal
  Beta quality-improvement phase: a tag does not imply that complex spreadsheet
  handling, a complete knowledge graph, or all retrieval quality goals are done.

## UI guide

The current interface uses Chinese labels:

| Goal | Where to start | What to expect |
| --- | --- | --- |
| Save and manage material | 知识库 / top-bar add menu | Files, notes, web bookmarks, categories, and tags |
| Check ingestion | 收件箱 / 处理中心 | Pending organization, failed stages, progress, and retry controls |
| Inspect document processing | Document details → 内容与切片 | Original, parsed content, live chunks, and candidate comparison |
| Search or ask questions | 搜资料 / 问知识 | Scope filters, matched passages, citations, and dataset results |
| Maintain database datasets | Field profiles / 设置 → 知识源 | Field descriptions, imported tables, snapshot age, and refresh policies |
| Configure models | 设置 → 对话模型 / 向量与索引 / 图片文字识别 | Connection tests, optional model discovery, index builds, and rollback |
| Connect external AI | 设置 → 外部接入 | PAT scopes and CLI / Skill / MCP connection information |
| Manage spaces and account | 设置 → 工作空间 / 账户与安全 | Knowledge isolation, username/password changes, and login sessions |

Workspaces isolate personal knowledge; they are not team memberships or a
multi-user authorization system. Check the current workspace and knowledge
scope before uploading or asking.

## Available capabilities

### Ingestion and preservation

- Create and edit Markdown notes while retaining meaningful version history.
- Upload PDF, DOC, DOCX, XLSX, XLS, Markdown, and TXT files, and download the
  preserved originals.
- Save public web pages as HTML snapshots with extracted title, author, date,
  and main content.
- Connect read-only WebDAV directories through the same parsing,
  classification, and indexing pipeline.
- Browse PostgreSQL or MySQL schemas in read-only mode and import tables as
  refreshable local dataset snapshots. Choose background refresh when stale
  (default), strict freshness, or manual refresh; answers expose snapshot age.
- Deduplicate source blobs with SHA-256 and expose retryable background jobs.

### Understanding and organization

- Use ten built-in top-level categories, or create and maintain your own.
- Configure OpenAI-compatible or Ollama chat models for summaries,
  classification, and tagging.
- Complete basic ingestion without a model and keep unorganized content in the
  inbox for later processing.
- Automatically revisit pending documents after a working model is configured,
  while allowing users to override the primary category.
- Manage trash, restoration, permanent deletion, and WebDAV source lifecycle in
  one place.
- Optionally enable budgeted, window-based knowledge enhancement with evidence,
  progress, cancellation, and resume. It is off by default and does not replace
  basic summaries, classification, tags, or the retrieval index.
- Explore existing enhancement outputs and hierarchical overviews through
  REST/MCP/CLI/Skill, or browse source headings and blocks without enabling
  enhancement. Reads do not trigger model generation; unresolved entities are
  hints, not a complete knowledge graph. See [Enhancement](docs/KNOWLEDGE_ENHANCEMENT.md).

### Retrieval and grounded answers

- Detect general, legal, contract, paper, meeting, code, and table documents
  deterministically.
- Build structure-first parent and child chunks around headings, sections,
  clauses, paragraphs, and table boundaries.
- Preview live chunks in document details. Optional AI boundary candidates are
  program-validated drafts, not automatic replacements for the live index.
- Fuse PostgreSQL full-text and pgvector results with Reciprocal Rank Fusion.
- Store tabular datasets as rebuildable Parquet versions and push controlled
  filtering, projection, sorting, grouping, and aggregation into DuckDB. The API
  and MCP endpoint accept a safe query plan, never arbitrary SQL.
- Field profiles keep program-computed types, null counts, distinct values,
  ranges, and samples separate from optional AI descriptions, units, aliases,
  and confidence. Database refreshes reuse unchanged field descriptions by
  default and generate only missing or changed ones; full regeneration is optional.
- Excel regions must pass structural validation before entering dataset queries.
  Unverifiable layouts keep originals, coordinates, merged-cell information,
  and diagnostics, but do not become ordinary text chunks or vectors. Common
  header layouts can be supported through validated mappings; complex reports
  require data cleanup, not special-case overfitting.
- Apply categories, tags, source types, connectors, and saved knowledge scopes
  consistently to both retrieval paths.
- Return matched passages, section paths, and source locations.
- Ask across all knowledge, notes, web pages, files, or saved scopes with
  clickable citations. Question and answer records sync across devices, but
  each question performs an independent retrieval and does not silently use old
  chat history as model memory.

### Models, data ownership, and integrations

- Configure, test, and switch chat and embedding providers independently.
- Configure optional external visual OCR separately. Discover models after
  entering a base URL and key, or enter a model name manually. Scanned PDFs use
  local OCR first, with bounded external fallback.
- Build versioned vector indexes in the background, retry failures, activate
  atomically, and roll back in one step.
- Export one knowledge item as Markdown or JSON, or export the complete library
  as a portable ZIP archive.
- Back up PostgreSQL and file storage together, verify checksums, and rehearse
  restoration in isolation.
- Let external agents such as Hermes and OpenClaw retrieve evidence through
  `/api/v1`, `python -m apps.cli`, and `/api/mcp`.
- Give every client a separate, least-privilege, revocable personal access token.
- Use one administrator across multiple workspaces. Change the username and
  password, inspect active sessions, and sign out other devices from the native
  account page. Password changes rotate the current session and revoke other
  browser sessions; PATs must be revoked separately.

## How it works

```text
Links / files / notes / WebDAV / database tables
                         ↓
          Preserve originals and versions
                         ↓
        Parse structure → AI organization
                         ↓
       Full-text + versioned vector indexes
                         ↓
 Browse / search / grounded Q&A / agent evidence
```

AI is an enhancement, not a prerequisite for durable storage. Original content
and structured metadata are the source of truth; summaries, chunks, vectors,
Parquet files, and previews are rebuildable derivatives.

## Current scope

Cangzhi currently targets one user running a personal server. Its goal is to be
the hub between personal knowledge and AI tools, not a general-purpose cloud
drive, bidirectional file-sync system, or team collaboration suite. Multi-user
authorization, email ingestion, browser extensions, deeper OCR, and
domain-specific parsing are tracked in the [roadmap](docs/ROADMAP.md).
Controlled file upload and document Scope Keys are available without turning
Cangzhi into a general document-management or multi-tenant platform.

## Documentation

Most detailed project documentation is currently maintained in Chinese:

- [Contributor guide and repository rules](AGENTS.md)
- [Current project status](docs/PROJECT_STATUS.md)
- [Developer handoff](docs/DEVELOPER_HANDOFF.md)
- [Spreadsheet testing and sample handling](docs/SPREADSHEET_TESTING.md)
- [Development log](docs/DEVLOG.md)
- [Product definition](docs/PRODUCT.md)
- [Architecture and safety boundaries](docs/ARCHITECTURE.md)
- [Deployment and first-time setup](docs/DEPLOYMENT.md)
- [Roadmap](docs/ROADMAP.md)
- [Backlog](docs/BACKLOG.md)
- [Architecture decision index](docs/DECISIONS.md)
- [External agents, CLI, and MCP integration](docs/INTEGRATIONS.md)
- [API, MCP, and CLI reference](docs/API_REFERENCE.md)
- [Backup and restore](docs/BACKUP_AND_RESTORE.md)
- [External uploads and document Scope Keys](docs/EXTERNAL_CLIENT_ACCESS.md)

## Technology stack

- Web: Next.js and TypeScript
- API: FastAPI and Python
- Database: PostgreSQL with pgvector
- Background processing: database-backed job queue and a dedicated worker
- File storage: local filesystem, with a path toward S3/MinIO
- Document parsing: format-specific parsers and LibreOffice conversion
- Retrieval: PostgreSQL full-text search, pgvector, and RRF
- Deployment: Docker Compose

## Quick start

### Requirements

- Docker Engine and Docker Compose v2
- Git

Python and Node.js are not required on the host when using Compose.

```bash
git clone --branch v1.2 https://github.com/percyc/cangzhi.git
cd cangzhi
cp .env.example .env
# Set a secure POSTGRES_PASSWORD in .env before production use.
docker compose up -d --build
make doctor
```

Services:

- Web: <http://localhost:3000>
- API: <http://localhost:8000>
- PostgreSQL 16 with pgvector
- Background worker
- Automatic database migrations

The first visit to the web app opens `/setup`, where you create the sole
administrator account. Later sessions use `/login`. The default upload limit is
50 MB per file and the default web snapshot limit is 5 MB.

Model configuration is optional. After signing in, configure chat and embedding
providers separately under Settings. Settings saved in the UI take effect
immediately and override compatible `.env` values. Provider secrets are stored
encrypted and are never displayed back in plaintext.

For LAN or public deployment, HTTPS, ports, upgrades, backup, and
troubleshooting, see [Deployment](docs/DEPLOYMENT.md).

### First-time workflow

1. Open the web app and create the sole administrator. After login, start in
   the library: upload a small file, write a note, or save a web page. A model
   is not required to preserve originals.
2. For summaries, classification, tags, and answers, open Settings → 对话模型.
   Enter the API URL and key, optionally discover models or enter a model name,
   then save and test. Ollama supports local models.
3. Add semantic search when needed under 向量与索引: test, build, then activate
   the completed index. External visual OCR and knowledge enhancement are optional.
4. Check the inbox, then inspect the original, parsed content, and live chunks.
   Ask within a selected scope and follow citations. Dataset filtering and
   aggregation use controlled exact queries, not model-estimated arithmetic.
5. Add workspaces or knowledge sources as needed. Create a least-privilege PAT
   for external AI under 外部接入. Account maintenance is available from the
   top-right menu → 账户与安全.

**Important distinctions:**

- Saved Q&A is not model memory: each question performs independent retrieval.
- Database sources are versioned local snapshots, not live remote queries on
  every question. The interface shows snapshot time and refresh status.
- AI chunking candidates do not automatically replace chunks, vectors, or citations.
- Complex spreadsheets need data cleanup. Originals and diagnostics are retained;
  the system does not force unreliable structures into exact calculations.

> Before upgrading an existing installation, back up and read the deployment
> guide. Do not overwrite your existing `.env`. `v1.2` is a fixed tag; explicitly
> switch to `main` to follow development. Public deployments require HTTPS.

### Common commands

```bash
make ps               # Show service status
make doctor           # Check containers, migrations, and health endpoints
make upgrade          # Build new code, migrate, and update services
make logs             # Show logs for all services
make logs-api         # Show API logs only
make backup           # Back up PostgreSQL and storage together
make migrate          # Run database migrations
make test             # Run all tests
make test-api         # Run API tests only
make down             # Stop services
make install          # Install local development dependencies
```

`make down-volumes` also deletes database data. Run it only when intentionally
resetting an environment.

### Health checks

- Web: `GET http://localhost:3000/api/health`; with
  `CANGZHI_WEB_BASE_PATH=/_cangzhi`, use
  `GET http://localhost:3000/_cangzhi/api/health`
- API liveness: `GET http://localhost:8000/api/liveness`
- API readiness: `GET http://localhost:8000/api/readiness`

### Optional subpath gateway

The web app is served from `http://localhost:3000/` by default. To mount it
behind an external gateway at a subpath such as
`https://example.com/_cangzhi/`, set:

```dotenv
CANGZHI_WEB_BASE_PATH=/_cangzhi
```

The value is passed to `apps/web/Dockerfile` as a build argument and injected
into Next.js at runtime by `compose.yaml`. Next.js embeds it in static asset URLs
and `basePath`, so changing it requires rebuilding the web image:

```bash
docker compose build web
docker compose up -d web
```

Without this setting, the web app remains mounted at `/`, and its health endpoint
is `/api/health`.

## License

Cangzhi is released under the [MIT License](LICENSE). Modification,
distribution, and commercial use are permitted as long as the original
copyright notice and license text are retained. Third-party dependencies,
models, and imported materials remain subject to their respective licenses.
