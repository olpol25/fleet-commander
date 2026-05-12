# Fleet Commander — Installation Guide for AI Agents

Read this entire file, then follow the steps. Ask the user for their Anthropic API key when needed.
Target: ~5 minutes to a running install.

Mac only. Windows instructions are in README.md.

---

## Step 0: Prerequisites

```bash
python3 --version          # need 3.8+
brew --version             # need Homebrew
curl localhost:3030/health # check if ScreenPipe already running
```

If Python < 3.8: `brew install python3`
If Homebrew missing: direct the user to https://brew.sh — do not install it for them.
If ScreenPipe is already healthy, skip Step 1 and Step 2.

---

## Step 1: Install ScreenPipe

```bash
brew install screenpipe
screenpipe --version
```

Verify the version string prints. If install fails, check Homebrew is up to date:
`brew update && brew install screenpipe`

---

## Step 2: Grant screen recording permission

**This step requires user action — you cannot automate it.**

Tell the user:

> ScreenPipe needs Screen Recording permission for your terminal app.
> Go to: System Settings → Privacy & Security → Screen Recording
> Enable your terminal (Terminal.app, iTerm2, Warp, etc.).
> If it's not listed, click + and add it from /Applications/.
> Once enabled, come back here.

After they confirm, proceed.

---

## Step 3: Start ScreenPipe with auto-restart

Create a launchd plist so ScreenPipe starts at login and restarts if it crashes:

```bash
mkdir -p ~/.screenpipe

cat > ~/Library/LaunchAgents/com.screenpipe.plist << 'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>com.screenpipe</string>
    <key>ProgramArguments</key>
    <array>
        <string>/opt/homebrew/bin/screenpipe</string>
        <string>--disable-audio</string>
        <string>--fps</string><string>0.2</string>
        <string>--disable-telemetry</string>
    </array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key>
    <string>/Users/YOURUSERNAME/.screenpipe/screenpipe.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/YOURUSERNAME/.screenpipe/screenpipe.log</string>
</dict>
</plist>
EOF
```

Replace `YOURUSERNAME` with the actual username:

```bash
USERNAME=$(whoami)
sed -i '' "s/YOURUSERNAME/$USERNAME/g" ~/Library/LaunchAgents/com.screenpipe.plist
launchctl load ~/Library/LaunchAgents/com.screenpipe.plist
launchctl start com.screenpipe
```

Verify it's running (may take 3-5 seconds to start):

```bash
sleep 5 && curl -s localhost:3030/health
```

You should see `"status":"healthy"`. If not, check the log:
`tail -20 ~/.screenpipe/screenpipe.log`

Common failure: ScreenPipe binary is at `/usr/local/bin/screenpipe` on Intel Macs, not `/opt/homebrew/bin/screenpipe`. Fix:

```bash
PIPE_BIN=$(which screenpipe)
sed -i '' "s|/opt/homebrew/bin/screenpipe|$PIPE_BIN|g" ~/Library/LaunchAgents/com.screenpipe.plist
launchctl unload ~/Library/LaunchAgents/com.screenpipe.plist
launchctl load ~/Library/LaunchAgents/com.screenpipe.plist
launchctl start com.screenpipe
```

---

## Step 4: Get Fleet Commander

```bash
git clone https://github.com/olpol25/fleet-commander.git ~/fleet-commander
cd ~/fleet-commander
```

No pip install needed — brief.py uses only the Python standard library.

---

## Step 5: Set the API key

Ask the user for their Anthropic API key (from https://console.anthropic.com/).

```bash
# Add to shell profile so it persists
echo 'export ANTHROPIC_API_KEY=sk-ant-YOUR_KEY_HERE' >> ~/.zshrc
export ANTHROPIC_API_KEY=sk-ant-YOUR_KEY_HERE
```

Replace `sk-ant-YOUR_KEY_HERE` with the actual key the user provides.

---

## Step 6: Verify

```bash
cd ~/fleet-commander
python3 brief.py --dry-run
```

A healthy dry-run prints JSON with these keys:
- `days_of_data` — how many days ScreenPipe has captured
- `transition_cost_events` — context-switch patterns
- `repeated_sequences` — automation candidates (empty until 3+ weeks of data)
- `focus_fragmentation` — longest focus block per day

If `days_of_data` is 0 or `transition_cost_events` is empty: ScreenPipe is running but hasn't captured enough yet. Wait 30 minutes, then re-run `--dry-run`. If still empty, the screen recording permission wasn't granted correctly — go back to Step 2.

If you see an error about ScreenPipe schema: `screenpipe --version` and compare to the version pinned in `brief.py` (search `REQUIRED_COLUMNS`).

---

## Step 7: Generate the brief

Once `days_of_data >= 1`:

```bash
cd ~/fleet-commander
python3 brief.py
```

Output is written to:
- `~/Documents/weekly-brief-YYYY-MM-DD.md` — the advisor brief (open this)
- `~/Documents/fleet-YYYY-MM-DD.json` — machine-readable pattern data

The brief is most useful after 3+ days of data. After 3 weeks, repeated-sequence automation candidates start surfacing.

---

## Useful commands

```bash
python3 brief.py               # generate this week's brief
python3 brief.py --dry-run     # see extracted patterns, no API call (free)
python3 brief.py --explain     # show raw screen data behind each recommendation
python3 brief.py --days 3      # use only the last 3 days
```

---

## Uninstall

```bash
launchctl unload ~/Library/LaunchAgents/com.screenpipe.plist
rm ~/Library/LaunchAgents/com.screenpipe.plist
pkill -f "screenpipe --disable-audio"
rm -rf ~/fleet-commander
```

ScreenPipe data stays in `~/.screenpipe/` — delete that too if you want a clean removal.
