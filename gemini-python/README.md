# 🤖 PM Voice Agent

A real-time AI voice agent that joins **Microsoft Teams meetings** on behalf of a project manager. Built with **Gemini 2.5 Flash Live API** and **Recall.ai**, it listens to meeting audio, generates intelligent spoken responses, and injects the AI voice back into the call — live.



## 📐 Architecture Overview

```
┌─────────────────────────────────────────────────────────────┐
│                      MEETING SIDE                           │
│                                                             │
│   [Teams Participants]                                      │
│       └─> Speaking in the call                              │
│                    │                                        │
│                    ▼                                        │
│   [Recall.ai Bot]                                           │
│       └─> Joins the meeting as a participant                │
│       └─> Streams mixed audio → WS /audio                   │
│       └─> Opens index.html in a headless browser            │
│       └─> Captures page audio → injects into call           │
└────────────────────┬────────────────────────────────────────┘
                     │  WebSocket (raw PCM audio)
                     ▼
┌─────────────────────────────────────────────────────────────┐
│                     SERVER SIDE (server.py)                 │
│                                                             │
│   [aiohttp Server]  ← port 3000                             │
│       │                                                     │
│       ├─> GET /        → serves index.html to Recall.ai     │
│       ├─> GET /audio   → receives meeting audio from Recall │
│       └─> GET /ws      → streams AI audio to browser        │
│                    │                                        │
│                    ▼                                        │
│   [PMVoiceAgent]                                            │
│       └─> send_audio()    → forwards PCM to Gemini          │
│       └─> attach_sender() → links WebSocket to browser      │
│       └─> auto-reconnect  → re-opens Gemini on drop         │
└────────────────────┬────────────────────────────────────────┘
                     │  Gemini Live API (persistent WS)
                     ▼
┌─────────────────────────────────────────────────────────────┐
│                    GEMINI SIDE                              │
│                                                             │
│   [Gemini 2.5 Flash Native Audio]                           │
│       └─> model: gemini-2.5-flash-native-audio-preview      │
│       └─> input:  audio/pcm @ 16kHz                         │
│       └─> output: audio/pcm @ 24kHz (voice: Charon)         │
│       └─> system prompt: PM representative persona          │
└─────────────────────────────────────────────────────────────┘
                     │  base64 PCM chunks over WS /ws
                     ▼
┌─────────────────────────────────────────────────────────────┐
│                    CLIENT SIDE (index.html)                 │
│                                                             │
│   [Browser Page — opened by Recall.ai]                      │
│       └─> Connects to WS via ?wss= URL param                │
│       └─> Receives response.audio.delta events              │
│       └─> Decodes PCM16 → Float32                           │
│       └─> Plays audio via Web Audio API @ 24kHz             │
│       └─> Recall.ai captures this audio → injects to call   │
└─────────────────────────────────────────────────────────────┘
```



## 🔄 Full Execution Flow

```
[Meeting participant speaks]
  └─> Audio is captured by Recall.ai's meeting bot

             │
             ▼
[Recall.ai → /audio WebSocket]
  └─> Sends JSON events: { "event": "audio_mixed_raw.data", ... }
  └─> Each event contains base64-encoded raw PCM audio

             │
             ▼
[PMVoiceAgent.send_audio()]
  └─> Decodes base64 → raw PCM bytes
  └─> Guards against sending if Gemini session is reconnecting
  └─> Forwards audio to Gemini Live at 16kHz

             │
             ▼
[Gemini 2.5 Flash Live API]
  └─> Processes real-time audio stream
  └─> Generates spoken response as PCM audio chunks
  └─> Streams response.data back to server

             │
             ▼
[PMVoiceAgent receives Gemini response]
  └─> Base64-encodes each audio chunk
  └─> Sends JSON: { "type": "response.audio.delta", "delta": "..." }
  └─> Sends over WS /ws to the connected browser page

             │
             ▼
[index.html — browser opened by Recall.ai]
  └─> Receives audio.delta events
  └─> Decodes base64 → Int16Array → Float32Array
  └─> Queues and plays through Web Audio API @ 24kHz
  └─> Dot indicator: green (connected) / blue pulsing (speaking)

             │
             ▼
[Recall.ai captures browser audio]
  └─> Injects it back into the Teams call as the agent's voice
  └─> Participants hear the PM Agent speaking
```

## 📁 Project Structure

```
gemini-python/
│
├── client/
│   └── index.html          # Browser audio playback page (opened by Recall.ai)
│
├── python-server/
│   ├── server.py           # aiohttp server + PMVoiceAgent + Gemini Live session
│   ├── .env                # API keys (not committed)
│   └── .gitignore
│
├── pyproject.toml          # uv project config
├── uv.lock
└── README.md
```



## 🛠️ Key Components

| Component | File | Role |
|-----------|------|------|
| `PMVoiceAgent` | `server.py` | Core class — manages Gemini session, audio I/O, auto-reconnect |
| `handle_audio` | `server.py` | WebSocket endpoint — receives raw PCM from Recall.ai |
| `handle_ws` | `server.py` | WebSocket endpoint — streams AI audio to the browser |
| `handle_http` | `server.py` | HTTP endpoint — serves `index.html` to Recall.ai |
| `index.html` | `client/` | Browser page — decodes and plays AI audio via Web Audio API |



## ⚙️ Setup

### 1. Install dependencies

```bash
uv sync
```

### 2. Configure environment variables

Create a `.env` file inside `python-server/`:

```env
GEMINI_API_KEY=your_gemini_api_key_here
PORT=3000
```

Get a Gemini API key from [Google AI Studio](https://aistudio.google.com/app/apikey).


## 🚀 Running the Project

### Step 1 — Start the server

Run from the 'gemini-python' directory

```bash
uv run server.py
```

The server starts on `http://0.0.0.0:3000` with three routes:

```
GET /       → serves index.html
GET /audio  → Recall.ai audio input (WebSocket)
GET /ws     → browser audio output (WebSocket)
```

> The server waits 10 seconds on startup before initialising — this is intentional to allow infrastructure (e.g. tunnels, Recall bot) to be ready.

### Step 2 — Expose your server publicly in new termial

Recall.ai needs to reach your server from the internet. Use a tunnel:

```bash
ngrok http 3000

```

Note your public URL, e.g. `https://abc123.ngrok.io`.

### Step 3 — Create a Recall.ai bot


```bash
curl --location 'https://ap-northeast-1.recall.ai/api/v1/bot/' \
--header 'Authorization: Token <YOUR_RECALL_API_TOKEN>' \
--header 'accept: application/json' \
--header 'content-type: application/json' \
--data '{
    "meeting_url": "https://teams.microsoft.com/meet/<YOUR_MEETING_ID>?p=<YOUR_MEETING_PASSCODE>",
    "bot_name": "PM Agent",
    "variant": {"microsoft_teams": "web_4_core"},
    "recording_config": {
      "audio_mixed_raw": {},
      "realtime_endpoints": [
        {
          "type": "websocket",
          "url": "wss://<YOUR_NGROK_DOMAIN>/audio",
          "events": ["audio_mixed_raw.data"]
        }
      ]
    },
    "output_media": {
      "camera": {
        "kind": "webpage",
        "config": {
          "url": "https://<YOUR_NGROK_DOMAIN>?wss=wss://<YOUR_NGROK_DOMAIN>/ws"
        }
      }
    }
  }'
```

### Step 4 — Agent joins and speaks

Once the bot joins:
- The Gemini session opens and the agent introduces itself with a joke
- Meeting audio starts flowing through `/audio` → Gemini → `/ws` → browser → back into the call
- The agent responds in real time as participants speak



## 🔁 Auto-Reconnect

The Gemini Live session is kept alive with an automatic reconnect loop in `PMVoiceAgent.run()`. If the session drops:

1. The `_session_ready` event is cleared — audio sends are paused silently
2. After a 2-second delay, a new Gemini session is opened
3. The `_session_ready` event is set again — audio resumes
4. The greeting is **not** repeated on reconnects


## 🎙️ Customising the Agent

All persona config lives at the top of `server.py`:

```python
SYSTEM_PROMPT = """
You are an AI representative attending this Microsoft Teams meeting
on behalf of the project manager. Make SURE TO SOUND EXCITED AND CRACK JOKES.
"""

LIVE_CONFIG = types.LiveConnectConfig(
    response_modalities=["AUDIO"],
    speech_config=types.SpeechConfig(
        voice_config=types.VoiceConfig(
            prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name="Charon")
        )
    ),
)
```

Available Gemini voices: `Charon`, `Puck`, `Kore`, `Fenrir`, `Aoede`, and others — see [Google's voice list](https://ai.google.dev/gemini-api/docs/live).


## 📦 Dependencies

```
google-genai      # Gemini Live API client
aiohttp           # Async HTTP + WebSocket server
python-dotenv     # .env file loading
websockets        # WebSocket support
numpy             # Audio buffer processing
```

Python `>= 3.11` required.