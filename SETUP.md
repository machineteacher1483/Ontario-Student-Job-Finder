# Setup — literal step by step

Assumes: a computer, VS Code, a GitHub account, and the Netlify site you already have.

---

## Phase A — Get it running on your own machine (30 min)

### A1. Check Python
Open a terminal (VS Code: `` Ctrl+` ``) and run:

    python3 --version

Need 3.10 or higher. On Windows the command is usually `python --version`.
No Python? Install from python.org — on Windows tick **"Add Python to PATH"**
during install, or nothing below will work.

### A2. Unzip and open in VS Code
Unzip `oif-pipeline.zip` somewhere sensible. In VS Code: **File → Open Folder**,
pick the `oif-pipeline` folder. Not a parent folder, not a single file — that
folder, so the relative paths in the scripts resolve.

### A3. Install the Python extension
Extensions sidebar (`Ctrl+Shift+X`) → search "Python" → install the Microsoft one.

### A4. Create a virtual environment
A venv is a private Python install for this project, so its packages can't
collide with anything else on your machine.

    python3 -m venv .venv

Activate it:

    # macOS / Linux
    source .venv/bin/activate
    # Windows PowerShell
    .venv\Scripts\Activate.ps1
    # Windows cmd
    .venv\Scripts\activate.bat

Your prompt should now start with `(.venv)`. If PowerShell refuses with an
execution-policy error, run:
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`

Then tell VS Code to use it: `Ctrl+Shift+P` → "Python: Select Interpreter" →
pick the one with `.venv` in the path.

### A5. Install dependencies

    pip install -r requirements.txt
    pip install pytest

### A6. Run the tests — THIS IS THE REAL "DOES IT COMPILE" CHECK

    python -m pytest tests/ -q

Expect `115 passed`. This imports every module, so it catches syntax errors,
bad imports and broken logic in one shot. If this passes, the code is sound
on your machine.

### A7. Run the pipeline offline

    python scripts/refresh.py --config config/sources.demo.json --no-verify --force

Expect `5 raw -> 3 Ontario student roles` and `DONE: 3 active`.
No API keys or internet needed — it reads a fixture file.

### A8. Run the validator on the real data

    python scripts/validate_listings.py --input data/site_listings.json

Then open `reports/review_queue.html` in a browser. That's your work queue.

### A9. View the site locally
`fetch()` is blocked on `file://` URLs, so double-clicking index.html will NOT
work. You need a local server:

    python -m http.server 8000

Visit **http://localhost:8000**. Stop it with `Ctrl+C`.

(Alternative: install the "Live Server" VS Code extension, then right-click
index.html → "Open with Live Server".)

**Checkpoint:** filters work, cards render, the meta line shows counts.

---

## Phase B — Get it into GitHub (20 min)

### B1. Decide the repo layout
One repo holds both the site and the pipeline. The site is at the root so
Netlify serves it with no configuration:

    your-repo/
      index.html              <- the site
      data/opportunities.json <- what the site reads
      pipeline/  scripts/  config/  tests/
      .github/workflows/

### B2. Initialise git

    git init
    git add .
    git commit -m "Add refresh pipeline, validation and JSON-driven frontend"

### B3. Create the GitHub repo
On github.com: **New repository**. Do NOT tick "add a README" — you already
have files. Then:

    git remote add origin https://github.com/YOURNAME/YOURREPO.git
    git branch -M main
    git push -u origin main

### B4. Confirm .gitignore is doing its job

    git status --ignored --short | head

`.env` and `__pycache__/` should be listed as ignored. If `.env` ever shows up
as a tracked file, stop and fix it — that's how API keys leak.

---

## Phase C — Connect Netlify (10 min)

### C1. Link the repo
Netlify dashboard → your site → **Site configuration → Build & deploy →
Link repository**. Pick your GitHub repo.

(If your current Netlify site was drag-and-drop deployed, it isn't connected to
git. Either link it, or create a new site via **Add new site → Import an
existing project**.)

### C2. Build settings
- Build command: **leave empty** (there's nothing to build — it's static)
- Publish directory: **`.`** (repo root)
- Branch: `main`

### C3. Deploy and check
Netlify builds in ~30 seconds. Open the live URL. Open DevTools (F12) →
Network tab → reload → confirm `opportunities.json` returns **200**.

A 404 there means the publish directory is wrong.

---

## Phase D — Turn on automation (15 min)

### D1. Get Adzuna credentials
Register free at https://developer.adzuna.com/ . You get an `app_id` and
`app_key` immediately.

### D2. Store them as GitHub secrets
Repo → **Settings → Secrets and variables → Actions → New repository secret**.

    Name: ADZUNA_APP_ID    Value: <your id>
    Name: ADZUNA_APP_KEY   Value: <your key>

Never put these in a file you commit.

### D3. Check your source tokens
Locally, with the venv active:

    python scripts/check_sources.py

The board tokens in `config/sources.json` are placeholders I could not verify.
A `FAIL 404` means a wrong token or a company that moved ATS. Fix or disable
each one (`"enabled": false`) until the table is clean.

Finding a real token: visit the company's careers page. If the URL is
`boards.greenhouse.io/shopify`, the token is `shopify`. If it's
`jobs.lever.co/clio`, the slug is `clio`.

### D4. First manual pipeline run
Repo → **Actions** tab → "Refresh opportunity dataset" → **Run workflow** →
tick **force** and **dry_run** → Run.

Dry run means it fetches and processes but publishes nothing. Read the log.
If it looks right, run again with force on and dry_run OFF.

### D5. Confirm the loop closed
Check three things in order:
1. GitHub → a new commit by `oif-bot` touching `data/opportunities.json`
2. Netlify → a new deploy triggered by that commit
3. Live site → the meta line shows a fresh `generated_at` timestamp

If 1 and 2 happened but the page looks stale, hard-refresh (`Ctrl+Shift+R`).

### D6. Let it run
Nothing more to do. The workflow runs daily at 13:00 UTC and does real work
every 20th day. The validator runs weekly on Mondays.

---

## Phase E — Work the review queue (ongoing)

Open `reports/review_queue.html` (or download it from the weekly Actions run
artifacts). Work top-down, worst confidence first.

| Finding | What to do |
|---|---|
| `search_url_as_listing` | Find the direct posting URL, or relabel the record as a monitored search source — the same URL is fine when labelled honestly |
| `term_passed` | Set status to archived. Keep the record. |
| `vague_employer` | Name the real employer, or delete the record |
| `bot_protected_source` | Replace with the employer's own posting URL if one exists |
| `never_verified` | Run with `--network`, or check the link yourself |

Re-run the validator afterwards to confirm the finding clears.

---

## Troubleshooting

**`ModuleNotFoundError: No module named 'requests'`**
The venv isn't active, or you skipped A5. Look for `(.venv)` in your prompt.

**`python: command not found`**
Try `python3`. On Windows, Python wasn't added to PATH — reinstall with that box ticked.

**Site loads but shows "Could not load listings"**
You opened index.html by double-clicking. Use `python -m http.server 8000`.

**Actions run is green but Netlify didn't deploy**
The repo isn't linked to Netlify. See Phase C1.

**Workflow fails: "Permission denied" on git push**
Check `permissions: contents: write` is present in the workflow file, and that
repo Settings → Actions → General → Workflow permissions is set to
"Read and write permissions".

**Everything suddenly shows 0 active listings**
The pipeline should have refused to publish (that's the 60% collapse guard).
If it did publish, run `python scripts/refresh.py --rollback`.
