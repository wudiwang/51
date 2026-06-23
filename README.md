# Telegram Lark Automation

Sync Telegram group messages to a Lark Sheet and run optional Telegram account keepalive jobs.

## What Goes Into Git

- Python scripts under `scripts/`
- Tests
- Example configs under `config/`
- systemd service and timer templates

## What Must Stay Out Of Git

- `*.session`
- `*.session-journal`
- `*.config.json`
- `.env`
- Lark CLI auth files
- logs and exported message data

## Server Layout

Recommended path:

```bash
/opt/telegram-lark
  scripts/
  config/
  systemd/
  secrets/
  .venv/
```

Real configs and Telegram session files should live under:

```bash
/opt/telegram-lark/secrets
```

## Install On Ubuntu

```bash
sudo apt update
sudo apt install -y git python3 python3-venv python3-pip npm
sudo npm install -g @larksuiteoapi/lark-cli

sudo mkdir -p /opt/telegram-lark
sudo chown -R "$USER":"$USER" /opt/telegram-lark
git clone <YOUR_GIT_REPO_URL> /opt/telegram-lark

cd /opt/telegram-lark
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
mkdir -p secrets logs
```

Copy `config/telegram_lark_sync.config.example.json` to:

```bash
secrets/telegram_lark_sync.config.json
```

Then edit real values on the server only.

## Telegram Login

```bash
cd /opt/telegram-lark
. .venv/bin/activate
python scripts/telegram_lark_sync.py --config secrets/telegram_lark_sync.config.json request-code
python scripts/telegram_lark_sync.py --config secrets/telegram_lark_sync.config.json login-code --code <CODE>
```

## Lark Login

Run on the server:

```bash
lark-cli auth login
```

Follow the QR/browser authorization flow with the Lark account that can edit the target Sheet.

## Backfill Test

```bash
python scripts/telegram_lark_sync.py --config secrets/telegram_lark_sync.config.json backfill --days 1 --batch-size 100
```

## Enable Timers

```bash
sudo cp systemd/telegram-lark-backfill.service /etc/systemd/system/
sudo cp systemd/telegram-lark-backfill.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now telegram-lark-backfill.timer
systemctl list-timers telegram-lark-backfill.timer
```

Optional secondary account keepalive:

```bash
sudo cp systemd/telegram-keepalive-alt.service /etc/systemd/system/
sudo cp systemd/telegram-keepalive-alt.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now telegram-keepalive-alt.timer
```

## Discussion Intake To Lark Base

The intake job scans configured Telegram groups every 5 minutes, creates
Lark Base records for clearly discussed demands or online issues, updates
matching records when messages indicate completion/testing, and sends a
Telegram bot notification when it writes data.

Copy `config/telegram_lark_intake.config.example.json` to:

```bash
secrets/telegram_lark_intake.config.json
```

Use the existing Telegram sync config/session by setting:

```json
{
  "telegram_config_path": "secrets/telegram_lark_sync.config.json"
}
```

Enable the timer:

```bash
sudo cp systemd/telegram-lark-intake.service /etc/systemd/system/
sudo cp systemd/telegram-lark-intake.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now telegram-lark-intake.timer
systemctl list-timers telegram-lark-intake.timer
```
