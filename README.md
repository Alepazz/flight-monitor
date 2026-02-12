# Flight Monitor

Automatic flight price monitor using **Google Flights**. No API key needed — uses Playwright to scrape prices directly.

Get email, Telegram, and macOS notifications when prices drop below your threshold.

## Features

- Monitors round-trip flights across multiple origin airports
- Configurable date ranges, trip duration, and price thresholds
- Multi-channel alerts: **email** (HTML), **Telegram**, **macOS notifications**
- Direct Google Flights booking links in every alert
- Price history tracking (JSONL)
- Runs automatically via macOS LaunchAgent (or cron/systemd)

## Requirements

- Python 3.8+
- macOS (for LaunchAgent auto-scheduling; Linux works with cron)
- A Gmail account with an [App Password](#gmail-app-password)

## Quick Start

```bash
git clone https://github.com/YOUR_USERNAME/flight-monitor.git
cd flight-monitor
./setup.sh
```

The setup script will:
1. Create `config.json` from the template and ask for your email/password
2. Install Python dependencies in a virtual environment
3. Configure automatic execution via macOS LaunchAgent

## Configuration

All settings are in `config.json` (created from `config.example.json` on first run).

| Field | Description | Example |
|---|---|---|
| `origins` | List of departure airport IATA codes | `["MXP", "LIN", "BGY"]` |
| `destination` | Destination airport IATA code | `"MLE"` |
| `date_from` | Start of travel date range | `"2027-01-01"` |
| `date_to` | End of travel date range | `"2027-02-28"` |
| `nights_min` | Minimum trip duration (nights) | `7` |
| `nights_max` | Maximum trip duration (nights) | `10` |
| `adults` | Number of adult passengers | `2` |
| `price_threshold_pp` | Alert threshold (per person, round-trip) | `700` |
| `max_stops` | Maximum number of stops | `1` |
| `sample_every_n_days` | Sample departure dates every N days | `5` |
| `delay_between_searches` | Seconds between searches (avoid rate limits) | `4` |
| `email_to` | Recipient email address | `"you@gmail.com"` |
| `email_from` | Sender Gmail address | `"you@gmail.com"` |
| `email_cc` | CC email (optional) | `""` |
| `email_app_password` | Gmail App Password | see below |
| `telegram_bot_token` | Telegram bot token (optional) | `""` |
| `telegram_chat_id` | Telegram chat ID (optional) | `""` |
| `check_interval_hours` | Hours between automatic checks | `12` |

### Environment Variables

Secrets can also be set via environment variables (they override `config.json`):

| Variable | Overrides |
|---|---|
| `FLIGHT_EMAIL_TO` | `email_to` |
| `FLIGHT_EMAIL_FROM` | `email_from` |
| `FLIGHT_EMAIL_CC` | `email_cc` |
| `FLIGHT_EMAIL_PASSWORD` | `email_app_password` |
| `FLIGHT_TELEGRAM_TOKEN` | `telegram_bot_token` |
| `FLIGHT_TELEGRAM_CHAT_ID` | `telegram_chat_id` |

## Gmail App Password

Google requires an **App Password** for SMTP access (regular password won't work):

1. Go to [myaccount.google.com](https://myaccount.google.com)
2. **Security** → **2-Step Verification** (enable it if not already)
3. **Security** → **App passwords**
4. Create a new app password (select "Mail" or "Other")
5. Copy the 16-character password into `config.json` → `email_app_password`

## Telegram Notifications (Optional)

1. Open Telegram, search for **@BotFather**
2. Send `/newbot`, follow the prompts, copy the **bot token**
3. Search for **@userinfobot**, send `/start` to get your **chat ID**
4. Add both values to `config.json`

## Useful Commands

```bash
# Manual test run
./venv/bin/python3 ./monitor.py

# View live logs
tail -f ./monitor.log

# View price history
cat ./price_history.jsonl

# View found deals
cat ./deals.txt

# Stop the monitor (macOS)
launchctl unload ~/Library/LaunchAgents/com.flightmonitor.plist

# Restart the monitor (macOS)
launchctl unload ~/Library/LaunchAgents/com.flightmonitor.plist && \
launchctl load ~/Library/LaunchAgents/com.flightmonitor.plist
```

## How It Works

1. Generates departure/return date combinations based on your config
2. Searches outbound flights on Google Flights (gets round-trip prices)
3. Searches return flight details separately
4. Filters by max stops, removes duplicates, sorts by price
5. If any flight is below the threshold: sends email + Telegram + macOS notification
6. Saves all results to `price_history.jsonl` and deals to `deals.txt`

## License

MIT
