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

---

## 1. FastAPI — the web framework

**What:** an async Python framework that turns functions into HTTP endpoints and
validates data automatically.

**In your code:**
- App is created once: `app = FastAPI(...)` — [main.py:72](main.py#L72)
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

**In your code:** [main.py:47-61](main.py#L47-L61)
```python
class LoginRequest(BaseModel):
    username: str
    password: str
```
If a client POSTs `/login` without a `password`, Pydantic rejects it with a 422
*before your function even runs*.

- `Optional[int] = None` means the field can be missing — [main.py:56](main.py#L56)
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
- **Issue** on login: `create_access_token` — [main.py:86-88](main.py#L86-L88)
  ```python
  jwt.encode({"sub": username, "exp": expire}, SECRET_KEY, algorithm="HS256")
  ```
  `sub` = subject (who), `exp` = expiry. Signed with `SECRET_KEY`.
- **Verify** on every request: `verify_token` — [main.py:90-95](main.py#L90-L95)
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

**In your code:** `get_embedding` calls Gemini — [main.py:101-107](main.py#L101-L107)
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

**In your code:** `classify_log_with_gemini` — [main.py:109-124](main.py#L109-L124)
- You send a **prompt** that demands JSON-only output — [main.py:110-118](main.py#L110-L118)
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
`CLASSIFICATION_MODEL` / `EMBEDDING_MODEL` ([main.py:40-41](main.py#L40-L41),
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
python agent.py 56
# 7. Prove the durable checkpointer: pause in one process, resume in another
python kill_test.py start
python kill_test.py resume
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
- Bare `except:` blocks still in [main.py:94](main.py#L94) and [main.py:123](main.py#L123).
- Secrets have insecure defaults (`password123`, `your-secret-key-change-me`).
- `ivfflat` on 3072 dims can hit pgvector's 2000-dim index limit on some versions — if a
  future `CREATE INDEX` errors, that's why (drop the index or reduce dimensions).

None of these block learning; they're the gap between a sandbox and production.
