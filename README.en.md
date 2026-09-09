# Cangzhi (藏知)

[简体中文](README.md) | [English](README.en.md)

> A user-controlled AI knowledge hub · **Keep what you know. Ground every answer.**

Cangzhi turns web pages, files, quick notes, external directories, and structured
data into durable knowledge assets that both people and AI agents can search,
verify, and reuse. It is more than a note-taking app or a RAG chat interface: it
manages the complete knowledge lifecycle, from ingestion and preservation to
understanding, organization, indexing, evidence retrieval, and reuse.

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
  refreshable local dataset snapshots.
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

### Retrieval and grounded answers

- Detect general, legal, contract, paper, meeting, code, and table documents
  deterministically.
- Build structure-first parent and child chunks around headings, sections,
  clauses, paragraphs, and table boundaries.
- Fuse PostgreSQL full-text and pgvector results with Reciprocal Rank Fusion.
- Store tabular datasets as rebuildable Parquet versions and push controlled
  filtering, projection, sorting, grouping, and aggregation into DuckDB. The API
  and MCP endpoint accept a safe query plan, never arbitrary SQL.
- Apply categories, tags, source types, connectors, and saved knowledge scopes
  consistently to both retrieval paths.
- Return matched passages, section paths, and source locations.
- Ask across all knowledge, notes, web pages, files, or saved scopes with
  clickable citations. Question and answer records sync across devices, but
  each question performs an independent retrieval and does not silently use old
  chat history as model memory.

### Models, data ownership, and integrations

- Configure, test, and switch chat and embedding providers independently.
- Build versioned vector indexes in the background, retry failures, activate
  atomically, and roll back in one step.
- Export one knowledge item as Markdown or JSON, or export the complete library
  as a portable ZIP archive.
- Back up PostgreSQL and file storage together, verify checksums, and rehearse
  restoration in isolation.
- Let external agents such as Hermes and OpenClaw retrieve evidence through
  `/api/v1`, `python -m apps.cli`, and `/api/mcp`.
- Give every client a separate, least-privilege, revocable personal access token.

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
