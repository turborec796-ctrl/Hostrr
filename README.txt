TURBO BOT HOSTER - UPDATED
==========================

Features:
- Semantic inline-button styles: primary, success, danger (requires a recent Telegram client/Bot API).
- Free users can host up to 2 projects; premium users can host unlimited projects while premium is active.
- Users can start, stop, and permanently delete their own projects. After upload, Start, Stop, Delete File, Check Files, and Home buttons appear immediately for that specific project.
- Main menu includes a How to Use guide explaining upload, start, stop, delete, and supported entry files.
- Start uses Success style, Stop/Delete use Danger style, and navigation/How to Use uses Primary style where supported by Telegram clients/Bot API.
- Every new uploaded source file is sent to the configured admin chat with user and project details.
- Admin panel uses buttons for Broadcast, Set Update Channel, Set Support Username, Set Premium, Banned Users, and Unban User. Premium asks for user ID first, then duration (30m, 1h, 2d, 1w; maximum 365 days).
- Premium expires automatically when its duration ends.
- Existing SQLite database is migrated automatically.
- Hosting-bot detection scans uploaded Python/JavaScript files and source files inside ZIP archives. If several hosting-specific signals are found, the upload is rejected and the user receives a 30-minute automatic ban.
- During a ban, uploads and starting hosted projects are blocked; currently running projects are stopped. Expired bans clear automatically when the user interacts with the bot.
- Admin panel includes “Banned Users” to view active bans, reasons, and expiry times. Use the “Unban User” button to clear a ban early.
- Detection is heuristic rather than perfect; ordinary bots are less likely to trigger because multiple independent hosting indicators are required.

Setup:
1. Copy .env.example to .env.
2. Set BOT_TOKEN to a NEW token from BotFather and ADMIN_IDS to your Telegram numeric user ID.
3. Install dependencies: pip install -r requirements.txt
4. Start: python app.py

Important security note:
The bot token in the original uploaded file was hard-coded and exposed. Revoke it with BotFather and create a new token before running this version. Never publish .env.

Admin commands remain available as alternatives, but the requested setup actions can be done from the /admin button panel.
