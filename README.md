# ArchAI

ArchAI converts an enterprise Java repository into an interactive,
evidence-backed developer workspace. It helps developers and architects
understand a codebase before they modify it, without building or running the
uploaded application.

---

## Part 1: Project overview

### The problem

Enterprise Java systems become difficult to understand as they grow:

- Business logic is spread across controllers, services, handlers, clients,
  repositories, messaging consumers, configuration, and legacy modules.
- New developers spend significant time finding where a request starts, which
  components it crosses, and where it finishes.
- Architects and maintainers need a reliable view of dependencies and change
  impact without manually reading the entire repository.
- General-purpose AI assistants can answer individual questions, but they do
  not provide a reusable, team-wide architecture workspace for every analyzed
  revision.

### What ArchAI provides

ArchAI creates one reusable developer workspace containing:

- A repository overview with the purpose, detected technical stack, important
  backend paths, data surfaces, and major capabilities.
- A connected-components view showing how source components depend on and call
  one another.
- A compact architecture diagram organized into entry-point, application, and
  integration boundaries.
- Selectable execution flows with numbered, evidence-backed handoffs.
- Component-specific execution diagrams for focused investigation.
- Repository- and component-scoped AI chat backed by retrieved source evidence.
- Revision-aware persistence so completed analysis and chat sessions can be
  reused by the team.

### Supported Java systems

Spring is optional. ArchAI can analyze normal Java repositories and detects
supported entry and integration patterns from source evidence, including:

- Java `main` methods
- Spring MVC and REST endpoints
- JAX-RS resources
- Servlet handlers
- Java RMI
- Kafka, RabbitMQ, JMS, and other message listeners
- WebSocket and STOMP handlers
- Schedulers, application events, and worker pools
- Repository, entity, JDBC, JPA, cache, database, and external HTTP boundaries

Maven, Gradle, Ant, and plain Java layouts are supported. Technology detection
comes from build descriptors and configuration evidence rather than printing
every imported package.

### How analysis works

1. ArchAI resolves the repository revision and reuses an existing completed
   workspace when the same revision was already analyzed.
2. The repository is cloned into an isolated workspace directory.
3. Deterministic Java discovery parses source files, build descriptors, entry
   points, component roles, calls, dependencies, configuration, and data
   surfaces.
4. Important execution paths are traced from detected triggers through
   source-resolved calls.
5. A bounded semantic system map is created from major components,
   source-backed relationships, messaging infrastructure, persistence, caches,
   and named external systems.
6. Component maps and chat retrieval indexes are prepared in the background.
7. The resulting workspace is persisted for the repository revision.

ArchAI does not build, test, or execute the repository being analyzed.

### Architecture

| Area | Responsibility |
| --- | --- |
| Frontend | Next.js, React, TypeScript, React Flow, and ELK render the workspace and interactive diagrams. |
| Backend API | FastAPI manages repository analysis, workspace lifecycle, diagrams, component details, and chat sessions. |
| Java analysis | Tree-sitter-based parsing, symbol resolution, framework detection, call analysis, and execution-path discovery create deterministic evidence. |
| Diagram generation | Deterministic Java source analysis produces repository and component maps from discovered symbols, relationships, and framework evidence. |
| AI chat | A locally installed Ollama model answers questions using bounded, redacted repository evidence. |
| Retrieval | Local embeddings with FAISS are used for repositories within the configured limit; large repositories use SQLite FTS/BM25, with keyword retrieval as a fallback. |
| Persistence | Revision-aware JSON, JSONL, FAISS, and SQLite artifacts are stored under `backend/data/workspaces`. |

### Evidence and accuracy

Architecture relationships are attached only when ArchAI has supporting source,
entry-point, build, configuration, or technology evidence. Returned map
references are validated before they are persisted. Unsupported runtime wiring,
reflection, active deployment profiles, and externally generated code are not
invented.

### Workspace storage

Each analyzed revision is stored under:

```text
backend/data/workspaces/<workspace-id>/
├── workspace.json
├── inventory.json
├── orientation.json
├── system-map.json
├── system-map.static.json
├── chunks.jsonl
├── components/
├── chat-index/
├── chat/
└── source/
```

Generated artifacts remain reusable even when the disposable source checkout is
removed. Repository access tokens are used only for the clone request and are
not written to workspace artifacts.

### Project structure

```text
archai/
├── backend/
│   ├── archai/              # API, analyzers, workspace jobs, retrieval, and chat
│   ├── tests/               # Backend regression tests
│   ├── requirements.txt
│   └── .env.example
├── frontend/
│   ├── src/app/             # Next.js application and styles
│   └── package.json
├── scripts/
│   ├── start.sh             # Starts frontend and backend
│   └── shutdown.sh          # Stops both services
└── README.md
```

---

## Part 2: Setup and run

### Prerequisites

Install the following before starting ArchAI:

- Python 3.10 or newer
- Node.js 24 LTS
- npm 11 or newer
- Git
- [Ollama](https://ollama.com/) installed locally
- SSH or HTTPS permission for repositories that ArchAI will analyze

### 1. Clone the ArchAI repository

```bash
git clone <archai-repository-url>
cd archai
```

### 2. Set up the backend

From the project root:

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
cd ..
```

On Windows PowerShell, activate the virtual environment with:

```powershell
backend\.venv\Scripts\Activate.ps1
```

### 3. Configure backend environment variables

Edit `backend/.env`.

Install and start Ollama, then download the default local coding model:

```bash
ollama pull qwen2.5-coder:7b
ollama serve
```

`ollama serve` is not necessary when the Ollama desktop app or service is already
running. ArchAI sends repository evidence only to the configured local endpoint.

Ollama settings for repository chat:

```dotenv
ARCHAI_OLLAMA_BASE_URL=http://127.0.0.1:11434
ARCHAI_OLLAMA_MODEL=qwen2.5-coder:7b
ARCHAI_OLLAMA_TIMEOUT_SECONDS=240
```

Set `ARCHAI_OLLAMA_MODEL` to any model already pulled into Ollama. Larger models
can improve answers but require more local memory.

Useful optional settings are already documented in `backend/.env.example`,
including:

- repository clone timeout
- diagram-generation timeout
- embedding model and device
- large-repository retrieval threshold
- workspace storage directory
- allowed frontend origins

The default embedding policy is local-files-only. When the configured embedding
model is unavailable locally, ArchAI continues with lexical retrieval instead
of downloading the model.

### 4. Set up the frontend

```bash
cd frontend
npm install
cd ..
```

### 5. Start ArchAI

Start the frontend and backend together from the project root:

```bash
./scripts/start.sh
```

The services run in the background:

- Frontend: <http://localhost:3000>
- Backend: <http://127.0.0.1:8000>
- API documentation: <http://127.0.0.1:8000/docs>

Logs are written to:

```text
logs/frontend.log
logs/backend.log
```

### 6. Analyze a repository

1. Open <http://localhost:3000>.
2. Enter an SSH or HTTPS Git repository URL.
3. Add an HTTPS access token only when the repository requires it.
4. Select **Analyze repository**.
5. Wait for repository discovery and system-map generation to complete.
6. Use the overview, connected components, architecture diagram, component
   execution views, and AI chat.

### Stop ArchAI

```bash
./scripts/shutdown.sh
```

### Development mode

Run both services in the foreground with backend reload and visible logs:

```bash
./scripts/start.sh --debug
```

Press `Ctrl+C` to stop both services.

### Custom ports

```bash
./scripts/start.sh --frontend-port 3001 --backend-port 8001
```

The startup script automatically configures the frontend API URL and backend
CORS origins for the selected ports.

### Start services manually

Backend:

```bash
cd backend
source .venv/bin/activate
python -m uvicorn archai.api:app --host 127.0.0.1 --port 8000
```

Frontend, in a separate terminal:

```bash
cd frontend
NEXT_PUBLIC_ARCHAI_API_BASE_URL=http://127.0.0.1:8000 npm run dev
```

### Verify the project

Backend:

```bash
cd backend
source .venv/bin/activate
python -m unittest discover -s tests
```

Frontend:

```bash
cd frontend
npm test
npm run typecheck
npm run lint
npm run build
```

The automated tests use local fixtures and mocked AI boundaries. They do not
contact Ollama or diagram-generation services and do not execute analyzed Java
applications.

### Common setup issues

#### `Backend environment not found`

Create `backend/.venv` and install `backend/requirements.txt` as described
above.

#### `Frontend dependencies not found`

Run:

```bash
cd frontend
npm install
```

#### Local Ollama chat is unavailable

Verify that Ollama is running, that `ARCHAI_OLLAMA_BASE_URL` is reachable, and
that the selected `ARCHAI_OLLAMA_MODEL` has been pulled. Repository analysis and
static workspace evidence remain available when local chat generation is unavailable.

#### A private repository cannot be cloned

Confirm that the SSH agent has access, or provide a valid HTTPS access token for
the clone request.

#### Port 3000 or 8000 is already in use

Stop the existing process or start ArchAI with custom ports:

```bash
./scripts/start.sh --frontend-port 3001 --backend-port 8001
```
