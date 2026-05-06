# Fleet Commander

A Mac background agent that watches your screen for a week and tells you what to automate.

Not a dashboard. No charts. Just a frank advisor-style brief, like a technically fluent EA who watched your whole week and is giving you a debrief.

---

## Example output

```
Fleet Commander Brief — Week of May 5, 2026
Confidence: 9/10 — 7 days of data

You switched between Slack, Notion, and Linear an average of 9 times per
2-minute window on weekday mornings. That's not multitasking — that's context
destruction. Your longest uninterrupted focus block this week was 34 minutes
(Tuesday at 2pm). Everything else was under 20.

Every Monday at 9am you open Notion Sprint Planning, then Linear, then Chrome
— same sequence, 4 weeks running. That's 23 minutes of manual setup that an
agent could do in 90 seconds.

You recorded 3 Loom walkthroughs this week explaining the same onboarding flow.
Different recipients, same content. That's a doc that doesn't exist yet.

---

What to hand to an agent:

Sprint kickoff prep — Agent pulls open Linear issues, creates the Notion sprint
doc from a template, pre-fills the agenda with items from last week's backlog.
You show up and the doc is ready.

Weekly async update — You write a summary Loom every Friday. Agent drafts it
from your Linear closes and calendar. You record, not write.
```

---

## How it works

1. [ScreenPipe](https://github.com/mediar-ai/screenpipe) runs in the background, capturing app name + window title at 1 frame every 5 seconds. All data stays local — nothing leaves your machine except the pattern summary sent to Claude.
2. At the end of the week, you run `python brief.py`. It queries the local SQLite database, extracts behavioral patterns, and asks Claude to synthesize them into a brief.
3. You get a markdown file in `~/Documents/` and a machine-readable `fleet.json` alongside it.

**What it detects:**
- **Context-switch storms** — windows where you switched apps 5+ times in 2 minutes
- **Repeated manual sequences** — same app + window title, same day of the week, 3+ weeks running (these are your automation candidates)
- **Focus fragmentation** — your longest uninterrupted focus block per day

**Privacy:** Raw screen data never leaves your machine. Only the derived pattern summary (app names, window titles, frequency counts) is sent to the Claude API for synthesis.

---

## Requirements

**Mac:**
- Apple Silicon or Intel
- Python 3.8+
- [Homebrew](https://brew.sh)
- An [Anthropic API key](https://console.anthropic.com/)

**Windows:**
- Windows 10 or 11 (64-bit)
- Python 3.8+ from [python.org](https://www.python.org/downloads/) — during install, check **"Add Python to PATH"**
- [.NET 8.0 Runtime](https://dotnet.microsoft.com/en-us/download/dotnet/8.0) (required by ScreenPipe)
- An [Anthropic API key](https://console.anthropic.com/)

No pip dependencies — brief.py uses only the Python standard library.

---

## Setup — Mac

### 1. Install ScreenPipe

```bash
brew install screenpipe
```

### 2. Grant screen recording permission

ScreenPipe needs screen recording access. The permission must be granted to your **terminal app** (Terminal.app, iTerm2, Warp, etc.) — not to ScreenPipe itself.

**System Settings → Privacy & Security → Screen Recording**

If your terminal isn't listed, click **+** and add it from `/Applications/`.

### 3. Start ScreenPipe

```bash
screenpipe --disable-audio --fps 0.2 --disable-telemetry
```

Flags explained:
- `--disable-audio` — no microphone capture, screen only
- `--fps 0.2` — 1 frame every 5 seconds, low CPU overhead
- `--disable-telemetry` — no data sent to ScreenPipe servers

To run it in the background and keep it across sessions:

```bash
nohup screenpipe --disable-audio --fps 0.2 --disable-telemetry \
  > ~/.screenpipe/screenpipe.log 2>&1 &
```

Verify it's running:

```bash
curl localhost:3030/health
```

You should see `"status":"healthy"`.

### 4. Set your API key

```bash
export ANTHROPIC_API_KEY=your_key_here
```

Add it to `~/.zshrc` or `~/.bashrc` to make it permanent.

### 5. Let it run

ScreenPipe needs data to find patterns:

- **2 hours** → first useful brief (context-switch and focus data only)
- **3 days** → day-of-week trends start emerging
- **3 weeks** → repeated sequence patterns surface ("you do this every Monday")

The brief tells you its confidence level based on how many days of data it has.

---

## Setup — Windows

### 1. Install Python

Download Python 3.8+ from [python.org](https://www.python.org/downloads/). During install, check **"Add Python to PATH"** — it's unchecked by default and you need it.

Verify: open Command Prompt and run `python --version`.

### 2. Install .NET 8.0 Runtime

ScreenPipe requires the [.NET 8.0 Runtime](https://dotnet.microsoft.com/en-us/download/dotnet/8.0). Download and install the **x64** version.

### 3. Install ScreenPipe

ScreenPipe doesn't have a Windows installer — you download the CLI zip from GitHub Releases.

1. Go to [github.com/screenpipe/screenpipe/releases](https://github.com/screenpipe/screenpipe/releases)
2. Download the latest `screenpipe-*-x86_64-pc-windows-msvc.zip`
3. **Extract the full zip** to a folder like `C:\screenpipe\` — do not just extract the `.exe`. The DLLs alongside it are required.
4. Add that folder to your PATH: search "Edit the system environment variables" → Environment Variables → Path → New → `C:\screenpipe`

Verify: open a new Command Prompt and run `screenpipe --version`.

### 4. Grant screen capture permission

Windows 10/11 requires screen capture consent. Run ScreenPipe once manually so Windows prompts for permission:

```cmd
screenpipe --disable-audio --fps 0.2 --disable-telemetry
```

If you see a permission prompt, click **Allow**. You only need to do this once.

To run in the background (new Command Prompt window, keep open):

```cmd
start /b screenpipe --disable-audio --fps 0.2 --disable-telemetry
```

Verify it's running:

```cmd
curl localhost:3030/health
```

You should see `"status":"healthy"`.

### 5. Set your API key

Open **System Properties → Environment Variables → User variables → New**:
- Name: `ANTHROPIC_API_KEY`
- Value: `sk-ant-...` (your key, no quotes)

Click OK. Open a **new** Command Prompt for the variable to take effect.

### 6. Let it run

Same data accumulation timeline as Mac:

- **2 hours** → first useful brief (context-switch and focus data only)
- **3 days** → day-of-week trends start emerging
- **3 weeks** → repeated sequence patterns surface ("you do this every Monday")

---

## Usage

```bash
# Generate this week's brief
python brief.py

# See what patterns were extracted, without calling Claude (free)
python brief.py --dry-run

# Show the raw screen data behind each automation recommendation
python brief.py --explain

# Use only the last 3 days of data
python brief.py --days 3
```

Output is written to:
- `~/Documents/weekly-brief-YYYY-MM-DD.md` — the advisor brief
- `~/Documents/fleet-YYYY-MM-DD.json` — machine-readable pattern data

---

## Automatic weekly delivery

### Mac

**1. Edit the plist file** — open `com.fleetcommander.brief.plist` and replace:
- `INSTALL_PATH` with the full path to the folder containing `brief.py`
- `YOUR_API_KEY_HERE` with your Anthropic API key
- `YOURUSERNAME` with your Mac username (`whoami` in Terminal)
- The email fields if you want delivery to your inbox (or delete those lines to skip email)

For Gmail, `FLEET_SMTP_PASSWORD` must be an [App Password](https://myaccount.google.com/apppasswords), not your regular password.

**2. Install the scheduler:**
```bash
cp com.fleetcommander.brief.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.fleetcommander.brief.plist
```

**3. Test it fires correctly:**
```bash
launchctl start com.fleetcommander.brief
```

The brief will now run automatically every Friday at 5pm. Output logs to `~/.fleet-commander/fleet-commander.log`. You'll get a macOS notification when it's ready, plus an email if configured.

**To uninstall:**
```bash
launchctl unload ~/Library/LaunchAgents/com.fleetcommander.brief.plist
rm ~/Library/LaunchAgents/com.fleetcommander.brief.plist
```

### Windows

**1. Set `ANTHROPIC_API_KEY` as a system environment variable** (see Setup → Windows → Step 5 above). The task runs without a terminal, so it can't read shell exports.

**2. Edit `fleet-commander-task.xml`** and replace:
- `INSTALL_PATH` with the full path to the folder containing `brief.py` (e.g. `C:\Users\you\fleet-commander`)
- `PYTHON_PATH` with the full path to `python.exe` — run `where python` to find it
- `YOURUSERNAME` with your Windows username (`whoami` in Command Prompt)
- The email env vars in `fleet-commander-task.xml` are not supported — set `FLEET_EMAIL_*` variables via System Properties → Environment Variables instead if you want email delivery

**3. Install the task:**
```cmd
schtasks /create /xml fleet-commander-task.xml /tn FleetCommander
```

**4. Test it fires correctly:**
```cmd
schtasks /run /tn FleetCommander
```

The brief will run every Friday at 5pm. Output logs to `%USERPROFILE%\.fleet-commander\fleet-commander.log`.

**To uninstall:**
```cmd
schtasks /delete /tn FleetCommander /f
```

---

## Stopping ScreenPipe

**Mac:**
```bash
kill $(cat ~/.screenpipe/screenpipe.pid)
```

**Windows:**
```cmd
taskkill /im screenpipe.exe /f
```

Or just close the terminal window if you ran it in the foreground.

---

## Troubleshooting

**"The user declined TCCs for application, window, display capture"** (Mac)
ScreenPipe doesn't have screen recording permission. Go to System Settings → Privacy & Security → Screen Recording and enable your terminal app. Then re-run ScreenPipe.

**"Only N rows found — not enough data yet"**
ScreenPipe hasn't been running long enough. Wait at least 2 hours.

**"ScreenPipe schema mismatch"**
You upgraded ScreenPipe and the database schema changed. Run `sqlite3 ~/.screenpipe/db.sqlite .schema` (Mac) or `sqlite3 %USERPROFILE%\.screenpipe\db.sqlite .schema` (Windows) and update `REQUIRED_COLUMNS` in `brief.py`. The script is pinned to version `0.2.13` — check your version with `screenpipe --version`.

**DLL not found / screenpipe crashes immediately** (Windows)
You extracted only `screenpipe.exe` from the zip. Re-extract the full contents of the zip to the same folder — it includes `onnxruntime.dll`, `vcruntime140.dll`, and other required DLLs.

**`python` not found in Command Prompt** (Windows)
Python was installed without the "Add to PATH" option. Either reinstall Python from python.org and check that box, or find the python.exe path manually (`%LOCALAPPDATA%\Programs\Python\Python3XX\python.exe`) and use the full path.

**Brief feels generic / no strong patterns**
Window title data can be sparse (lots of "Slack", "Chrome"). This improves as ScreenPipe accumulates data and your specific window titles start repeating. The brief will tell you when the signal is weak rather than making things up.

---

## What's coming

- **Delta briefs** — week-over-week memory ("this got worse since you said you'd fix it")
- **Draft the actual automations** — not just name them, generate the Zapier/Make config or Claude agent spec
- **Team mode** — surface collective workflow patterns across a team
