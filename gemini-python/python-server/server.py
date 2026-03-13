"""
PM Voice Agent — Gemini Live API + Recall.ai
Single port serves both HTTP (index.html) and WebSocket (bot audio bridge).
"""

import asyncio
import json
import logging
import os
import base64
import pathlib
from dotenv import load_dotenv
from aiohttp import web
from google import genai
from google.genai import types

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

load_dotenv()

PORT = int(os.getenv("PORT", 3000))
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
MODEL = "gemini-2.5-flash-native-audio-preview-12-2025"

if not GEMINI_API_KEY:
    raise ValueError("GEMINI_API_KEY must be set in .env file")

BASE_DIR = pathlib.Path(__file__).parent
CLIENT_HTML = BASE_DIR.parent / "client" / "index.html"

SYSTEM_PROMPT = """
You are an AI representative attending this Microsoft Teams meeting on behalf of the project manager. Make SURE TO CRACK JOKES EVERY TIME.
"""

LIVE_CONFIG = types.LiveConnectConfig(
    response_modalities=["AUDIO"],
    system_instruction=types.Content(
        parts=[types.Part(text=SYSTEM_PROMPT)],
        role="user",
    ),
    speech_config=types.SpeechConfig(
        voice_config=types.VoiceConfig(
            prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name="Charon")
        )
    ),
)


async def handle_http(request):
    if not CLIENT_HTML.exists():
        return web.Response(text=f"index.html not found at {CLIENT_HTML}", status=404)
    return web.FileResponse(CLIENT_HTML)


class PMVoiceAgent:
    def __init__(self, ws_send, ws_iter):
        self._send = ws_send
        self._iter = ws_iter
        self.client = genai.Client(api_key=GEMINI_API_KEY)

    async def run(self):
        logger.info("Opening Gemini Live session...")
        async with self.client.aio.live.connect(
            model=MODEL, config=LIVE_CONFIG
        ) as session:
            logger.info("Gemini Live session open")

            # Kick Gemini immediately — introduce itself with a joke
            await session.send_client_content(
                turns=types.Content(
                    role="user",
                    parts=[
                        types.Part(
                            text="Please introduce yourself to the meeting with a joke."
                        )
                    ],
                ),
                turn_complete=True,
            )
            logger.info("Sent greeting to Gemini")

            await asyncio.gather(
                self._inbound(session),
                self._outbound(session),
            )

    async def _inbound(self, session):
        """Meeting audio → Gemini"""
        try:
            async for raw in self._iter:
                try:
                    msg = json.loads(raw)
                    t = msg.get("type", "")
                    if t == "input_audio_buffer.append":
                        b64 = msg.get("audio", "")
                        if b64:
                            await session.send_realtime_input(
                                media=types.Blob(
                                    data=base64.b64decode(b64),
                                    mime_type="audio/pcm;rate=24000",
                                )
                            )
                    elif t == "conversation.item.create":
                        for part in msg.get("item", {}).get("content", []):
                            if part.get("type") == "input_text" and part.get("text"):
                                await session.send_client_content(
                                    turns=types.Content(
                                        role="user",
                                        parts=[types.Part(text=part["text"])],
                                    ),
                                    turn_complete=True,
                                )
                except Exception as e:
                    logger.error(f"Inbound error: {e}")
        except Exception:
            logger.info("Inbound stream ended")

    async def _outbound(self, session):
        """Gemini audio → meeting"""
        try:
            async for response in session.receive():
                if response.data:
                    logger.info(f"Sending audio chunk: {len(response.data)} bytes")
                    await self._send(
                        json.dumps(
                            {
                                "type": "response.audio.delta",
                                "delta": base64.b64encode(response.data).decode(),
                            }
                        )
                    )
                if response.text:
                    logger.info(f"Gemini text: {response.text}")
                if hasattr(response, "server_content") and response.server_content:
                    if getattr(response.server_content, "turn_complete", False):
                        logger.info("Gemini turn complete")
                        await self._send(json.dumps({"type": "response.audio.done"}))
        except Exception as e:
            logger.error(f"Outbound error: {e}")


async def handle_ws(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    logger.info(f"WebSocket connected from {request.remote}")

    async def sender(data):
        if not ws.closed:
            await ws.send_str(data)

    async def iterator():
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                yield msg.data
            elif msg.type in (web.WSMsgType.CLOSE, web.WSMsgType.ERROR):
                break

    agent = PMVoiceAgent(ws_send=sender, ws_iter=iterator())
    try:
        await agent.run()
    except Exception as e:
        logger.error(f"Agent error: {e}")
    finally:
        if not ws.closed:
            await ws.close()
        logger.info("WebSocket cleaned up")
    return ws


async def main():
    app = web.Application()
    app.router.add_get("/", handle_http)
    app.router.add_get("/ws", handle_ws)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()

    logger.info(f"PM Voice Agent on http://0.0.0.0:{PORT}")
    logger.info("  GET /    → serves index.html")
    logger.info("  GET /ws  → WebSocket bridge to Gemini")
    logger.info(f"Model: {MODEL}")
    logger.info("Ready.")
    await asyncio.Future()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Shutting down")
