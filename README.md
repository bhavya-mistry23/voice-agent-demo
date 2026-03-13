# 🎙️ Voice Agent

A collection of real-time AI voice agents that join live meetings, listen to participants, and respond with spoken audio — powered by different LLM backends.

## 📁 Implementations

| Directory | Stack | Model |
|-----------|-------|-------|
| [`gemini-python/`](./gemini-python/) | Python + aiohttp + Recall.ai | Gemini 2.5 Flash Live API |
| [`openai-python/`](./openai-python/) | Python + WebSocket + Recall.ai | OpenAI Realtime API |

Both implementations follow the same core pattern: a Recall.ai bot joins the meeting, streams audio to the server, the server forwards it to an LLM, and the AI's spoken response is injected back into the call via a browser page.

## 🔧 How it works

```
Meeting participants
      │  audio
      ▼
Recall.ai bot  ──────────────────────────────────────────┐
      │  streams PCM to /audio WS                        │
      ▼                                                  │
Python server  ──► LLM Live API  ──► /ws WebSocket       │
                                          │              │
                                     index.html          │
                                     plays audio         │
                                          │  captured    │
                                          └──────────────┘
                                      injected into call
```

## 🚀 Getting started

Pick the implementation you want and follow its README:

- **[gemini-python](./gemini-python/README.md)** — uses Gemini 2.5 Flash with native audio, `uv` for dependency management
- **[openai-python](./openai-python/README.md)** — uses OpenAI Realtime API with a React client

## 📋 Prerequisites

- [Recall.ai](https://www.recall.ai/) account and API key
- Public tunnel (e.g. [ngrok](https://ngrok.com/)) to expose your local server
- API key for your chosen LLM provider (Gemini or OpenAI)