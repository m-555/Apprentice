# Try it yourself — a beginner's walkthrough (local model only, free)

Everything below runs on your own machine with the free local model. No cloud account, no
API key, no spending. Follow it top to bottom the first time; each step tells you what
"working" looks like.

Windows paths are used throughout (that's this machine). On macOS/Linux swap
`E:\projects\qwen-pipeline\.venv\Scripts\python.exe` for `.venv/bin/python`.

---

## Step 0 — start the model server (do this first, every time)

Apprentice talks to **Ollama**, which must be running.

```powershell
ollama serve
```

Leave that window open. Check it answers:

```powershell
curl http://127.0.0.1:11434/api/version
```

> **If that fails with "actively refused":** Windows has reserved the port. This has
> already happened twice on this machine (11434, then 11800). Pick a free high port and
> use it consistently:
> ```powershell
> $env:OLLAMA_HOST="127.0.0.1:21434"
> ollama serve
> ```
> then set the same address in `config\qwen.local.json`:
> ```json
> { "runner": { "host": "http://127.0.0.1:21434" } }
> ```
> **Your config currently points at 11800, which is dead — fix this before anything else.**

Check the rest of your setup in one command:

```powershell
E:\projects\qwen-pipeline\.venv\Scripts\python.exe E:\projects\qwen-pipeline\src\cli.py doctor
```

You want: `ollama : Ollama reachable …` and `providers : enabled = qwen`.

---

## Step 1 — make a toy project to experiment on

Never learn a new agent on code you care about. Two minutes:

```powershell
mkdir C:\temp\agent-demo
cd C:\temp\agent-demo
```

Create `calc.py`:

```python
"""Small calculator helpers."""


def add(a: float, b: float) -> float:
    """Return the sum of a and b."""
    return a + b
```

Create `check.py` — this is the **test** the agent must satisfy:

```python
from calc import add, mul

assert add(1, 2) == 3
assert mul(3, 4) == 12
print("all tests passed")
```

Create `.qwen-pipeline.json` — your project's rules and test command:

```json
{
  "conventions": "Every function needs full type hints and a one-line docstring.",
  "test_cmd": "E:\\projects\\qwen-pipeline\\.venv\\Scripts\\python.exe -B check.py"
}
```

Make it a git repo (the agent refuses a non-git folder — git is your undo):

```powershell
git init
git add -A
git commit -m "start"
```

---

## Step 2 — your first chat

```powershell
E:\projects\qwen-pipeline\.venv\Scripts\python.exe E:\projects\qwen-pipeline\src\cli.py chat --repo .
```

At the `you >` prompt type:

```
Add a mul(a, b) function to calc.py that multiplies two numbers.
```

**What you should see:** the reply appearing word by word (that's streaming), then
`-> read_file(calc.py)`, `-> edit_file(calc.py)`, `-> run_tests()`, and finally
`[OK] verified (tests)`.

Then check the work yourself — this is the habit worth building:

```powershell
git diff
```

Type `/quit` to leave. Everything is saved; `apprentice sessions` lists past runs.

---

## Step 3 — see verification actually save you

This is the whole point of Apprentice, so watch it work. Ask for something wrong on purpose:

```
Change mul so it returns a + b instead of a * b.
```

The agent will make the change, `run_tests` will fail, and you'll see:

```
[FAILED] verification (tests) - change REVERTED, agent retrying
```

Then confirm with `git diff` that **`calc.py` is untouched**. A broken change never
survives a turn. (Compare: `/verify off` turns this protection off — try it once so you
can see the difference, then switch back with `/verify tests`.)

---

## Step 4 — plan mode (look before it leaps)

Start with `--plan`:

```powershell
... src\cli.py chat --repo . --plan
```

Ask for something bigger:

```
Add divide(a, b) that raises ValueError when b is zero, and cover it in the tests.
```

The agent can only **read** in this phase — editing tools are physically removed, not just
discouraged. It investigates, then prints a numbered plan and asks
`Execute this plan?`. Answer `n` the first time and verify with `git status` that nothing
changed. Run it again and answer `y` to let it proceed.

---

## Step 5 — the commands worth knowing

Inside a chat:

| Type this | What happens |
|---|---|
| `/undo` | reverts the agent's last completed turn |
| `/verify off` \| `gate` \| `tests` | change how strict the checking is |
| `/plan` | turn plan-first mode on (`/plan off` to disable) |
| `/cost` | tokens used and estimated spend (`$0` on local) |
| `/files` | what's been changed this session |
| `/help`, `/quit` | the rest |

---

## Step 6 — unattended mode

Give it a goal and a finish line, then walk away:

```powershell
... src\cli.py run "Add a power(a, b) function to calc.py" --done-when "E:\projects\qwen-pipeline\.venv\Scripts\python.exe -B check.py" --repo .
```

It loops until the command exits 0 (or gives up) and prints
`done_passed=True rounds=1 files_changed=['calc.py']`.

---

## Step 7 — in VS Code

```powershell
cd E:\projects\qwen-pipeline\vscode-extension
npm install
npm run package
code --install-extension apprentice-vscode-0.1.0.vsix --force
```

Open `C:\temp\agent-demo` in VS Code and press **Ctrl+Shift+A**.

First run only, tell the extension where Apprentice lives — File → Preferences →
Settings → search "apprentice":

- **Python Path**: `E:\projects\qwen-pipeline\.venv\Scripts\python.exe`
- **Repo Path**: `E:\projects\qwen-pipeline`

Then type a request in the panel. You get the same agent with tool rows you can expand,
green/red verification badges, and clickable changed files that open a diff. Run
**Apprentice: Check Setup (doctor)** from the Command Palette if anything looks wrong.

---

## When something goes wrong

| Symptom | Cause / fix |
|---|---|
| "Could not reach Ollama" | `ollama serve` isn't running, or the port in your config is wrong (see Step 0) |
| "not a git repository" | `git init` in the folder — git is the agent's undo |
| "working tree has uncommitted changes" | Commit or stash first, so you can tell your edits from the agent's |
| Agent keeps failing the same way | It's a weak model — after 3 identical attempts it gets told to change approach; if it still fails, take the task yourself or split it smaller |
| The panel is silent | **Apprentice: Show Log** in the Command Palette shows the raw process output |

## What to expect from a free local model

It's genuinely weaker than a frontier model. Small, well-specified tasks ("add this
function", "fix this test") work well. Vague or sprawling ones ("refactor my
architecture") do not — it loses the thread. That's not a bug you can configure away; it's
why verification exists. Give it small jobs with a real test, and check `git diff` before
you commit.
