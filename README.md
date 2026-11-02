# Collaborative Pixel Canvas — Setup and Run Guide

This guide describes the current prototype and how a teammate can run it on another Windows, macOS, or Linux computer.

> **Important:** These instructions assume the repository has `frontend/main.py` and `frontend/index.html`, because that is the structure used successfully during the initial prototype. If your local structure differs, adjust the paths and the Uvicorn import path accordingly.

## 1. Current status and scope

The current Phase 1 prototype uses:
- Python, FastAPI, and Uvicorn
- HTML/CSS/JavaScript and the browser Canvas API
- HTTP for drawing/state/clear requests
- WebSockets for live updates between browsers connected to the same backend process

The canvas supports server-issued HTTP-only browser sessions, live updates, and user-specific clear. Pixels retain the session that last drew them, so a clear preserves pixels another session has overwritten. Focused tests cover these behaviors.

Session cookies use `HttpOnly` and `SameSite=Lax`. `Secure` is disabled for the local HTTP development server. HTTP requests and WebSockets validate the same cookie against the in-memory session registry; refreshing the page retains the browser cookie. WebSocket snapshots, canvas changes, and their broadcasts share one async lock. The page uses the WebSocket snapshot as its initial render source to avoid applying an older parallel state response after newer events.

**Not implemented yet:** Docker shard processes, tile migration, durable SQLite logs, snapshots, replay, operation deduplication, ownership epochs, undo, and crash recovery. Canvas and session state are still in memory and are lost on restart. Do not treat this prototype as the completed distributed system.

## 2. Requirements

- Python 3.11 or newer recommended (the initial development environment used Python 3.12)
- A modern browser (Chrome, Edge, Firefox, etc.)
- VS Code or another editor (optional but recommended)
- Internet access during initial dependency installation
- Git (recommended for sharing changes with your teammate)

Install the packages listed in `requirements.txt`: `fastapi`, `uvicorn[standard]`, `httpx`, `pytest`, and `pytest-asyncio`.

## 3. Project structure

Expected current structure:

```text
Collaborative Pixel Canvas/
├── .venv/                  # local only; do not commit/share this folder
├── frontend/
│   ├── main.py
│   └── index.html
├── requirements.txt
├── README.md
└── decision.md
```

Your actual project may contain additional folders. Keep the `main.py` and `index.html` locations consistent with the paths used by the code.

## 4. Install dependencies

Install the project dependencies from its existing root `requirements.txt`:

```powershell
python -m pip install -r requirements.txt
```

## 5. Windows setup (PowerShell)

Open a terminal in the project root:

```powershell
py --version
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

If PowerShell blocks activation, you can use Command Prompt instead (see below), or run the virtual environment's Python directly without activating it.

### Windows Command Prompt alternative

```bat
py --version
py -m venv .venv
.venv\Scripts\activate.bat
python -m pip install -r requirements.txt
```

### Run the application

Because the working backend is in `frontend/main.py`, run Uvicorn from the project root with:

```powershell
python -m uvicorn frontend.main:app --reload
```

If `frontend` is not a Python package in your setup or this import path fails, use the confirmed working method:

```powershell
cd frontend
python -m uvicorn main:app --reload
```

Then open `http://127.0.0.1:8000/`.

## 6. macOS / Linux setup

From the project root:

```bash
python3 --version
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Run from the project root:

```bash
python -m uvicorn frontend.main:app --reload
```

If that import path fails, run from the `frontend` directory instead:

```bash
cd frontend
python -m uvicorn main:app --reload
```

Open `http://127.0.0.1:8000/`.

## 7. Verify it works

1. Open `http://127.0.0.1:8000/`.
2. Draw a few pixels and change colours.
3. Open the same URL in a second browser tab/window.
4. Draw in one browser and confirm the other receives the update.
5. Draw in both sessions, then clear one and confirm the other session's pixels remain.
6. Run the automated tests with `python -m pytest -q tests -p no:cacheprovider`.
7. Open `http://127.0.0.1:8000/docs` to inspect HTTP API endpoints.
8. In the terminal, successful requests should generally show `200 OK`; WebSocket connections should remain open.

If the server says `Could not import module "main"`, check the current directory and module path. For `frontend/main.py`, either run `uvicorn main:app` from inside `frontend/`, or run `uvicorn frontend.main:app` from the project root if that import path is supported.

## 8. Common Windows path issue

Paths containing spaces must be quoted when used as standalone executable paths. Prefer these commands after activating the virtual environment:

```powershell
python -m pip install -r requirements.txt
python -m uvicorn main:app --reload
```

Do not type an unquoted full path like:

```text
D:\Desktop\IIIT HYD\...\python.exe
```

into Command Prompt as if it had no spaces. If you need the full path, quote it.

## 9. Share the project with your teammate

### Recommended: Git

1. Make sure `requirements.txt`, `README.md`, `decision.md`, and the source files are saved.
2. Add a `.gitignore` file containing:

```gitignore
.venv/
__pycache__/
*.py[cod]
.env
data/
*.db
*.sqlite
*.sqlite3
```

3. Commit and push the source files to a private/shared repository.
4. Your teammate clones the repository, creates her own `.venv`, installs `requirements.txt`, and runs the server using the commands above.

Do **not** share the `.venv` directory; virtual environments contain machine-specific paths and executables. Each teammate should create their own environment.

### If sharing a ZIP

Zip the source files and documentation, but exclude `.venv/`, caches, and local database/data directories. The teammate extracts the ZIP and follows the setup instructions.

## 10. Troubleshooting

### `python` or `py` is not recognized
Install Python and ensure it is available on PATH, then reopen the terminal. On Windows, `py --version` may work even if `python --version` does not.

### `ModuleNotFoundError: No module named 'fastapi'`
Activate the project's `.venv` and run:

```powershell
python -m pip install -r requirements.txt
```

### `Could not import module "main"`
Ensure the terminal is in `frontend/` before running `python -m uvicorn main:app --reload`, or run from the root with `python -m uvicorn frontend.main:app --reload`.

### Port 8000 is already in use
Stop the other server with `Ctrl+C`, or use another port:

```powershell
python -m uvicorn main:app --reload --port 8001
```

If using port 8001, open `http://127.0.0.1:8001/`.

### WebSocket connection fails
Check the browser console and terminal. Confirm the frontend connects to `/ws` on the same host and port serving the page, and that the WebSocket endpoint exists in `main.py`.

### Drawing works but another browser does not update
Check that both browser windows use the same URL and backend process. The current in-memory WebSocket manager broadcasts only within one backend process.

## 11. Development workflow

- Make one change at a time.
- Test it in two browser windows.
- Keep API payloads and WebSocket message types in sync between backend and frontend.
- Update `decision.md` when a design decision changes.
- Add tests before implementing migration and recovery.
- Never claim migration/recovery correctness until the corresponding failure tests pass.
