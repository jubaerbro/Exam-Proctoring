# Running Sentinel locally

Two ways. Pick by what you want to do.

| | **Path A — Docker** | **Path B — native + VS Code** |
|---|---|---|
| Best for | seeing it work, demos, running the full stack | breakpoints, editing, running tests |
| Needs | Docker Desktop | Docker Desktop *(for Postgres + Redis only)*, Python 3.11+, Node 20+ |
| Startup | one command | ~10 minutes the first time |
| Debugging | awkward | F5 in VS Code |

If you just want it running, do **Path A**. If you want to step through the code
in VS Code, do **Path B** — that is what "run it in VS Code" usually means.

---

## Before either path: the one-time database fix

If you have run `docker compose up` on this project before, your Postgres volume
was created **before** `infrastructure/postgres/01-roles.sh` learned to install
the `pgcrypto` and `citext` extensions. That script only runs on an empty data
directory, so an existing volume will never pick up the change, and the migration
will keep failing with:

```
psycopg.errors.InsufficientPrivilege: permission denied to create extension "pgcrypto"
```

There is nothing in that volume you want to keep — it is demo data. Wipe it:

```powershell
docker compose down -v
```

`-v` is what removes the volumes. Without it the old database survives and the
error comes back.

> If you *do* have data you care about, keep the volume and run this instead:
>
> ```powershell
> docker compose up -d postgres
> docker compose exec postgres psql -U postgres -d sentinel -c "CREATE EXTENSION IF NOT EXISTS pgcrypto; CREATE EXTENSION IF NOT EXISTS citext;"
> ```

---

## Path A — Docker

From `D:\exam proctoring\sentinel-source`, in PowerShell:

```powershell
# 1. Config. .env is gitignored; the CHANGEME values are fine for local use.
Copy-Item .env.example .env -Force    # skip if you already have a .env

# 2. Start everything.
docker compose up --build
```

First run takes a few minutes (three images to build, npm install inside the web
container). When it settles you have:

| | URL | |
|---|---|---|
| Candidate web app | http://localhost:3000 | this is the one you want |
| API docs (Swagger) | http://localhost:8000/docs | |
| API health | http://localhost:8000/ready | tells you which dependency is unhappy |
| MinIO console | http://localhost:9001 | not used until Phase 6 |

### Signing in

Accounts come from the seed. The password is whatever `SEED_PASSWORD` is in your
`.env` — `CHANGEME_seed_password` if you copied `.env.example` unchanged.

| Account | Role | Notes |
|---|---|---|
| `aisha.rahman@demo-university.example.edu` | candidate | **start here** — has the seeded exam assigned |
| `daniel.okafor@…`, `mei.tanaka@…`, `luis.ferreira@…`, `nadia.hassan@…` | candidate | same exam, different papers |
| `instructor@demo-university.example.edu` | instructor | authoring is API-only; use `/docs` |
| `admin@demo-university.example.edu` | org_admin | **requires MFA enrolment first** — see below |
| `reviewer@demo-university.example.edu` | reviewer | same |

Sign in as the candidate, click **Start or continue**, and you get the exam
runner: a deterministic paper, a server-driven countdown, autosave, and submit.
Reload the page mid-exam — nothing is lost. That is the Phase 2 deliverable.

### Signing in as org_admin or reviewer

MFA is mandatory for those two roles and login **fails closed** until an
authenticator is enrolled. That is deliberate. The 403 you get back carries an
`enrolment_token` that can do exactly one thing:

```powershell
# 1. Get the enrolment token (read it out of the 403 body).
curl.exe -s -X POST http://localhost:8000/api/v1/auth/login `
  -H "content-type: application/json" `
  -d '{\"email\":\"admin@demo-university.example.edu\",\"password\":\"CHANGEME_seed_password\"}'

# 2. Begin enrolment — returns a `secret` and an otpauth:// URI.
curl.exe -s -X POST http://localhost:8000/api/v1/auth/mfa/enrol -H "authorization: Bearer <ENROLMENT_TOKEN>"

# 3. Put the secret in any TOTP app (Google Authenticator, 1Password, Aegis),
#    then confirm with a code from it.
curl.exe -s -X POST http://localhost:8000/api/v1/auth/mfa/enrol/confirm `
  -H "authorization: Bearer <ENROLMENT_TOKEN>" -H "content-type: application/json" `
  -d '{\"secret\":\"<SECRET>\",\"code\":\"123456\"}'
```

After that, sign in normally at http://localhost:3000 and enter a code.

### Running code questions (the judge)

The judge is a separate profile because it needs the sandbox images and, in
development, the host's Docker socket:

```powershell
# Build the three sandbox images (a few minutes; ~2 GB for the C++ one).
docker compose --profile build-only build judge-python311 judge-cpp20 judge-java17

# Start the worker alongside the rest of the stack.
docker compose --profile judge up
```

Without the worker running, pressing **Run sample tests** on a coding question
leaves the run `queued` forever. That is the correct behaviour — the API never
executes code — but it looks like a hang, so start the profile before demoing a
coding question.

> **The judge worker mounts `/var/run/docker.sock`.** That is root-equivalent on
> your machine. It is acceptable on a laptop and a critical vulnerability
> anywhere else; production uses a rootless daemon on an isolated host pool. The
> API deliberately cannot reach a container runtime at all.

To run the sandbox security suite yourself:

```powershell
docker compose --profile build-only build judge-python311
$env:JUDGE_TEST_IMAGE = "sentinel-judge-python311:latest"
.\.venv\Scripts\Activate.ps1
pytest -q tests\security
```

It skips loudly if there is no Docker daemon or no image — a judge test that
passes without executing anything would be worse than no test.

### Useful commands

```powershell
docker compose logs -f api        # follow the API logs
docker compose logs migrate       # why the migration failed
docker compose ps                 # what is actually up
docker compose down               # stop, keep data
docker compose down -v            # stop, wipe data
docker compose run --rm seed      # re-run the seed (idempotent)
```

---

## Path B — native, with VS Code debugging

The API and web app run on your machine; only Postgres and Redis stay in Docker.

### 1. Infrastructure only

```powershell
docker compose up -d postgres redis
```

Wait for `docker compose ps` to show postgres as `healthy`.

### 2. Environment

Native processes cannot resolve `postgres` or write to `/run/keys` — those are
container-only. Copy the native profile and leave `.env` alone for Docker:

```powershell
Copy-Item .env.local.example .env.local
```

`.env.local` is gitignored and differs from `.env` in exactly four things:
`DATABASE_URL` and `MIGRATION_DATABASE_URL` point at `localhost`, `REDIS_URL`
points at `localhost`, and the key paths are a local `.keys` folder.

### 3. Python

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e ".\apps\api[dev]"
```

If `py -3.11` is not found, install Python 3.11 or newer from python.org and
tick **Add python.exe to PATH**.

### 4. Keys, migrations, seed

```powershell
.\scripts\load-env.ps1 .env.local     # loads .env.local into this shell
python .\scripts\keygen.py --out .keys
cd apps\api ; alembic upgrade head ; cd ..\..
python -m scripts.seed
```

`load-env.ps1` only affects the shell you run it in. Open a new terminal and you
need to run it again — or just use the VS Code launch configs below, which load
the file themselves.

### 5. Run

Two terminals:

```powershell
# terminal 1 — API
.\scripts\load-env.ps1 .env.local
cd apps\api
uvicorn sentinel_api.main:app --reload --port 8000
```

```powershell
# terminal 2 — web
cd apps\web
npm install
npm run dev
```

Open http://localhost:3000.

### 6. Tests

```powershell
.\scripts\load-env.ps1 .env.local
cd apps\api
pytest -q                 # 144 tests, needs the live database
pytest -q -m security     # the 51 security-boundary tests
```

The suite runs migrations and the seed itself, so it works against an empty
database. It is also written to pass against a warm one — run it twice.

---

## VS Code

`.vscode/` is committed with four things ready to use.

**Extensions.** Open the Extensions panel and accept the workspace
recommendations (Python, Pylance, Ruff, ESLint, Prettier, Tailwind, Docker).

**Run and Debug (F5).** The dropdown has:

| Configuration | What it does |
|---|---|
| `API (uvicorn, reload)` | starts the API with the debugger attached — breakpoints work |
| `Web (Next.js dev)` | starts the web app |
| `Full stack (API + Web)` | both at once |
| `Seed database` | runs `scripts/seed.py` |
| `Pytest: current file` | debugs the test file you have open |

All of them load `.env.local`, so Path B step 2 is the only prerequisite.

**Testing panel.** Python tests are configured against `apps/api`. Click the
flask icon in the sidebar to run or debug individual tests.

**Debugging tips for this codebase.** The interesting breakpoints are:

- `assessment/paper.py` → `generate_paper` — watch a candidate's paper being dealt
- `assessment/delivery.py` → `save_answer` — the revision/stale-save logic
- `grading/graders.py` → `grade` — why a mark is what it is
- `tenancy/deps.py` → `authorize` — why a request was refused

---

## When it does not work

**`permission denied to create extension "pgcrypto"`**
Your Postgres volume predates the fix. `docker compose down -v`, then up again.
See the top of this file.

**`/usr/bin/env: 'bash\r': No such file or directory`**
Git converted `infrastructure/postgres/01-roles.sh` to Windows line endings. The
container needs LF. Fix it once:

```powershell
git config core.autocrlf false
git rm --cached -r . ; git reset --hard
```

Or set the file to LF in VS Code's status bar (bottom right, click `CRLF`).

**`Ports are not available: 0.0.0.0:5432`**
You already have PostgreSQL installed on Windows and it owns 5432. Either stop
that service, or change the host side of the mapping in `docker-compose.yml` to
`"5433:5432"` and point `DATABASE_URL` at 5433.

**`EADDRINUSE :3000` or `:8000`**
Something else is on the port. `netstat -ano | findstr :3000`, then
`taskkill /PID <pid> /F`.

**The web app loads but every request fails**
The browser is calling the API at `NEXT_PUBLIC_API_BASE_URL`, which is baked in
at *build* time. If you changed it, rebuild: `docker compose up --build web`, or
for Path B delete `apps/web/.next` and run `npm run dev` again.

**`Refused to execute script … MIME type ('text/html')`**
A stale `.next` build. Stop the web server, delete `apps/web/.next`, start again.

**Login redirects to `/login?email=…&password=…`**
The page has not hydrated — usually the same stale-`.next` problem above. The
form is `method="post"` so it fails visibly rather than leaking the password into
a GET, but it still means the JavaScript did not load.

**`DATABASE_URL must not use the 'postgres' superuser`**
Working as intended. RLS is bypassed for superusers, so the API refuses to start
as one. Use the `sentinel_app` role.

**The reviewer or admin account cannot sign in**
Also working as intended, until you enrol an authenticator. See "Signing in as
org_admin or reviewer" above.

---

## What you can and cannot do yet

Working end to end today: sign in, see your assessments, sit an exam, autosave,
survive a reload, submit, get a mark for choice / multiple-choice / short-answer
/ numeric questions.

Not yet: running or scoring code questions (Phase 3), integrity monitoring
(Phase 4), the signed audit chain (Phase 5), evidence capture (Phase 6), the
reviewer workflow (Phase 8), and any staff authoring UI — authoring is API-only,
through http://localhost:8000/docs.

[`docs/PHASE_REPORTS.md`](docs/PHASE_REPORTS.md) is the honest status, including
a section on what looks finished and is not.
