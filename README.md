# BatteryLife.mobileconfig Monitor

This script checks Apple's BatteryLife.mobileconfig download, computes the file's
MD5 hash, compares it to the previous run, and sends a daily email update.

Because Apple requires login, the script uses Playwright with a saved browser
session:

1. Run once with `--login`
2. Sign in to your Apple developer account manually
3. Schedule the normal script run once per day

## Files

- `battery_life_monitor.py`: main monitor script
- `requirements.txt`: Python dependency list
- `.env.example`: SMTP environment variable template
- `monitor_data/`: created automatically for session/state/downloads

## Setup

1. Create a virtual environment and install dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
python3 -m playwright install chromium
```

2. Create your local env file:

```bash
cp .env.example .env
```

3. Export the variables before running, for example:

```bash
set -a
source .env
set +a
```

## First Login

Run:

```bash
python3 battery_life_monitor.py --login
```

The script opens a Chromium window. Sign in to Apple, complete 2FA if needed,
and wait for the `BatteryLife.mobileconfig` download to begin once. The session
is saved under `monitor_data/apple_profile/`.

## Daily Check

Run:

```bash
python3 battery_life_monitor.py
```

Test commands:

```bash
python3 battery_life_monitor.py -test -run
python3 battery_life_monitor.py -test -loginexpie
```

`-test -run` does one real download check and sends a test email without updating
`monitor_data/state.json`.

`-test -loginexpie` sends a simulated Apple-login-expired test email so you can
verify that alert format too.

Example email outcomes:

- First run: `New BatteryLife.mobileconfig detected`, current MD5 set, previous MD5 `None`
- Unchanged file: `No new BatteryLife.mobileconfig detected`
- Changed file: `New BatteryLife.mobileconfig detected`, current MD5 differs from previous MD5

The script always saves:

- the newest downloaded file to `monitor_data/BatteryLife.mobileconfig`
- an archived timestamped copy to `monitor_data/downloads/`
- MD5 state to `monitor_data/state.json`

## GitHub Actions Option

You can also run this on GitHub Actions instead of leaving your Mac on.

Important tradeoff:

- GitHub can run the script on a daily schedule
- but Apple login is still fragile, because the saved Apple session can expire
- when that happens, the workflow can email you that login expired, but you still need to refresh the Apple auth state from your Mac

Files already included for GitHub:

- `.github/workflows/battery_life_monitor.yml`: daily scheduled workflow
- `.gitignore`: keeps local secrets and Apple browser data out of git

### GitHub Secrets

Create these repository secrets in GitHub:

- `SMTP_HOST`
- `SMTP_PORT`
- `SMTP_USERNAME`
- `SMTP_PASSWORD`
- `SMTP_USE_TLS`
- `EMAIL_FROM`
- `EMAIL_TO`
- `APPLE_AUTH_STATE_BASE64`

### Export Apple Auth State

After you successfully run local login once, convert `monitor_data/auth_state.json` to base64 and save that value as the GitHub secret `APPLE_AUTH_STATE_BASE64`.

On macOS:

```bash
base64 -i monitor_data/auth_state.json | tr -d '\n'
```

Copy the output into the GitHub secret.

### State Persistence on GitHub

GitHub runners are temporary, so the workflow commits `monitor_data/state.json`
back to the repo after each run. That keeps the previous MD5 available for the
next scheduled run.

### Schedule

The included workflow runs every day at `8:00 AM` in `America/Chicago`.
You can change that in `.github/workflows/battery_life_monitor.yml`.

## Cron Example

This runs every day at 8:00 AM:

```cron
0 8 * * * cd /Users/zihangxu/Documents/BatteryLife-mobileconfig-profile-download && /bin/zsh -lc 'source .venv/bin/activate && set -a && source .env && set +a && python3 battery_life_monitor.py >> monitor.log 2>&1'
```

## Notes

- If Apple expires the session, rerun `python3 battery_life_monitor.py --login`
- For Gmail, use an app password instead of your normal account password
- The script uses MD5 because that's the change-detection behavior you asked for
