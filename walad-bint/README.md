# ولد بنت حيوان جماد — online multiplayer

A realtime Arabic letters game for friends. A Python server holds each room and
runs every timer and rule; phones and browsers connect over one WebSocket.
Claude judges the answers.

## Files

| File | What it is |
|---|---|
| `server.py` | Game server: rooms, phases, timers, scoring, kick, chat, reconnects |
| `judge.py` | AI judge (Claude, model `claude-opus-5-5`) |
| `public/` | The web page (`index.html`, `app.js`, `style.css`) |
| `requirements.txt`, `render.yaml` | For deploying |

## Run it on your PC

1. Get an API key at https://console.anthropic.com (Settings → API Keys).
2. In PowerShell, inside this folder:

```powershell
python -m pip install -r requirements.txt
$env:ANTHROPIC_API_KEY = "sk-ant-..."   # your key
python server.py
```

3. Open http://localhost:8080. Friends on the same Wi‑Fi can use `http://<your-PC-IP>:8080`.

Without a key the game still runs, but every answer goes to a player vote.
`$env:WBHJ_FAKE_JUDGE = "1"` accepts every word with 2+ letters. Use it for testing only.

## Put it online (Render, free plan)

1. Push this folder to a GitHub repository.
2. On https://render.com: **New → Blueprint**, pick the repository (it reads `render.yaml`).
3. When asked, paste your `ANTHROPIC_API_KEY`.
4. Share the `https://….onrender.com` link.

On the free plan the server sleeps after ~15 minutes without visitors. The first visit
then takes about 30 seconds, and games that were in progress are lost. A paid instance
($7/mo) stays awake. Railway or Fly.io work the same way: start command `python server.py`,
and set `ANTHROPIC_API_KEY`.

## Game rules (as implemented)

- 2–12 players. The host sets the round time (45–240 s) and number of rounds.
- Players take turns picking a letter. Used letters are locked. After round 1, a picker
  who is in the top 3 may add one extra category for that round.
- 3‑second countdown, then everyone fills the 6 categories. Answers autosave as you type.
  The round ends when the timer runs out or everyone presses **انتهيت**.
- Judging: empty answers and wrong first letters are rejected right away (hamza forms and
  "ال" are ignored). Claude judges the rest and gives a short reason for each.
- Scoring: a valid unique answer = 10, a valid answer someone else also gave = 5, otherwise 0.
- Objections: anyone can object to any ruling. The other players (not the answer's
  owner) vote, and the majority wins. A tie keeps the original ruling. Votes close after 25 s.
- Next round starts when everyone is ready and no vote is open. The host can always skip.

## Reliability fixes vs. the old version

- Updates go only to the players in that room, as one message per change. There is no
  polling and no reloading of everything.
- The server owns every timer and sends the time left, so a wrong phone clock doesn't matter.
- Typing is never interrupted: inputs are built once per round and only counters update.
- Refresh or a dropped signal: you rejoin the same seat automatically. Your answers are
  kept, and the game waits 10 s before moving on without you.
- Host leaves → the host role passes to someone else after 20 s.
- Kick works in every phase. A kicked player is removed right away and can't rejoin.
- Chat works in every phase, with unread badges on phones and spam limits.
- If Claude can't be reached, affected answers go to a vote instead of freezing the game.

## Limits

- Rooms live in the server's memory: restarting or redeploying the server ends all games.
- Run **one** server instance. Rooms are not shared between instances.
