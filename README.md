# 📅 GrafikBot — Kadromierz → Google Calendar

This project is meant to help users add thair Kadromierz schedule to google calendar. Sadly there isn't available Kadromierz API to easily do this, so to work around this issue the integration requires a bit manual work. It's available as a **Discord bot** (multi-user, runs on a server) or a **standalone script** (runs on your own PC).

---

## Table of Contents

- [Which version should I use?](#which-version-should-i-use)
- [Prerequisites](#prerequisites)
- [Step 1 — Google Cloud Setup](#step-1--google-cloud-setup-required-for-both)
- [Command-line script](#command-line-script)
- [Discord bot](#discord-bot)
- [Settings reference](#settings-reference)
- [Troubleshooting](#troubleshooting)

---

## Which version should I use?

| | Command-line script | Discord bot |
|---|---|---|
| **Best for** | Personal use on your own PC | Shared use with a team |
| **Requires** | Python on your machine | A server to host the bot |
| **Settings** | `.env` file | Bot commands (per user) |
| **How to import** | `python kalendarz.py schedule.pdf` | Drop the PDF in a Discord channel |

---

## Prerequisites

Before you start, make sure you have:

1. A **Kadromierz schedule exported as a PDF** (see below)
2. **Python 3.10 or newer** — download from [python.org](https://www.python.org/downloads/)
3. A **Google account** with the ability to enable the Google Calendar API

### How to export your schedule from Kadromierz

1. Log in to Kadromierz and go to the **Grafik** (Schedule) view.
2. Navigate to the month you want to export.
3. Click **Opcje** (Options) in the top toolbar, then select **Eksportuj/Drukuj** (Export/Print).
4. In the dropdown that appears, choose **Pobierz plik** (Download file).
5. A dialog will open — select **PDF** as the file format.
6. Click **Pobierz** (Download) to save the file to your computer.

> 💡 You need sufficient permissions in Kadromierz to export the schedule. If the export option is greyed out or missing, ask your manager to export it for you.

---

## Step 1 — Google Cloud Setup (required for both)

Both versions need a `credentials.json` file from Google. This is a **one-time setup** that takes about 5 minutes.

### 1.1 Create a Google Cloud project

1. Go to [console.cloud.google.com](https://console.cloud.google.com) and sign in.
2. Click the project dropdown at the top → **New Project**.
3. Give it any name (e.g. `GrafikBot`) and click **Create**.

### 1.2 Enable the Google Calendar API

1. In the left menu go to **APIs & Services → Library**.
2. Search for **Google Calendar API** and click it.
3. Click **Enable**.

### 1.3 Create OAuth credentials

1. Go to **APIs & Services → Credentials**.
2. Click **+ Create Credentials → OAuth client ID**.
3. If prompted to configure the consent screen first:
   - Choose **External** and click **Create**.
   - Fill in an app name (anything works, e.g. `GrafikBot`), your email address, and click **Save and Continue** through all steps.
   - On the **Test users** step, add your own Google email address, then click **Save and Continue**.
4. Back on the **Create OAuth client ID** screen:
   - Application type: **Desktop app** *(for the command-line script)* or **Web application** *(for the Discord bot)*
   - Click **Create**.
5. Click **Download JSON** and save the file as `credentials.json` in the same folder as the script or bot.

> ⚠️ Keep `credentials.json` private — never share it or commit it to Git.

---

## Command-line script

### Install dependencies

**macOS / Linux:**
```bash
pip install pdfplumber google-auth google-auth-oauthlib google-api-python-client python-dotenv
```

**Windows:**
```cmd
py -m pip install pdfplumber google-auth google-auth-oauthlib google-api-python-client python-dotenv
```

### Configure settings

1. Copy the example settings file:

   **macOS / Linux:**
   ```bash
   cp .env.example .env
   ```
   **Windows:**
   ```cmd
   copy .env.example .env
   ```

2. Open `.env` in any text editor and adjust the values if needed. The defaults work for most people — see the [Settings reference](#settings-reference) for a full description of each option.

3. Place your `credentials.json` in the same folder as `kalendarz.py`.

### First run — Google login

The first time you run the script, a browser window will open asking you to sign in with Google and grant calendar access. After you approve, a `token.json` file is saved automatically — you won't need to log in again unless you delete that file.

### Usage

> **Windows users:** replace `python` with `py` in all commands below.

```bash
# Import a schedule PDF into your calendar
python kalendarz.py schedule.pdf

# Preview what would be added without touching your calendar
python kalendarz.py schedule.pdf --dry-run

# List all your Google Calendars and their IDs
python kalendarz.py --list-calendars

# Show the current settings loaded from .env
python kalendarz.py --settings
```

**Tip:** Always do a `--dry-run` first when importing a new PDF to make sure everything looks correct before it's added to your calendar.

### Changing settings

Open `.env` in a text editor and save your changes. The next run picks them up automatically.

---

## Discord bot

### Requirements

- Python 3.10 or newer
- A Discord account with permission to add bots to a server
- A publicly reachable server to host the bot (VPS, cloud VM, Raspberry Pi, etc.)

### Install dependencies

```bash
pip install discord.py pdfplumber google-auth google-auth-oauthlib \
    google-api-python-client aiohttp sqlalchemy python-json-logger
```

### Setup

#### 1. Create the Discord bot

1. Go to [discord.com/developers/applications](https://discord.com/developers/applications) and click **New Application**.
2. Give it a name and click **Create**.
3. Go to the **Bot** tab and click **Add Bot**.
4. Under **Token**, click **Reset Token**, copy the token, and save it somewhere safe.
5. Scroll down to **Privileged Gateway Intents** and enable **Message Content Intent**.
6. Go to **OAuth2 → URL Generator**:
   - Scopes: `bot`, `applications.commands`
   - Bot permissions: `Send Messages`, `Read Message History`, `Attach Files`
7. Copy the generated URL, open it in your browser, and invite the bot to your server.

#### 2. Set environment variables

On the machine running the bot, set the following variables (or add them to a `.env` file):

| Variable | Description |
|---|---|
| `DISCORD_TOKEN` | The bot token you copied in step 4 above |
| `PUBLIC_BASE_URL` | The public URL of your server, e.g. `https://myserver.com` |
| `OAUTH_PORT` | Port for the OAuth callback server (default: `8080`) |
| `DATABASE_URL` | Database connection string (default: `sqlite:///tokens.db`) |

#### 3. Add credentials.json

Place `credentials.json` in the same folder as `bot.py`. Use the **Web application** credential type (from [Step 1.3](#13-create-oauth-credentials)), and set the redirect URI to:

```
https://your-public-url/oauth/callback
```

#### 4. Run the bot

```bash
python bot.py
```

### Connecting your Google Calendar

Each user on the server connects their own Google Calendar independently — the bot never shares one account across users.

1. Type `/login` in any channel.
2. The bot sends you a **private message** with a login link.
3. Open the link, sign in with Google, and approve calendar access.
4. The bot confirms in the channel — you're connected.

| Command | What it does |
|---|---|
| `/status` | Check whether your Google account is connected |
| `/logout` | Disconnect your Google account |

### Importing a schedule

Drop a PDF file into any Discord channel where the bot is active. It will automatically detect the file, parse the schedule, and add the shifts to your connected Google Calendar.

### All bot commands

| Command | Description |
|---|---|
| `/login` | Connect your Google Calendar |
| `/logout` | Disconnect your Google Calendar |
| `/status` | Check your connection status |
| `/settings` | View your current settings (sent as a DM) |
| `/setreminder <minutes>` | Reminder before each shift — e.g. `/setreminder 15`. Use `0` to disable. |
| `/setcolor <id>` | Event color in Google Calendar (1–11, see color table below) |
| `/setlocation <place>` | Work location shown in each calendar event |
| `/setcalendar` | Choose which of your Google Calendars to use |
| `/users` *(admin)* | List all users with connected accounts |
| `/removeuser <user>` *(admin)* | Disconnect a specific user |

---

## Settings reference

### Event colors

| ID | Color | | ID | Color |
|---|---|---|---|---|
| 1 | 🔵 Lavender | | 7 | 🩵 Peacock *(default)* |
| 2 | 💚 Sage | | 8 | 🫐 Blueberry |
| 3 | 🫐 Grape | | 9 | 🫐 Blueberry (dark) |
| 4 | 🩷 Flamingo | | 10 | 🌿 Basil |
| 5 | 🍌 Banana | | 11 | 🍅 Tomato |
| 6 | 🍊 Tangerine | | | |

### `.env` options (command-line script)

| Key | Default | Description |
|---|---|---|
| `CALENDAR_ID` | `primary` | Which calendar to add events to. Use `--list-calendars` to find available IDs. |
| `REMINDER_MINUTES` | `30` | Minutes before each event for a popup reminder. Set to `0` to disable. |
| `EVENT_COLOR` | `7` | Event color ID (1–11, see table above). |
| `LOCATION` | `PP Wrocław Świdnicka` | Location text shown inside each calendar event. |
| `CREDENTIALS_FILE` | `credentials.json` | Path to your Google OAuth credentials file. |
| `TOKEN_FILE` | `token.json` | Path where the login token is cached after first login. |

---

## Troubleshooting

**`credentials.json` not found**
The file must be in the same folder as the script and named exactly `credentials.json`. Download it from Google Cloud Console → APIs & Services → Credentials → your OAuth client → **Download JSON**.

**Browser doesn't open during login**
The script also prints the login URL in the terminal — copy and paste it into your browser manually.

**"Access blocked: app not verified"**
Your OAuth app is in testing mode. Go to Google Cloud Console → APIs & Services → OAuth consent screen → **Test users**, add your Google email address, and try again.

**"invalid_grant" or token expired error**
Delete `token.json` and run the script again to log in fresh.

**No events added / "No shifts found in PDF"**
The parser expects a table on the first page with time ranges in `HH:MM-HH:MM` format. Run with `--dry-run` first to see what the parser finds. If nothing is detected, the PDF layout may differ from what the tool expects — check that you're exporting the correct view from Kadromierz.

**Discord bot: OAuth callback fails**
`PUBLIC_BASE_URL` must be a publicly reachable URL (not `localhost`). The redirect URI in your Google Cloud credentials must match `https://your-public-url/oauth/callback` exactly, including the path.

**Discord bot: slash commands don't appear**
Slash commands can take up to an hour to propagate after the first sync. Restart the bot and wait a few minutes — it's usually much faster than the maximum delay.