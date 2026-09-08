# 📚 Log Analyzer — Full Revision Guide

A concept-by-concept review of everything used in this project. Each section says
**what it is → where it lives in your code → why it matters**. Work top to bottom;
it follows the flow of a real request.

---

## 0. The big picture

```
  Browser (index.html)
        │  fetch() + JWT
        ▼
  FastAPI  (main.py)  ──► JWT auth ──► endpoints
        │                                  │
        │                                  ├─► Gemini API   (classify + embed)
        ▼                                  ▼
  Database (db.py) ──► PostgreSQL + pgvector
        │                    ├─ logs           (the text)
        │                    └─ log_embeddings (the vectors)
```

One request to `/logs/53/similar` = HTTP → JWT check → SQL vector search → JSON back.

On top of this sits the **LangGraph agent** ([agent.py](agent.py)) — a multi-step pipeline
(fetch → classify → similar-search → human approval → report) exposed via
`/logs/{id}/analyze` and `/analysis/{thread_id}/approve`. Section 12 covers it.

The agent's data access now goes through an **MCP server** ([mcp_server.py](mcp_server.py)):
the agent no longer runs SQL itself — it spawns the server as a subprocess and calls its
tools over JSON-RPC. Sections 22–23 cover the protocol and the hybrid search inside it.

```
  agent.py (MCP HOST) ──spawns──▶ mcp_server.py (MCP SERVER) ──SQL──▶ PostgreSQL
           └── JSON-RPC over stdio: tools/call ──┘
```

The agent's data access no longer touches Postgres directly: it goes through an **MCP
server** ([mcp_server.py](mcp_server.py)) — the agent spawns it as a subprocess and calls
its tools over JSON-RPC. The same server also serves Claude Code and the MCP Inspector.
Section 22 covers it.

Every Gemini *text* call — whether from `main.py` or the agent — now flows through one
tracing wrapper, [llm.py](llm.py), which records tokens, latency, and cost per call.
Section 20 covers it.

---

## 1. FastAPI — the web framework

**What:** an async Python framework that turns functions into HTTP endpoints and
validates data automatically.

**In your code:**
- App is created once: `app = FastAPI(...)` — [main.py:71](main.py#L71)
- Each endpoint is a decorated function: `@app.get("/logs")`, `@app.post("/login")` — [main.py:136](main.py#L136)
- Path params: `/logs/{log_id}` → `async def get_log(log_id: int, ...)` — FastAPI parses & type-checks `log_id` for you — [main.py:141](main.py#L141)
- Query params: `limit: int = 5` becomes `?limit=5` — [main.py:155](main.py#L155)

**Revise these questions:**
- Why `async def`? → so one worker can handle many requests while waiting on I/O (DB, Gemini).
- What makes `log_id` a *path* param vs `limit` a *query* param? → path params appear in the URL path `{...}`; anything else with a default becomes a query param.

---

## 2. Pydantic — data validation

**What:** classes that define the *shape* of request/response JSON. FastAPI uses them
to validate input and auto-generate docs.

**In your code:** [main.py:46-60](main.py#L46-L60)
```python
class LoginRequest(BaseModel):
    username: str
    password: str
```
If a client POSTs `/login` without a `password`, Pydantic rejects it with a 422
*before your function even runs*.

- `Optional[int] = None` means the field can be missing — [main.py:55](main.py#L55)
- `response_model=LoginResponse` filters the output to exactly those fields — [main.py:130](main.py#L130)

---

## 3. Dependency Injection — `Depends`

**What:** FastAPI's way to run a helper *before* your endpoint and pass its result in.

**In your code:** every protected endpoint has `username: str = Depends(verify_token)` — [main.py:137](main.py#L137)

Flow: request comes in → FastAPI runs `verify_token` first → if the token is valid it
returns the username, which is injected as the `username` argument → if invalid it raises
401 and your endpoint never runs. This is how you avoid copy-pasting auth checks into
every function.

---

## 4. JWT authentication

**What:** JSON Web Token — a signed string proving "this user logged in", so the server
doesn't store sessions.

**The two halves in your code:**
- **Issue** on login: `create_access_token` — [main.py:85-87](main.py#L85-L87)
  ```python
  jwt.encode({"sub": username, "exp": expire}, SECRET_KEY, algorithm="HS256")
  ```
  `sub` = subject (who), `exp` = expiry. Signed with `SECRET_KEY`.
- **Verify** on every request: `verify_token` — [main.py:89-94](main.py#L89-L94)
  ```python
  jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
  ```
  If the signature or expiry is wrong → exception → 401.

**Key idea:** the token is *signed, not encrypted*. Anyone can read its contents (paste
it into jwt.io), but nobody can forge one without `SECRET_KEY`. Never put secrets in a JWT.

**The flow you built:**
```
POST /login (user+pass) → get token → send "Authorization: Bearer <token>" on every call
```
See it in the UI: `authHeaders()` and the login flow — [index.html](index.html)

---

## 5. PostgreSQL + psycopg2 — the database layer

**What:** `psycopg2` is the Python driver that talks to PostgreSQL.

**In your code:** [db.py](db.py)
- **Connection:** made once at startup, reused — [db.py:17-23](db.py#L17-L23)
- **`RealDictCursor`:** makes queries return `dict` rows (`row['message']`) instead of
  tuples (`row[3]`) — [db.py:31](db.py#L31). That's why you can write `logs[0]['count']`.
- **Parameterized queries:** `cur.execute(query, params)` with `%s` placeholders — [db.py:32](db.py#L32).
  **Why it matters:** this prevents SQL injection. You never format user input into the
  SQL string yourself.

### 5a. Transactions — the bug you actually hit 🐛
This is the most important DB lesson in the whole project.

- PostgreSQL wraps statements in a **transaction**. If one statement errors, the whole
  transaction enters an **aborted** state, and *every* later query fails with
  `InFailedSqlTransaction` until you `rollback()`.
- Your original `execute` never rolled back on error. At startup, an invalid
  `ALTER TABLE ... ADD CONSTRAINT IF NOT EXISTS` (not valid Postgres!) threw, poisoned the
  shared connection, and then **every** `/logs` request returned 500.
- **The fix** — roll back on any failure so one bad statement can't poison the rest — [db.py:40-44](db.py#L40-L44):
  ```python
  except Exception:
      self.conn.rollback()
      raise
  ```
- `commit()` saves changes; `rollback()` undoes the current transaction. SELECTs don't
  change data but still run inside a transaction.

**Revise:** what's the difference between commit and rollback? Why did a *read* endpoint
(`/logs`) break because of a *write* statement (`ALTER TABLE`)? → shared connection +
aborted transaction.

---

## 6. Embeddings — turning text into vectors 🧠

**What:** an embedding is a list of numbers (a **vector**) that represents the *meaning*
of a piece of text. Similar meanings → nearby vectors.

**In your code:** `get_embedding` calls Gemini — [main.py:100-106](main.py#L100-L106)
```python
response = client.models.embed_content(model=EMBEDDING_MODEL, contents=text)
return response.embeddings[0].values   # → list of 3072 floats
```

- **3072 dimensions:** that's the size of `gemini-embedding-001`'s output. Each dimension
  is one coordinate of the point in 3072-D space. More dims = more nuance, more storage,
  slower search. (Gemini can output smaller sizes via `output_dimensionality`.)
- You embed every log at seed time — [db_seed.py:41-47](db_seed.py#L41-L47) — and store the
  vector alongside the log.

**Mental model:** embeddings map text onto a giant map where "database timeout" and
"connection refused" land near each other, while "payment failed" lands far away —
*even though they share no words*. That's the superpower over keyword search.

---

## 7. pgvector — storing & searching vectors in Postgres

**What:** a Postgres extension that adds a `vector` column type and distance operators.

**In your code:** [db.py](db.py)
- **Enable it:** `CREATE EXTENSION IF NOT EXISTS vector;` — [db.py:48](db.py#L48)
- **The column:** `embedding vector(3072)` — [db.py:66](db.py#L66)
- **The index:** `ivfflat (embedding vector_cosine_ops)` — [db.py:79-83](db.py#L79-L83).
  An index makes nearest-neighbor search fast by not comparing against *every* row.
  `lists = 100` = how many buckets it splits vectors into.

### 7a. The distance operators (memorize these)
| Operator | Meaning        | Used for |
|----------|----------------|----------|
| `<=>`    | cosine distance | your project — direction/meaning |
| `<->`    | L2 / Euclidean  | straight-line distance |
| `<#>`    | inner product   | when vectors are normalized |

---

## 8. Cosine distance & similarity — the math you're learning

**What:** cosine distance measures the *angle* between two vectors, ignoring their length.
Two vectors pointing the same direction = similar meaning.

- **Cosine distance:** `0` = identical direction, `1` = unrelated, `2` = opposite.
- **Cosine similarity:** `1 - distance`. So `1` = identical, `0` = unrelated.

**In your code:** the similarity query — [main.py:162-170](main.py#L162-L170)
```sql
SELECT l.id, 1 - (le.embedding <=> %s) AS similarity   -- distance → similarity
FROM logs l JOIN log_embeddings le ON l.id = le.log_id
WHERE l.id != %s
ORDER BY le.embedding <=> %s                            -- smallest distance first
LIMIT %s
```

**Two subtle but critical points to revise:**
1. **`1 - distance`** converts distance to similarity (they're inverses). Common exam gotcha.
2. **`ORDER BY <=>` (raw distance), not `ORDER BY similarity`.** Sorting by the raw
   operator is what lets the **index** be used. If you sorted by the computed `1 - ...`
   column, Postgres couldn't use the ivfflat index and would fall back to scanning
   everything. Performance detail with big real-world impact.

**Your real result** proved the concept: a *DB-timeout* log matched an *SMTP connection
failure* (0.68) and *auth service unreachable* (0.65) — all "something couldn't connect,"
with almost no shared keywords.

---

## 9. Gemini classification — LLM as a labeler

**What:** using the Gemini chat model to read a log and return structured JSON labels.

**In your code:** `classify_log_with_gemini` — [main.py:108-124](main.py#L108-L124)
- You send a **prompt** that demands JSON-only output — [main.py:109-117](main.py#L109-L117)
- You extract the JSON with a regex and `json.loads` — [main.py:121-122](main.py#L121-L122)

### 9a. The `import json` bug 🐛
`json.loads` was called but `json` was never imported → `NameError` → swallowed by the
bare `except:` → **every** classification silently returned the fallback dict. The one-line
fix (`import json`, [main.py:13](main.py#L13)) made real AI output appear.

**Lesson:** a bare `except:` that returns a fallback will hide real bugs forever. It made a
`NameError` look identical to an API failure.

---

## 10. Configuration & secrets — `.env` + dotenv

**What:** keep secrets (API keys, DB password) out of code, in a `.env` file.

**In your code:**
- `load_dotenv()` reads `.env` into environment variables — [main.py:20](main.py#L20), [db.py:8](db.py#L8)
- `os.getenv("GEMINI_API_KEY")` reads them back — [main.py:35](main.py#L35)
- Defaults for local dev: `os.getenv('DB_HOST', 'localhost')` — [db.py:18](db.py#L18)

**Revise:** why not hardcode the key? → secrets in code leak via git/screenshots; env vars
let the same code run in dev and prod with different values.

---

## 11. The frontend — index.html

**What:** a single static page that talks to your API with `fetch()`.

**Key patterns in [index.html](index.html):**
- Store the JWT in `localStorage` so a refresh keeps you logged in.
- Send it on every call via `Authorization: Bearer <token>` (`authHeaders()`).
- **Same-origin:** the page is served by FastAPI at `/` ([main.py](main.py)), so the
  browser and API share an origin → no CORS problem for the page itself.
- `escapeHtml()` before injecting log text into the DOM → prevents a log message from
  injecting HTML/script (XSS).

---

## 12. LangGraph — the agent layer 🤖

**What:** a framework for building LLM apps as a **graph**: a shared **state** dict flows
through **nodes** (plain functions), and **edges** decide which node runs next. Unlike a
straight-line script, a graph can branch, **loop**, and even **pause mid-run**.

**Your graph** ([agent.py](agent.py) — the ASCII diagram at the top of the file is the map):

```
START ─▶ fetch_log ─▶ classify ⟲ (retry ≤3) ─▶ find_similar ─▶ human_approval ⏸ ─▶ write_report ─▶ END
```

There's also a minimal warm-up graph in [graphDemo.py](../LangGraph/graphDemo.py) — same
ideas (node, conditional edge, loop) with a mock LLM and no side effects.

### 12a. State — the shared notebook
One `TypedDict` that every node reads and writes — [agent.py:48-56](agent.py#L48-L56).
It starts almost empty (`log_id` only) and each node fills in more. Crucially, a node
returns **only the keys it changed** and LangGraph merges the partial update into the
state — [agent.py:64-74](agent.py#L64-L74). That's why `write_report` can see the log,
the classification *and* the similar logs: everyone wrote into the same notebook.

### 12b. Nodes vs routers — workers vs signposts
- A **node** does work and returns a partial state update: `fetch_log`, `classify`,
  `find_similar`, `human_approval`, `write_report`.
- A **router** (used with `add_conditional_edges`) does *no* work — it just returns the
  **name** of the next node as a string — [agent.py:239-262](agent.py#L239-L262).

Wiring is explicit — [agent.py:269-284](agent.py#L269-L284): `add_edge` for "always go
here next", `add_conditional_edges` for "ask this router".

### 12c. The loop — a cycle with an exit condition
`route_after_classify` can return `"classify"` — the node it just came from. That's a
**cycle** — [agent.py:249-252](agent.py#L249-L252). Two things make it useful instead of
infinite:
1. **An exit condition in state:** the `attempts` counter — the router only loops while
   `attempts < MAX_ATTEMPTS`.
2. **Each pass is different:** `build_classify_prompt` escalates — attempt 1 = message
   only, attempt 2 = + metadata & keyword hints, attempt 3 = + few-shot examples —
   [agent.py:87-128](agent.py#L87-L128). Retrying the *same* prompt would mostly get the
   same answer; retrying a *better* prompt is what makes the loop worth having.

Note the defensive detail: malformed/empty LLM output is normalized to `"unknown"` so it
goes through the same retry path — [agent.py:149-152](agent.py#L149-L152).

> **⚠ Superseded by §25:** this loop is no longer a graph *cycle*. When `classify`
> became the self-contained `classify_agent` sub-agent, the `while attempt <
> MAX_ATTEMPTS` loop moved *inside* the function and `route_after_classify` lost its
> `return "classify"` branch. The **lesson is unchanged** — an exit condition
> (`attempts`) plus an *escalating* prompt is what makes a retry worth having — only its
> *location* moved (graph edge → function body). Read this section for the principle, §25
> for where it lives now.

### 12d. `interrupt()` — human-in-the-loop ⏸
`human_approval` calls `interrupt(payload)` — [agent.py:198-203](agent.py#L198-L203).
This **pauses the entire graph**: the payload is returned to the caller (under the
`__interrupt__` key), and the graph sits frozen until someone resumes it with
`Command(resume=value)` — at which point the node re-runs from the top and `interrupt()`
*returns that value* instead of pausing — [agent.py:327-334](agent.py#L327-L334).

**Key mental model:** a paused graph is **not a sleeping process**. It's just rows in the
checkpointer. Nothing is running while it waits.

### 12e. Checkpointer + threads — where the pause lives
`interrupt()` only works because of the **checkpointer**: it snapshots the state after
every node — [agent.py:294-297](agent.py#L294-L297).

- `MemorySaver` = snapshots in RAM → a paused run **dies with the process**.
- `SqliteSaver("checkpoints.db")` = snapshots on disk → a paused run **survives restarts**.
- *(Since the MCP refactor it's `AsyncSqliteSaver` — same idea on the async path; see 22e
  for why the swap was forced and the event-loop gotcha it uncovered.)*
  (Since the MCP refactor it's the async twin, `AsyncSqliteSaver` — same idea, same file;
  why it had to change is a section 22e gotcha.)
- [kill_test.py](kill_test.py) proves it with two separate processes: `start` runs to the
  interrupt and exits; `resume` is a brand-new process that successfully continues the run.

Every run belongs to a **thread**: `{"configurable": {"thread_id": ...}}` is the key the
checkpointer files snapshots under, and it's how a resume finds *the exact paused run* —
[agent.py:308-334](agent.py#L308-L334).

### 12f. Exposed through FastAPI
- `POST /logs/{id}/analyze` → mints a fresh `thread_id` (uuid), runs until the interrupt,
  returns the approval question + `thread_id` — [main.py:177-188](main.py#L177-L188)
- `POST /analysis/{thread_id}/approve` → resumes that thread with the human's decision —
  [main.py:196-203](main.py#L196-L203)

The `thread_id` is effectively a **claim ticket**: the client must hand it back to approve.

### 12g. A naming gotcha you hit 🐛
The stale `__pycache__` in the LangGraph folder shows the demo file was once named
`langgraph.py`. A file named after a package **shadows** the installed package — `from
langgraph.graph import ...` finds *your* file instead and the import breaks. That's why
it's `graphDemo.py`. Never name a script after a library you import.

---

## 13. Docker — the same code, containerized 🐳

**The rule that drove every code change:** an image should be **generic** — everything
environment-specific enters via env vars at runtime. That's why the model names, port,
and checkpoint path moved from hardcoded strings to `os.getenv(...)`:
`CLASSIFICATION_MODEL` / `EMBEDDING_MODEL` ([main.py:39-40](main.py#L39-L40),
[agent.py:39](agent.py#L39)), `PORT`, and `CHECKPOINT_DB` ([agent.py:298](agent.py#L298)).

**The three files:**
- [Dockerfile](Dockerfile) — python:3.11-slim → install requirements → copy code → uvicorn.
- [.dockerignore](.dockerignore) — keeps `.env` (your API key!) and `checkpoints.db` out of
  the image. `COPY . .` copies everything not ignored, and a secret baked into an image
  leaks to anyone who can pull it. Never put a real key in compose YAML either — use
  `env_file: .env`.
- [docker-compose.yml](docker-compose.yml) — two services on a private network:
  `db` (`pgvector/pgvector:pg16`) and `app` (built from the Dockerfile).

### 13a. The gotchas (each one bites for real)
1. **`localhost` inside a container means *that container*.** Services reach each other by
   **service name**: the app needs `DB_HOST: db`, never `localhost`.
2. **The two services must agree on credentials.** `POSTGRES_DB/USER/PASSWORD` on `db`
   must match `DB_NAME/USER/PASSWORD` on `app` — and Postgres only applies `POSTGRES_*`
   on **first init of an empty volume**. Changing them later does nothing until
   `docker compose down -v` wipes `pgdata`.
3. **Don't publish `5432:5432` if a native Postgres runs on the host** — port collision.
   Containers talk over the internal network; only map a port (e.g. `5433:5432`) if you
   want host tools like psql/pgAdmin looking in.
4. **Bare `depends_on` waits for "started", not "ready".** [db.py](db.py) connects at
   import and raises, so the app crash-loops while Postgres initializes. Fix: a
   `pg_isready` healthcheck on `db` + `condition: service_healthy`.
5. **`CHECKPOINT_DB` must point at a mounted volume** (`/data/checkpoints.db`). A relative
   path lands in the container's writable layer, which is deleted with the container —
   paused LangGraph runs would die on `docker compose down`. This is the containerized
   version of the MemorySaver → SqliteSaver lesson (12e).

### 13b. Three env layers in compose (easy to confuse)
- `${DB_PASSWORD:-default}` in the YAML = **compose-time interpolation** — compose itself
  reads `.env` when you run `docker compose up`.
- `env_file: .env` = inject the file's vars **into the container** at runtime.
- `environment:` = per-service overrides that **win over** `env_file` — where the
  Docker-specific values live (`DB_HOST: db`, `CHECKPOINT_DB: /data/checkpoints.db`).

Same `.env` file, three different consumption points.

### 13c. Run it
```bash
docker compose up -d --build
docker compose exec app python db_seed.py   # fresh pgdata volume = empty DB, seed once
# then open http://localhost:8000
```

**Kill test, containerized:** click 🧠 Analyze in the UI → `docker compose restart app` →
click Approve. It still works, because the paused graph lives on the `checkpoints` volume,
not in the container.

---

## 14. Running it — the commands

```bash
# 1. Start Postgres with pgvector (Docker) — must be running first
# 2. Seed the DB (creates tables, inserts 13 logs + their embeddings)
python db_seed.py
# 3. Run the API + UI
python -m uvicorn main:app --host 127.0.0.1 --port 8000
# 4. Open the UI
#    http://127.0.0.1:8000/
# 5. Quick sanity check without the browser
python test_all.py

# 6. Run the LangGraph agent from the CLI (pauses and asks you in the terminal)
#    (spawns mcp_server.py as a subprocess — see section 22)
python agent.py 56
# 7. Prove the durable checkpointer: pause in one process, resume in another
python kill_test.py start        # ⚠ still uses the old sync API — see 22f
python kill_test.py resume
# 8. Poke the MCP server by hand in a browser UI (you play the host)
npx @modelcontextprotocol/inspector python mcp_server.py
```
On Windows, if emoji prints crash with a `charmap` error, prefix with
`PYTHONIOENCODING=utf-8`.

Handy curl calls:
```bash
curl http://localhost:8000/health                                  # no auth
curl -X POST http://localhost:8000/login \
  -H "Content-Type: application/json" \
  -d '{"username": "admin", "password": "password123"}'            # → token
curl http://localhost:8000/logs \
  -H "Authorization: Bearer YOUR_TOKEN_HERE"                       # authed call
```

---

## 15. Self-test — can you answer these?

1. What does an embedding represent, and why does semantic search beat keyword search?
2. Cosine distance of 0 means what? Similarity of 1 means what? How do you convert between them?
3. Why does the query `ORDER BY <=>` instead of `ORDER BY similarity`?
4. What is a JWT, what's inside it, and what stops someone forging one?
5. Why did a bad `ALTER TABLE` at startup break the unrelated `/logs` endpoint?
6. What does `Depends(verify_token)` do and when does it run?
7. Why are `%s` parameterized queries safer than string formatting?
8. What's the risk of a bare `except:` — which two bugs did it hide in this project?
9. In LangGraph, what's the difference between a node and a router?
10. What stops the classify loop from running forever, and why is each retry *better* rather than just "again"?
11. What actually happens when `interrupt()` runs — where does the paused graph "live", and what does `Command(resume=X)` do?
12. Why did switching `MemorySaver` → `SqliteSaver` make `kill_test.py resume` work from a different process? What role does `thread_id` play?
13. Why must the app use `DB_HOST: db` instead of `localhost` inside Docker Compose?
14. Why does `.dockerignore` exclude `.env`, and what goes wrong without it?
15. In Docker, why must `CHECKPOINT_DB` point at a volume — and which earlier lesson is that the containerized version of?

If you can answer all 15, you've got the project cold.

---

## 16. Known rough edges (intentionally not "fixed" — this is a sandbox)
- Bare `except:` blocks still in [main.py:93](main.py#L93) and [main.py:123](main.py#L123).
- Secrets have insecure defaults (`password123`, `your-secret-key-change-me`).
- `ivfflat` on 3072 dims can hit pgvector's 2000-dim index limit on some versions — if a
  future `CREATE INDEX` errors, that's why (drop the index or reduce dimensions).

None of these block learning; they're the gap between a sandbox and production.

---

## 17. GitHub Actions — CI on every push 🔁

**What:** GitHub's built-in automation runner. You describe, in YAML, "when X happens, run
these steps," and GitHub spins up a **throwaway machine** (a *runner*) to execute them —
nothing runs on your own PC.

**In your code:** [.github/workflows/ci-cd.yml](.github/workflows/ci-cd.yml)

```
push to main ─▶ checkout ─▶ docker build ─▶ start stack (real secrets)
             ─▶ create extension + tables ─▶ seed data ─▶ run test_all.py ─▶ tear down
```

- **Trigger:** `on: push: branches: [main]` — every push to `main` fires this.
- **Job / steps:** one job (`test`) runs top to bottom on `ubuntu-latest`, a fresh Ubuntu
  VM with nothing on it — `actions/checkout@v4` is the first step *because the runner
  starts with no code at all*.
- **`run:` vs `uses:`** — `uses:` calls someone else's pre-built action (checkout);
  `run:` executes a raw shell command, same as your own terminal.

### 17a. GitHub Secrets — the CI equivalent of `.env`
A runner is a public, throwaway machine — your real `.env` never leaves your PC and was
never committed. **Settings → Secrets and variables → Actions** is an encrypted vault tied
to the repo: paste a value in once via the web UI, reference it in the workflow as
`${{ secrets.DB_NAME }}`, and it's injected at run time without ever touching your code,
logs, or git history.

Same three-layer idea as section 13b, one more layer added:
- **Locally:** Compose auto-reads `.env` for `${...}` substitution.
- **In CI:** the workflow's `env:` block sets those as real shell vars before
  `docker compose up` runs — `${{ secrets.X }}` (workflow → GitHub's vault) becomes
  `${X}` (shell → compose file), same substitution mechanism, different upstream source.
- The compose file itself never changes and never "knows" which environment it's in —
  that's what makes it environment-agnostic by construction.

### 17b. The gotchas you actually hit 🐛
1. **`env_file: .env` breaks in CI.** Locally the file exists; on the runner it never
   does (correctly gitignored). Compose errors immediately: `env file ... not found`.
   **Fix:** drop `env_file:` entirely, reference every value as `${VAR}` inside
   `environment:` instead — Compose fills it from `.env` locally and from the workflow's
   `env:` in CI, same file, two sources.
2. **A fresh Postgres container is empty — every single run, not just the first.**
   Locally you ran `CREATE EXTENSION` and `db_seed.py` once, by hand. In CI there is no
   "once" — the DB is destroyed and rebuilt on every push, so schema creation and seeding
   have to be explicit workflow steps, not a memory of something you did manually.
3. **Step order matters, and it's easy to get backwards.** Schema-setup and seed steps
   must come *after* `docker compose up -d` — there's no running container to `exec`
   into before the stack has started. An early version of this workflow had it reversed.
4. **YAML is whitespace-sensitive.** A single step indented one space off from its
   siblings fails to parse — the whole workflow won't run, not just that step. Worth a
   slow, deliberate look whenever a step is added or reordered by hand.

### 17c. `docker compose exec` vs `docker compose up` — env var scope
`up` starts containers with a given environment baked in *at start time*. A later
`exec` into an already-running container inherits *that* container's environment — but
Compose still re-resolves `${...}` in the compose file itself for the `exec` command's
own bookkeeping, which can throw "variable not set" warnings even though the actual
container is fine. **Lesson:** distinguish "the container's real env" (set once, at
`up`) from "Compose re-parsing the YAML for this specific command" (happens every time,
can warn even when nothing is actually broken).

### 17d. CD — deliberately deferred, not missing
The workflow has a second, commented-out job:
```yaml
# deploy:
#   needs: test
#   ...
#   uses: appleboy/ssh-action@v1
```
This is **CI without CD**, on purpose — `deploy` needs a real VPS to SSH into, and there
isn't one yet (see section 18). Uncommenting it and adding four secrets
(`VPS_HOST`, `VPS_USER`, `VPS_SSH_KEY`, and a `git pull && docker compose up --build -d`
script) is the entire remaining step once a droplet exists — nothing else about this
workflow needs to change.

**Revise these questions:**
- Why does a runner need `actions/checkout@v4` as its very first step?
- What's the actual difference between `${{ secrets.X }}` and `${X}` in a compose file —
  which one is GitHub's syntax and which is Compose's?
- Why did `env_file: .env` work locally but fail in CI, when `environment:` with `${VAR}`
  works in both places?
- Why must schema/seed steps run on *every* CI push, when locally you only ran them once?

---

## 18. What's still open — the deploy target

**Deliberately parked, not forgotten:**
- **VPS** — no droplet yet. Oracle Cloud's Always Free tier is the planned path (a real,
  permanent, no-cost VPS) — parked separately because its signup flow (card verification,
  occasional regional capacity limits) deserves its own session rather than tacking onto
  a debugging session.
- **Nginx + HTTPS** — a reverse proxy in front of FastAPI, cert via Certbot. Not required
  for the app to *work* (the port could be exposed directly), but standard for a portfolio
  link with a real domain instead of `http://<ip>:8000`.
- **The `deploy` job** — see 17d, written and ready, just commented out.

**Considered and correctly skipped:** ngrok, as a way to get a real public URL without a
VPS. Understood the mechanism (a tunnel from a public relay to `localhost`, not real
hosting) but judged not worth actually running — it proves nothing durable and the
concept alone was the point.

None of this blocks calling Weeks 1–7 conceptually complete: CI is real and green, the
agent is real and tested, containerization is real and working. What's left is entirely
"put it on a machine the public can reach," not "build anything new."

---

## 19. VPS / Nginx / HTTPS — concepts primer (not yet built)

Covered as concepts, not yet implemented — this section is here so the next session
starts with the vocabulary already in place, same as sections 6, 12, and 13 were written
*before* the corresponding code existed.

### 19a. VPS — Virtual Private Server
**What:** a rented computer in a data center. Mechanically no different from your own
PC — same OS, same terminal, same Docker commands — except it has a public IP address,
runs 24/7, and you interact with it entirely through SSH instead of a screen and keyboard.

**Why one is needed at all:** everything built so far (`docker compose up`) only runs
while *your* machine is on and connected. A VPS is just "a machine that's always on and
reachable," so the same `docker compose up -d` that already works locally works there
too — nothing about the app changes, only *where* it runs.

**Planned path:** Oracle Cloud's Always Free tier — a genuine, permanent, no-cost VPS
(not a trial). Parked deliberately: the signup flow (card verification even for the free
tier, occasional regional capacity limits) is real friction that deserves its own
session rather than riding along on a debugging session.

### 19b. Nginx — reverse proxy
**What:** software that sits in front of the app and forwards incoming web traffic to it,
rather than exposing the app's port directly to the internet.

**Why it's needed, specifically:**
- Lets the app be reached on the standard web ports (80/443) with a real domain, instead
  of `http://<ip>:8000`.
- It's what actually **terminates the SSL certificate** — HTTPS ends at Nginx, which then
  talks to the app over plain HTTP internally. The app itself never needs to know about
  certificates.
- Not strictly required for the app to *function* — port 8000 exposed directly on the
  VPS's IP would technically work — but it's the standard, correct way to do it, and
  skipping it means no real domain and no HTTPS.

### 19c. HTTPS — the certificate half
**What:** the padlock — traffic between the browser and Nginx is encrypted, and the
certificate proves the server is who it claims to be.

**How it gets set up:** Certbot, a free tool from Let's Encrypt, automatically obtains and
renews a certificate for a domain pointed at the VPS's IP, and configures Nginx to use it.
Requires a domain name pointed at the VPS first — an IP address alone can't get a
certificate issued against it.

### 19d. How this plugs into what already exists
Nothing already built changes:
- `Dockerfile` / `docker-compose.yml` — unchanged, run exactly as they do locally.
- The commented-out `deploy` job (17d) — this is the piece that becomes active: SSH into
  the VPS, `git pull`, `docker compose up --build -d`.
- Nginx is a *new* piece sitting in front of the existing `app` service, not a
  replacement for anything — traffic flow becomes:
  ```
  Browser → HTTPS (443) → Nginx (VPS) → app container (8000, internal)
  ```

**Revise these questions:**
- What does a VPS give you that your own laptop running Docker doesn't?
- Why can't the app "just" have HTTPS on its own — why does that responsibility sit with
  Nginx instead?
- Why does getting a certificate require a domain name, not just the VPS's IP address?
- Once the VPS exists, which files change — and which don't?

---

## 20. LLMOps — tracing, cost & the observability dashboard 📊

**What:** LLMOps = the operational discipline around LLM apps. An LLM app's behavior is
defined by prompts, model versions, and nondeterministic outputs — not just code — so
you need visibility into every call: what was asked, what came back, what it cost,
how long it took, whether it failed. This section is the first pillar (**observability**);
evals and prompt versioning are the next ones, not built yet.

### 20a. The choke-point tracer — [llm.py](llm.py)
**The rule:** business logic never calls `client.models.generate_content()` directly.
Everything goes through one wrapper, `tracked_generate` — [llm.py:38](llm.py#L38) — which
appends one JSON line per call to `llm_calls.jsonl` (gitignored: traces contain full
prompts, i.e. your data).

Design decisions worth being able to defend:
- **One choke-point.** Instrumentation scattered across five call sites gets forgotten in
  three. Centralized = impossible to skip. `main.py` and `agent.py` both import from here,
  and both share the single `genai.Client` it creates.
- **`purpose` tags name the code path, not the function.** `"classify"` (agent, escalating
  prompts) vs `"classify_endpoint"` (direct endpoint, minimal prompt) vs `"write_report"`.
  Tags are what let you ask "where does my money go?" and "which path is more accurate
  per dollar?" later.
- **Trace in `finally`, then re-raise** — [llm.py:63-68](llm.py#L63-L68). The tracer
  *observes* failures; the caller still owns error handling. A tracer that swallows
  errors silently changes app behavior — the cardinal sin of instrumentation. (Contrast
  with the bare-`except:` lesson in 9a: same theme, opposite role.)
- **JSONL** (one JSON object per line): append-only, crash-safe, greppable, loads
  straight into pandas. A half-written line from a crash corrupts one line, not the file.
- This is a miniature **Langfuse/LangSmith**: trace collection + trace API + trace UI.
  Adopting the real tool later, every screen will look familiar. (It did — section 21.)

### 20b. Token usage — `usage_metadata` and the thinking-token gotcha 🐛
Every Gemini response carries token counts for free — [llm.py:52-61](llm.py#L52-L61):
`prompt_token_count` (input), `candidates_token_count` (output), `total_token_count`.

**The gotcha:** `gemini-3.5-flash` is a *thinking* model. Its reasoning tokens are billed
at the **output** rate but are **not** inside `candidates_token_count` — they sit in a
separate `thoughts_token_count` field. Miss it and every cost figure silently
undercounts. If traces show `thinking_tokens` dwarfing `output_tokens` on a simple task,
that's the signal to set a `thinking_budget` and pocket the difference — a standard
LLMOps cost lever.

### 20c. Cost — computed per call, snapshotted at call time
Prices live in `.env` (`PRICE_IN_PER_1M=1.50`, `PRICE_OUT_PER_1M=9.00` — checked
Aug 2026), and the wrapper computes — [llm.py:70-75](llm.py#L70-L75):
```
cost = prompt_tokens/1M × $1.50  +  (output_tokens + thinking_tokens)/1M × $9.00
```
Three ideas to retain:
1. **Output costs 6× input.** `"Return ONLY JSON"` and `"max 5 sentences"` in the prompts
   are cost controls, not style. People obsess over trimming input context; a chatty
   output format often costs more.
2. **Cost is baked into the record when the call happens** — deliberately. If prices
   change next month, history still shows what you actually paid. (Recomputing at read
   time from current prices would rewrite history.) Old rows from before prices were
   configured show $0 forever — correct, not a bug.
3. **Prices are config, not an API lookup** — a price change should be a visible,
   reviewed change, because it shifts every metric downstream.

### 20d. The dashboard — `/llm-calls` + the 📊 LLM Traces card
- **Endpoint:** `GET /llm-calls` (JWT-protected) replays the JSONL newest-first —
  [main.py:210-229](main.py#L210-L229). Each line is parsed in its *own* try/except, so
  one torn line can't 500 the whole page — this resilience is *why* JSONL was chosen.
- **UI** ([index.html](index.html), `loadTraces()`): five stat tiles (calls, tokens,
  cost, avg latency, errors — the errors tile only goes red when nonzero) over a table
  of every call; **click a row to see the exact prompt and response**. That drill-down is
  the single most useful feature of any tracing tool: when a classification looks wrong,
  look at what the model was actually asked.
- **Split:** the server serves raw records; the client aggregates. Simplest correct
  answer at sandbox scale; at production scale you'd aggregate in the DB and page the
  records — same interface shape.
- Refreshes automatically after every Classify/Analyze, so agent retries appear live as
  separate rows (`#56 · try 1`, `try 2`…) with visibly growing token counts.

### 20e. Still open in this area
- **Embedding calls are untraced** (`embed_content` in `get_embedding`). Cheap per call
  but they run on every ingested log — at volume, the bigger line-item.
- The `classify_endpoint` fallback still returns a made-up classification on failure
  (see 9a / section 16) — the trace now *shows* the error while the API returns 200
  with fake data. Observability exposes the anti-pattern; fixing it is a later step.
- Next lesson: **evals** — harvest traced prompt/response pairs into a golden dataset
  and rerun it on every prompt change, so "did my tweak help?" gets a measured answer.

**Revise these questions:**
- Why must every LLM call go through one wrapper instead of calling the SDK directly?
- Why does the tracer write its record in `finally` and re-raise, instead of catching
  the error? Which earlier bug (9a) is the mirror image of this rule?
- Which tokens does `candidates_token_count` *not* include, and why does that matter
  for billing?
- Why is cost stored in the trace record instead of recomputed from current prices?
- Why did output-token price (6× input) already shape prompts written weeks earlier?
- Why does `/llm-calls` parse each JSONL line in its own try/except?

---

## 21. Langfuse — adopting the real tracing tool 🔭

**What:** section 20 built a miniature Langfuse by hand; this section plugs in the real
one (cloud free tier, Python SDK v4) *alongside* the JSONL tracer — both record every
call, so each can audit the other. The hand-rolled version wasn't wasted work: it's the
reason every Langfuse screen was instantly familiar, and it caught a real discrepancy
during rollout (21f).

### 21a. The vocabulary — trace / span / generation / session
- **Trace** = one end-to-end unit of work ("analyze log #56"). One trace per request.
- **Span** = one timed step inside a trace (DB fetch, pgvector search). Spans nest.
- **Generation** = a specialized span for a single **LLM call** — extra fields a plain
  span doesn't have: model, prompt/completion, token usage, cost. NOT "an agent call":
  the agent run is the *trace*, containing many generations.
- **Session** = a string ID grouping *multiple traces* into one workflow (see 21d).
- Rule of thumb: **span answers "where did the time go?", generation answers "where did
  the money go?"** — and the trace is what you replay when something misclassifies.
- The JSONL mapping: one JSONL line ≈ one generation; `purpose` → observation name;
  `**meta` → metadata; `thinking_tokens` → the `reasoning` usage bucket. The thing JSONL
  *can't* express is the tree — it's flat, so multi-attempt runs need a pandas join on
  `log_id`; in Langfuse they're one trace.

### 21b. The SDK is a buffered pipe, not a logger
The JSONL tracer blocks the request for a synchronous file append. The SDK appends to an
**in-memory queue**; a background thread ships batches over HTTPS. Three consequences:
- **~Zero latency** added to the request path — why vendors can claim "no overhead".
- **Lost tail on exit:** unsent events die with the process. Hence `langfuse.shutdown()`
  in main.py's FastAPI shutdown hook AND at the end of agent.py's CLI `__main__` —
  the explicit drain is the crash-safety the JSONL append gets for free.
- **Failure isolation, both directions:** 20a's rule was "the tracer must observe
  failures, not swallow them". The mirror rule: **the app must never fail because the
  tracer did** — Langfuse down/unreachable ⇒ SDK logs a warning and drops events, app
  never notices. Observability is a passenger, never a driver.

### 21c. Context propagation — how the tree builds itself
No trace IDs are passed anywhere. OpenTelemetry (Langfuse v4 is built on it) keeps a
**contextvar** holding "the currently open span":
- `@observe` on `start_analysis` / `resume_analysis` — [agent.py](agent.py) — creates a
  span on entry, sets it *current*, restores on exit (a stack).
- `start_observation(as_type="generation")` inside `tracked_generate` — [llm.py](llm.py)
  — parents onto *whatever is current right now*.
So the tree assembles from the call stack. If **nothing** is current (the bare
`/classify` endpoint before it was decorated), the SDK wraps each generation in its own
auto-created one-node trace — "flat mode", which is just the JSONL structure with a
prettier viewer. Corollary: anything that breaks the call stack (thread pools, task
queues) silently fragments traces — fine here, classic production trap.

**Instrumentation follows code paths, not intentions:** decorating
`classify_log_with_gemini` (main.py) does nothing for `/analyze` — that flow uses the
agent's own `classify` node. The `purpose` tags from 20a already encoded this split.

**Deliberately NOT decorated:** `human_approval`. `interrupt()` works by *raising* — a
span there would log a spurious ERROR on every legitimate pause.

### 21d. Sessions — the observability twin of the checkpointer
The approval interrupt splits one analysis across two HTTP requests, and a trace
mechanically *cannot* span them (the first request's call stack unwinds; spans ship when
they end). So: two traces, grouped by a **session** — set via
`propagate_attributes(session_id=thread_id)` around `graph.invoke`.
The key insight: LangGraph's `thread_id` already answers "how do I resume *state* across
requests?"; the session answers "how do I group *traces* across requests?" — same
question, same key. **Trace = one request. Session = one workflow/conversation.**

### 21e. Manual instrumentation vs the LangChain callback handler
Langfuse ships a `CallbackHandler` (`config={"callbacks": [handler]}` on `invoke`) that
auto-traces every node and edge — but it only sees what flows through LangChain
abstractions, and our LLM calls use the **raw google-genai SDK**. The handler would have
produced node spans with *no generations, no tokens, no cost*. Manual won because:
- the choke point (20a) already guarantees 100% coverage in ~15 lines;
- fidelity: `purpose` names, `attempt` metadata, `reasoning` bucket, `.env`-price
  `cost_details` — exactly matching the JSONL is what made line-for-line verification
  (and 21f) possible.
Rule of thumb: **auto-instrumentation buys coverage, manual buys fidelity.** We had
coverage; manual bought fidelity for free.

### 21f. Gotchas actually hit 🐛
- **v4 ≠ v3 API.** Docs/tutorials mostly describe v3 (`start_generation`,
  `update_current_trace`). Installed v4.14.4 renamed/replaced:
  `start_observation(as_type="generation")`, and `propagate_attributes(...)` for
  session/user/tags. Verified against the installed package with `inspect.signature`
  before writing code — cheaper than debugging a TypeError.
- **Env-var name trap:** the JS SDK uses `LANGFUSE_BASE_URL`, Python historically
  `LANGFUSE_HOST`. v4 Python accepts both — but only by luck did `.env` work.
- **Eventual consistency masquerading as data loss.** After the first CLI run,
  `resume_analysis`/`write_report` were missing from the UI → diagnosed as
  in-memory-queue loss on process exit (21b). Wrong: they appeared minutes later with
  the *original* timestamps — cloud **ingestion lag** (events → queue → ClickHouse; the
  read path trails the write path). The JSONL was the arbiter proving the call happened.
  Lesson: **absence on a dashboard at read time is not evidence of absence at write
  time** — and an independent synchronous record is what lets you tell lag from loss.
- **The error path verified itself:** a real Gemini 503 on `write_report` flowed exactly
  per the 20a rubric — `finally` traced it (latency measured, $0, error string), the
  exception re-raised, the node's own try/except degraded gracefully (graph completed
  with a failure-note report), and Langfuse shows the generation at level ERROR with the
  503 as status message.
- **Cost parity held** line-for-line between JSONL and Langfuse ($0.006534 classify /
  $0.007933 write_report) because `cost_details` keeps *our* `.env` prices
  authoritative — 20c's snapshot-at-call-time philosophy carried over unchanged.
  Graduation path: register prices in Langfuse's model table, drop `cost_details` and
  the `.env` prices.

### 21g. Still open in this area
- **Retryable vs fatal errors:** a transient 503 on `write_report` currently costs the
  whole approved analysis — no path to retry just the report. Backoff/retry policy for
  5xx is the "resilience" lesson, deliberately not built yet.
- Embedding calls remain untraced in *both* systems (same 20e item).
- Migrate cost computation to Langfuse's server-side price table (see 21f last bullet).
- The traced CRITICAL-log-classified-as-`warning` disagreements are accumulating in
  Langfuse — wiring them to **scores** (eval results attached to traces) connects this
  section to the eval harness.

**Revise these questions:**
- A generation is a span with extra fields — which fields, and why does an "agent call"
  not qualify as one?
- Why does the SDK add ~zero latency where the JSONL append blocks — and what did that
  trade away that `shutdown()` has to give back?
- With no IDs passed anywhere, what decides which trace a new generation lands in?
- Why is one analysis *two* traces, and why is `thread_id` the natural `session_id`?
- Why must `human_approval` never get an `@observe` decorator?

---

## 22. MCP — the agent becomes a protocol host 🔌

**What:** the Model Context Protocol — a standard for how LLM applications talk to
tool/data services. Before MCP, connecting *N* agents to *M* tools meant N×M custom
integrations; with it, every agent implements the protocol once as a client, every tool
source implements it once as a server, and any can talk to any: **N+M** ("USB-C for AI").

**The three roles (most common misconception: the LLM is NOT the client):**
```
 LLM (Gemini / Claude)          ← only ever sees text + tool schemas in its context
        ↕  (model API)
 HOST  (Claude Code, agent.py)  ← owns one MCP CLIENT per server connection
        ↕  JSON-RPC over stdio
 MCP SERVER (mcp_server.py)
        ↕
 PostgreSQL + pgvector
```
The host translates discovered tools into whatever its model speaks; the model just emits
"call `get_log` with `{...}`" and the host routes it. MCP standardizes the host↔server
leg only — which is exactly why it's vendor-neutral.

### 22a. The server — [mcp_server.py](mcp_server.py)
- One object, five tools: `mcp = MCPServer("log-classifier")` — [mcp_server.py:20](mcp_server.py#L20) —
  then `@mcp.tool()` on plain functions: `get_log`, `get_recent_logs`, `search_logs`,
  `find_similar_logs`, `log_stats`.
- **The docstring IS the API contract** — it's the description the model reads when
  deciding what to call; type hints compile into the argument JSON Schema. This paid off
  live: `search_logs`' docstring ("substring match... use find_similar_logs for
  meaning-based search") steered a model away from misusing it.
- **The stdio hard rule:** NOTHING may print to stdout — that's the protocol channel; a
  stray `print()` corrupts the JSON-RPC stream. db.py's `✅ Connected` banner is wrapped
  in `redirect_stdout(sys.stderr)` — [mcp_server.py:17-18](mcp_server.py#L17-L18) —
  because stderr is fair game (hosts show it as server logs).
- **Errors are data, not exceptions:** `get_log` returns `{"error": "Log 99 not found"}`
  as a *successful* result — [mcp_server.py:29-45](mcp_server.py#L29-L45). The protocol
  distinguishes "tool ran and reported a problem" (readable result; a model can adapt,
  the graph can route) from "the call itself failed" (`is_error` — unknown tool, crash).

### 22b. The wire — JSON-RPC 2.0, seen by hand
The lifecycle, which you drove manually through the MCP Inspector
(`npx @modelcontextprotocol/inspector python mcp_server.py`):
1. `initialize` — handshake: both sides declare protocol version + capabilities.
2. `notifications/initialized` — no `id` field = a *notification*, fire-and-forget.
   The server refuses real requests until it hears this (the lifecycle is enforced).
3. `tools/list` — **discovery, the core trick**: tools arrive as pure data (names,
   docstrings, schemas). Add a tool tomorrow; every client discovers it on next connect
   with zero integration code. Tools are *data, not code* from the client's view.
4. `tools/call` — a tool runs; the result comes back wrapped in a `content` array.

The `id` field matches responses to requests — that's what allows several requests in
flight at once. Results carry the payload **twice**: `content` (text blocks, for an LLM
to read) and `structured_content` (the original JSON, for code) — same data, two audiences.

**Transports:** stdio (host spawns the server as a subprocess — everything here) vs
streamable HTTP (remote server, many clients). Same messages either way.

### 22c. Three hosts, one server — N+M in practice
The identical `mcp_server.py` serves:
- **Claude Code**, registered project-scope via [.mcp.json](../.mcp.json) — the file
  `claude mcp add --scope project` would generate. `cwd` points at LogClassifier so
  db.py's `load_dotenv()` finds `.env`. Tools appear namespaced
  (`mcp__log-classifier__get_log`) so servers can't collide. Project-scope servers need
  one-time user approval — otherwise a cloned repo could auto-run arbitrary commands.
- **The Inspector** — the debugging UI where you played the host role by hand.
- **The LangGraph agent** (22d) — your own code as host.

`get_log` was written because the *agent* needed it — and Claude Code inherited it for
free on next connect. That's the N+M payoff, observed rather than claimed.

### 22d. agent.py as host — what actually changed
`from db import db` is **gone** from agent.py: the file that once ran raw SQL no longer
knows Postgres exists (importing it no longer even opens a DB connection).

- **Spawn spec:** `SERVER = StdioServerParameters(command=sys.executable, args=["mcp_server.py"], cwd=...)`
  — [agent.py:59-64](agent.py#L59-L64).
- **Session lifecycle:** `mcp_session()` — [agent.py:71-80](agent.py#L71-L80) — opens the
  connection **once per graph run** (each open = subprocess spawn + handshake; per-call
  would be wasteful) and publishes it via a module global for nodes.
- **The call helper:** `call_mcp()` — [agent.py:83-100](agent.py#L83-L100) — raises on
  `is_error` (protocol failure), returns `structured_content` (we're code, not an LLM).
- **The nodes collapsed:** `fetch_log` ([agent.py:122-133](agent.py#L122-L133)) and
  `find_similar` ([agent.py:164-182](agent.py#L164-L182)) went from SQL to ~3-line tool
  calls. The SQL didn't disappear — it moved behind the protocol boundary, into the server.
- **Session ≠ state.** `resume_analysis` opens a *fresh* MCP session — the one from
  `start_analysis` died with that call. The pause survives in the **checkpointer**, not
  the connection: sessions are per-run, state is per-thread —
  [agent.py:356-372](agent.py#L356-L372). (The observability twin of this split is 21d.)

The proof of a clean swap: the terminal output is identical to the pre-MCP version. Same
graph, same interrupt — the only tell is `✅ Connected to PostgreSQL` now printed by a
subprocess, twice per full run (once per session).

### 22e. The gotchas actually hit 🐛
1. **`FastMCP` doesn't exist in mcp 2.x.** The SDK's major version renamed it to
   `MCPServer` (`mcp.server.mcpserver`); the old module is a tombstone that exists only
   to raise a helpful error. Same decorator API — a two-line migration, but a reminder
   that major versions break imports on purpose.
2. **The async ripple.** The MCP client is async-only → nodes became `async def` →
   the graph must run via `ainvoke` → the sync `SqliteSaver` *refuses* async calls **by
   design** (`NotImplementedError`, pointing at `AsyncSqliteSaver`). One async dependency
   at the bottom pulled the whole call path async: nodes, entry points, `main.py`'s two
   endpoints (already `async def` — two `await`s), and the CLI (`asyncio.run`).
3. **`RuntimeError: no running event loop`.** `AsyncSqliteSaver.__init__` calls
   `asyncio.get_running_loop()` — at import time there is no loop, so the module-level
   `graph = builder.compile(...)` crashed. Fix: lazy compile via `_ensure_graph()` —
   [agent.py:313-320](agent.py#L313-L320) — on first use *inside* the loop.
   **The rule:** sync resources can be built at import time; async resources belong
   inside the loop (they often bind to it at construction — same trap awaits with
   aiohttp sessions and async DB pools).
4. **`similarity` arrives as a string.** The server's `ROUND(...)::numeric` serializes
   as `"0.6817"`; `write_report`'s `:.2f` needs a float — so `find_similar` converts at
   the boundary. Protocol payloads are JSON: types flatten, and the *client* owns
   re-widening them.

### 22f. Still open in this area
- **`semantic_search(query: str)`** — the one real gap found in live use: no tool
  searches all logs by the *meaning* of free text (`find_similar_logs` needs an existing
  log id as seed; `search_logs` is substring-only). Embed the query text, run the same
  pgvector cosine scan. All the pieces already exist.
- **[kill_test.py](kill_test.py) still calls the old sync API** — needs the same
  `asyncio.run` treatment before it runs again.
- **Resources and prompts** — tools are one of three server primitives (tools =
  model-controlled, resources = app-controlled readable data like `logs://recent`,
  prompts = user-controlled templates). Only tools built so far.
- **Streamable HTTP transport** — one `mcp.run()` argument away; would show the
  remote-server half of the protocol.

### 22g. Process model — there is no standing server 👻
Ask "where is the server running?" and the answer is usually **nowhere**. stdio transport
means the server is *spawned on demand as a child process of whoever needs it* and lives
exactly as long as that connection — each host gets a private copy with its own Postgres
connection. Three hosts connected = three `python mcp_server.py` processes. (Contrast:
HTTP transport = one long-lived server, many clients.)

- During the interrupt pause, **no server process exists at all** — `start_analysis`'s
  session already closed; the resume spawns a fresh one. Same lesson as 12d: nothing is
  running while it waits. Verifiable in Task Manager mid-pause.
- **Kill the server mid-run:** the pipe breaks → the in-flight `tools/call` gets EOF →
  `call_mcp` raises → the run dies. Damage is bounded: the checkpointer holds every
  completed node's state, and Postgres rolls back the in-flight transaction (5a working
  in your favor). Blast radius = one host–server pair.
- **Kill the host:** no orphaned server — `mcp.run(stdio)` blocks reading stdin, the OS
  closes the pipe when the parent dies, the server reads EOF and exits. Nobody wrote
  cleanup code; **the plumbing is the cleanup**. A genuine design virtue of stdio.

**Revise these questions:**
1. Who are the host, client, and server in this project — and why does the LLM itself
   never speak MCP? What does that split buy (N+M vs N×M)?
2. Why must an MCP stdio server never print to stdout, and how does mcp_server.py deal
   with db.py's connection banner?
3. `get_log(99999)` returns `{"error": ...}` as a *successful* call. Why is that better
   than raising — for a model caller and for the graph's routing alike?
4. What does `tools/list` return, and why does "tools are data, not code" mean a new
   tool needs zero client-side integration?
5. A result carries both `content` and `structured_content` — who is each for, and which
   does `call_mcp` use?
6. Why does the resume half of an analysis open a brand-new MCP session yet continue the
   same run? Where does the pause actually live?
7. Trace the async ripple: why did adding an MCP client force the checkpointer to change
   class — and why couldn't the new one be constructed at import time?
8. `find_similar_logs` was called by you in the Inspector, by Claude Code, and by
   agent.py — what changed between those three calls, and what didn't?
9. Right now, with no host connected, how many `mcp_server.py` processes are running?
   And during the agent's approval pause?
10. The server is killed mid-run: what exactly does the client see, what survives, and
    why can't the server become a zombie when the *host* dies instead?

---

## 23. Hybrid search — full-text + vectors, fused with RRF 🔀

**What:** two retrieval modes with opposite failure shapes, merged. Vector search
(section 8) finds *meaning* but shrugs at exact tokens; Postgres full-text search finds
*exact words* but has no idea "refused" and "timeout" are cousins. Hybrid = run both,
fuse the rankings. This lives **inside** `find_similar_logs` — the tool's contract didn't
change, so no caller (agent, Claude Code, Inspector) needed touching: an MCP dividend.

### 23a. Postgres full-text search — the second branch
**In your code:** the keyword query inside `find_similar_logs` — [mcp_server.py:116-186](mcp_server.py#L116-L186)
- `to_tsvector('english', message)` normalizes text to **lexemes** ("refused" → "refus" —
  stemming, stopword removal), `@@` tests a match, `ts_rank` scores it.
- No GIN index on the expression — a seq scan, fine at 14 rows. The fix at scale is an
  **expression index**, the FTS twin of section 7's ivfflat lesson.

### 23b. The gotcha that shaped the query 🐛 — AND vs OR semantics
The textbook snippet uses `plainto_tsquery`, which **ANDs every lexeme**. Our query text
is an *entire log message* — demanding another log contain ALL of its words. Measured on
real data before trusting the hunch: for log 65's message, `plainto` matched **zero**
logs; `websearch_to_tsquery` with `" OR ".join(message.split())` matched five. Same safe
parsing, any-shared-term semantics, ranked by overlap.

**Lesson:** a query function's *combining semantics* (AND vs OR) matters more than its
name suggests — and it's checkable with one SELECT before committing to it.

The FTS branch also **joins `log_embeddings`** so keyword hits still report true cosine
`similarity` — every merged entry keeps a uniform shape for downstream consumers
(`find_similar`'s `float(...)`, `write_report`'s `:.2f`).

### 23c. Reciprocal Rank Fusion — merging two rankings
**In your code:** `reciprocal_rank_fusion` — [mcp_server.py:82-114](mcp_server.py#L82-L114)
- Each list contributes `1/(k + rank)` per item; ids strong in **both** lists beat ids
  that top only one. Scores are **ordinal** — only ever sorted, never thresholded.
- **The dict-overwrite bug to avoid:** the naive merge
  `{log["id"]: log for log in vec + kw}` lets keyword rows *overwrite* vector rows,
  dropping fields on shared ids. The fix is a field-**union** merge
  (`{**old, **new}`) so an id keeps its vector `similarity` AND keyword `rank`.
- **Candidate pool > limit:** both branches fetch 10, fused output is cut to `limit` —
  fusing two top-3 lists could only *shuffle* 3 rows, never surface anything new.

### 23d. Verified on real data — fusion changed the answer, explainably
Source log 65 ("Docker container postgres_1: Connection refused - health check failed"):

| | order |
|---|---|
| vector alone | 53, 59, 62, … |
| keyword alone | 59, 63, 53, … |
| **fused** | **59, 53, 63** |

- 59 (SMTP "**refused connection**") overtook 53 — strong in both lists beats top-of-one.
- 62 ("Authentication service unreachable" — semantically close, **zero shared words**)
  dropped out entirely.
- For source 59, fusion promoted 63 ("**Failed to send** event" ↔ "Unable to **send**…")
  over wordless 62 — a delivery-failure log matching a delivery-failure log on exactly
  the words that matter.

A challenge worth remembering: "is there a better match being buried?" was answered by
**inventorying the corpus first** — 59 is the only notification log in the 14 rows, so
the loose neighbors *are* the best available. Judge retrieval against what the data
actually contains, not against what feels like it should exist.

### 23e. The k=60 question — sweep it, don't inherit it
RRF's `k=60` is a **web-scale default**, and at 14 rows the scores visibly compress
(0.0325 vs 0.0323 — near-ties). Symptom or problem? Swept k ∈ {1, 5, 10, 60} on real
sources: the resulting **order was identical** for source 59 and differed by one
adjacent deep-list swap for source 65. At this scale ranking is dominated by
*both-lists membership*, not by k — and since scores are ordinal, the compression is
cosmetic. Kept 60 **on purpose**: its damping also protects against the noisier branch
(OR-keyword ranks on generic shared tokens like "failed"; at k=1 it promoted the
*payment* log). Decision + date live in the docstring — [mcp_server.py:82-114](mcp_server.py#L82-L114).

**The transferable lesson:** a textbook constant isn't wrong at a different scale, but
it's *unexamined* until swept against your own data — and the sweep cost one script and
ten seconds. Revisit triggers, written down: corpus growth, or anyone thresholding on
`rrf_score`.

### 23f. Still open in this area
- **GIN expression index** for the FTS branch (23a) — needed the day the corpus is real.
- `rrf_score` is exposed in results but must stay ordinal — if anyone thresholds on it,
  the k decision (23e) must be revisited.
- Same neighbors: `semantic_search` (22f) would complete the retrieval trio —
  substring, seed-log similarity, free-text meaning.

**Revise these questions:**
1. What failure shape does each branch have — what does vector search miss that FTS
   catches, and vice versa? Which retrieved-log example demonstrates each direction?
2. Why did `plainto_tsquery` return zero matches when the query text is a whole log
   message, and what changed with OR-joined `websearch_to_tsquery`?
3. In RRF, why does an id ranked #2 in both lists beat one ranked #1 in a single list?
   What does k actually damp?
4. Why must the merge be a field-union, not a dict overwrite? Which fields collide?
5. Why do both branches fetch a candidate pool of 10 when the tool returns 3?
6. The k-sweep showed near-identical *orderings* despite compressed *scores* — why does
   that make the compression cosmetic, and what downstream change would make it matter?
7. Why did the FTS branch join `log_embeddings` — which two downstream consumers does
   the uniform `similarity` field protect?
8. Why did the hybrid upgrade require zero changes in agent.py, Claude Code, or the
   Inspector?

---

## 24. Guardrails & prompt-injection defense 🛡️

**What:** guardrails are programmatic checks around an LLM that constrain input and output,
*because* the model is neither trustworthy nor deterministic. Prompt-injection defense is
one instance.

### 24a. The core idea, and why it's NOT SQL injection
A prompt is one text stream; the model can't tell your trusted instructions from untrusted
data pasted into it. **Injection = data written to look like instructions.** Same *shape*
as SQL injection (untrusted input crossing into the control plane) — but SQL injection is
**solved** (`%s` params are a hard structural boundary, §5) and prompt injection is **not**:
LLMs have no `execute(prompt, params)` that renders data inert. Every defense is
*mitigation*, never a guarantee. The attack surface here is one line — [classifier.py:59](classifier.py#L59),
where untrusted `message` meets the trusted rubric.

### 24b. Direct vs indirect; the threat here
**Indirect injection** is the dangerous kind: hostile text rides in through *ingested data*
(a crafted log message) and detonates later when a trusted process classifies it. The
concrete threat is a **severity-downgrade attack**: force a critical incident to be labelled
`info`, so the agent buries it (no escalation, no report).

### 24c. The defense layers (weakest→strongest, none complete)
1. **Delimiting** — wrap untrusted text in named boundaries; beaten by delimiter injection.
2. **Framing** — tell the model the data may contain fake instructions to ignore.
3. **System-channel separation** — rubric in the model's dedicated `system_instruction`
   channel, log as user content; the closest thing to a real trust boundary.
4. **Sandwiching** — restate the true instruction *after* the data.
5. **Output guardrails** — check the *result*: e.g. stored `level=ERROR` but predicted
   `severity=info` = a suspicious downgrade. **Attack-agnostic** — catches the effect
   regardless of cause. Note `parse_classification` (§9/12) is already an output guardrail,
   but it validates **shape, not truth** — an injected `severity:info` is valid JSON and
   sails through. Form-checks can't catch content attacks.

### 24d. Measure before defending — the whole point 🎯
Built a **red-team harness** (`scratchpad/redteam_*.py`), each payload pairing ALARMING
content with an injected downgrade so honest (`critical`) and injected (`info`) *disagree*
— making a compromise unambiguous. (The original seed payload, id 61, was *ambiguous*:
benign content whose honest label `info` == the injection's goal, so its output proved
nothing. A discriminating test is the first requirement.)
- **Baseline: 0/11** — four naive + seven strong attacks (few-shot poisoning, rubric
  redefinition, forged conversation, base64 indirection, language-switch, policy-token
  social engineering, format-lock JSON prefill) **all HELD** on `gemini-3.5-flash`.
- Had we skipped the baseline, added defenses, then seen 0/11, we'd have claimed a **fake
  victory** over attacks that never worked. The null result is the more valuable outcome.
- A stray **503 corrupted one control** (empty output → `unknown` → false `MISCLASS`) until
  the harness got 503-retry — instrumentation must distinguish "model failed" from "API
  failed," same arbiter lesson as JSONL-vs-Langfuse (§21f).

### 24e. Why it resists, and the honest conclusion
Injection succeeds against *open-ended* tasks; this is the opposite — a **narrow task with
a firm, specific rubric demanding a constrained output that contradicts the injection**.
The narrowness IS the defense. So the disciplined call: **do not add prompt-level injection
defenses to `classify`** — measured evidence says 0/11 → 0/11, no benefit, just complexity.
Two things keep independent value and aren't refuted by 0/11:
- The **output guardrail is a *downgrade detector*, not an injection blocker** — it catches
  a bad label whether the cause is injection, hallucination, or a mislabeled log. Its worth
  is independent of the injection threat.
- **Least privilege is the real defense, already in place:** the classifier only *labels*;
  MCP tools are **read-only**; the agent can't delete/spend/send. A successful downgrade
  mislabels one row — bounded harm. Injection becomes severe only where output drives a
  *privileged action* (a tool-calling agent), which this isn't.

**The mature framing:** not "make injection impossible" (you can't) but "make a successful
injection cheap to survive" — least privilege + defense-in-depth + detect-the-effect.

**Revise these questions:**
- Why does `%s` fully solve SQL injection but nothing fully solves prompt injection?
- Why was the original id-61 payload a useless test, and what makes a payload discriminating?
- What did measuring the 0/11 baseline *save* you from concluding?
- Why is "add a delimiter/framing defense to classify" the wrong call given the data — and
  which defense still earns its place, and why?
- Why is this classifier low-risk for injection in a way a tool-calling agent would not be?

---

## 25. Multi-agent orchestration — sub-agents & an orchestrator 🧑‍✈️

**What:** the step from a *workflow* (you predefine the node order) toward an *agent* system
(a controller decides what runs). Two moves: (1) turn the graph's work-nodes into
**standalone sub-agents** with clean input/output, and (2) name the routing decision as an
explicit **orchestrator**.

### 25a. Workflow vs agent vs multi-agent — the distinction that matters
- **Workflow:** control flow is predefined by *you*; the LLM fills in a value, an `if`
  decides the next step. Your whole graph is this — deterministic, cheap, debuggable.
- **Agent:** the LLM decides its *own* control flow (which tool, whether to loop, when
  it's done). Claude Code is this; so was the `find_similar_logs` tool-choosing.
- **Multi-agent:** several such agents, each with its own context/role/tools, coordinating.

A `classify` node running a fixed prompt is NOT an agent — it's one LLM call in a fixed
slot. Building a workflow when the steps are knowable is the *right* instinct, not a limit.

### 25b. Sub-agents — standalone, independently callable
`classify` and `write_report` were extracted from the graph into functions with clean
contracts — [agent.py](agent.py):
- `classify_agent(log) -> {classification, attempts}`
- `report_agent(log, classification, similar_logs) -> report`

They take plain arguments, not graph `state`, so the orchestrator, the eval, or a REPL can
call them identically; the graph nodes are now thin wrappers. **The retry loop moved INSIDE
`classify_agent`** (the `while attempt < MAX_ATTEMPTS`), so the sub-agent owns its own
escalation and returns a FINAL answer. That's why §12c's graph *cycle* is gone — same
lesson (exit condition + escalating prompt), relocated from a graph edge into the function.

### 25c. The orchestrator — a DETERMINISTIC boss
The report decision (the former anonymous `route_after_approval`) is now named
`orchestrator` — [agent.py](agent.py). It gates on two things: did the operator approve,
AND does the severity warrant a report at all. New behavior: an **approved-but-info log now
gets NO report**, where before approval alone triggered one; info/unknown routes to a new
`skip_report` terminal node.

**Why deterministic (an `if`, not an LLM):** the route is fully knowable from state. You
hand routing to a *boss agent* only when the path genuinely varies per input in ways rules
can't capture. On a knowable pipeline an LLM orchestrator is strictly worse — it adds a
call before every step (~2x cost/latency), replaces a 100%-correct `if` with a
usually-correct guess, and turns "why did it skip?" from reading one line into inspecting an
LLM's reasoning. **Orchestration is a scaling answer to combinatorial routing, not a
sophistication upgrade** — the payoff arrives only when you add many *optional* capabilities
whose combinations explode past what hand-wired routers can cover.

### 25d. The orchestrator-workers pattern, demystified
A boss agent has no magic: it's a **tool-calling agent whose tools are other agents**. It
needs exactly what any tool-caller needs — a described contract per worker (name, inputs,
outputs). That's tool schemas *again* — the same MCP docstrings (§22) that let a model pick
the right tool. The hard parts if you ever go LLM-driven: **termination** (LLMs are bad at
stopping — needs an explicit `finish` action + a step cap, the `attempts` lesson at graph
scale), **context growth** (the boss re-reads the blackboard each turn), and **error
recovery becoming the boss's judgment** instead of a deterministic edge.

### 25e. Verified — the skip is real, and measurable
Two real logs through the real graph, auto-approving the interrupt:
- log 35 "Disk space at 95%..." -> `warning` -> **write_report** ran (a real report; a
  `write_report` generation appears in the trace).
- log 30 "MFA code verified..." -> `info` -> **skip_report**, with **no report LLM call at
  all** (no `write_report` generation; the skipped path cost $0 in report tokens).

Trace `orch-verify-critical-35` carries a write_report generation; `orch-verify-info-30`
carries a `skip_report` span instead. The orchestrator's value made measurable: it doesn't
just label the info log differently, it *doesn't spend* a call on a report nobody needed.

**Revise these questions:**
- What's the difference between a workflow, an agent, and a multi-agent system — and which
  one is your graph?
- Why is `classify_agent` owning its retry loop the reason §12c's graph cycle disappeared?
- Why is the orchestrator an `if` and not an LLM here — and what would have to change about
  the pipeline to justify an LLM boss?
- "A boss agent is a tool-calling agent whose tools are other agents" — what does each
  worker therefore need, and where have you seen that requirement before?
- How does the info-log run prove the skip happened, in BOTH state and Langfuse?
