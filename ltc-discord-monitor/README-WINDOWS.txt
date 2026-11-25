LITECOIN DISCORD BOT - WINDOWS 11 SETUP

SECURITY FIRST
- You previously pasted a bot token into chat. Treat that token as compromised.
- Discord Developer Portal > Your Application > Bot > Reset Token.
- Put the NEW token only in your local .env file. Never paste it into chat or share it.
- If you do not want transaction alerts, DISCORD_CHANNEL_ID can be 0. Monitoring alerts need a channel ID.

1. Install Python
- Download Python 3.11 or newer from https://www.python.org/downloads/windows/
- During installation, tick "Add python.exe to PATH".
- Open Command Prompt and run: py --version

2. Create the project folder
- Extract this ZIP somewhere easy, such as Desktop\ltc-discord-monitor.
- Open the folder in File Explorer.
- In Notepad, open .env.example, add your new token and the channel ID, then Save As:
  File name: .env
  Save as type: All files
  Encoding: UTF-8
  Make sure the file is .env, NOT .env.txt.
- Do not put quotes around the values.

3. Install packages
- Open the project folder in File Explorer.
- Click the address bar, type cmd, and press Enter.
- Run:
  py -m venv .venv
  .venv\Scripts\activate
  py -m pip install --upgrade pip
  pip install -r requirements.txt

4. Start the bot
- In the same Command Prompt:
  py bot.py
- Keep this window open while you want the bot online.
- Stop it with Ctrl+C.

5. Discord Developer Portal
- https://discord.com/developers/applications
- Select your application > Bot > Privileged Gateway Intents.
- Enable MESSAGE CONTENT INTENT and save.
- OAuth2 > URL Generator: select scopes "bot" and "applications.commands" (the latter is harmless; this bot uses prefix commands).
- Bot permissions: View Channels, Send Messages, Embed Links, Read Message History.
- Invite the bot to your server.

6. Commands
- .bal LTC_ADDRESS       Check any Litecoin address
- .monitor LTC_ADDRESS  Start monitoring an address (Manage Server permission required)
- .unmonitor LTC_ADDRESS Stop monitoring an address (Manage Server permission required)
- .wallets               List monitored addresses (Manage Server permission required)

7. Alerts
- Set DISCORD_CHANNEL_ID to the numeric ID of a channel where the bot can send messages.
- To copy a channel ID: Discord Settings > Advanced > Developer Mode ON; right-click the channel > Copy Channel ID.
- The bot checks monitored wallets every 30 seconds while running.
- First monitoring setup records a baseline, so older transactions do not trigger alerts.

NOTES
- .bal works on any valid Litecoin address; it does not need to be in the monitor list.
- The public Litecoin Space explorer API is used. It may rate-limit or be temporarily unavailable.
- This starter polls recent transaction pages. For high-volume wallets, use a self-hosted node or a provider with pagination/webhooks.
- A zero balance does not mean an address cannot receive funds.
- This is a monitoring utility, not proof of payment settlement. Wait for appropriate confirmations before treating a payment as final.
- No private keys, seed phrases, or funds are needed. Never enter them.
