"""
Voice Agent — Gemini Live API + Recall.ai
"""

import time
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
You are an AI representative attending this Microsoft Teams meeting on behalf of the project manager. Make SURE TO SOUND EXCITED AND CRACK JOKES.
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


class VoiceAgent:
    def __init__(self):
        self.client = genai.Client(api_key=GEMINI_API_KEY)
        self.gemini_session = None
        self._session_ready = asyncio.Event()
        self._ws_sender = None
        self._input_chunk_count = 0
        self._output_chunk_count = 0

    def attach_sender(self, sender):
        self._ws_sender = sender
        logger.info("WebSocket sender attached")

    def detach_sender(self):
        self._ws_sender = None
        logger.info("WebSocket sender detached — Gemini session still running")

    async def _send(self, data: str):
        if self._ws_sender:
            try:
                await self._ws_sender(data)
            except Exception as e:
                logger.warning(f"Send failed (ws gone?): {e}")
                self._ws_sender = None

    async def run(self):
        """
        Long-running Gemini session with AUTO-RECONNECT.

        When receive() ends (server closes the turn / connection),
        we re-open the session automatically so audio keeps flowing.
        """
        first_connect = True

        while True:  # ← auto-reconnect loop
            try:
                logger.info("Opening Gemini Live session...")
                async with self.client.aio.live.connect(
                    model=MODEL, config=LIVE_CONFIG
                ) as session:
                    self.gemini_session = session
                    self._session_ready.set()
                    logger.info("Gemini Live session open — ready for audio")

                    # ---------- greeting (first time only) ----------
                    if first_connect:
                        await session.send_client_content(
                            turns=[
                                types.Content(
                                    role="user",
                                    parts=[
                                        types.Part(
                                            text="Please introduce yourself to the meeting with a joke."
                                        )
                                    ],
                                )
                            ],
                            turn_complete=True,
                        )
                        logger.info("Greeting sent to Gemini")
                        first_connect = False

                    # ---------- receive loop ----------
                    self._output_chunk_count = 0
                    async for response in session.receive():
                        # — audio data —
                        if response.data:
                            self._output_chunk_count += 1
                            if (
                                self._output_chunk_count <= 3
                                or self._output_chunk_count % 50 == 0
                            ):
                                logger.info(
                                    f"Gemini audio chunk #{self._output_chunk_count}: "
                                    f"{len(response.data)} bytes"
                                )
                            await self._send(
                                json.dumps(
                                    {
                                        "type": "response.audio.delta",
                                        "delta": base64.b64encode(
                                            response.data
                                        ).decode(),
                                    }
                                )
                            )

                        # — transcript text —
                        if response.text:
                            logger.info(f"Gemini text: {response.text}")

                        # — turn complete —
                        if (
                            hasattr(response, "server_content")
                            and response.server_content
                        ):
                            if getattr(response.server_content, "turn_complete", False):
                                logger.info(
                                    f"Gemini turn complete "
                                    f"({self._output_chunk_count} audio chunks sent)"
                                )
                                self._output_chunk_count = 0
                                await self._send(
                                    json.dumps({"type": "response.audio.done"})
                                )

                # ---- receive() iterator ended → session closed by server ----
                logger.warning("Gemini receive() ended — session closed by server")

            except Exception as e:
                logger.error(f"Gemini session error: {e}")

            finally:
                # Mark session as unavailable so send_audio() stops trying
                self._session_ready.clear()
                self.gemini_session = None

            logger.info("Reconnecting to Gemini in 2 seconds...")
            await asyncio.sleep(2)

    async def send_audio(self, pcm_bytes: bytes):
        """Send audio to Gemini. Silently drops if session isn't ready."""
        # ── guard: skip if session is down or reconnecting ──
        if not self._session_ready.is_set() or self.gemini_session is None:
            return

        try:
            await self.gemini_session.send_realtime_input(
                audio=types.Blob(data=pcm_bytes, mime_type="audio/pcm;rate=16000")
            )
            self._input_chunk_count += 1
            if self._input_chunk_count <= 3 or self._input_chunk_count % 200 == 0:
                logger.info(
                    f"Audio → Gemini (chunk #{self._input_chunk_count}, "
                    f"{len(pcm_bytes)} bytes)"
                )
        except Exception:
            # Session died — clear the flag so we stop hammering it.
            # The run() reconnect loop will set it again.
            if self._session_ready.is_set():
                logger.warning(
                    "Gemini session gone — pausing audio sends until reconnect"
                )
                self._session_ready.clear()


# ── single global agent ──
agent = VoiceAgent()


# ── HTTP ──
async def handle_http(request):
    if not CLIENT_HTML.exists():
        return web.Response(text=f"index.html not found at {CLIENT_HTML}", status=404)
    return web.FileResponse(CLIENT_HTML)


# ── /ws — bot audio output ──
async def handle_ws(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    logger.info("Output Media WebSocket connected")

    async def sender(data):
        if not ws.closed:
            await ws.send_str(data)

    agent.attach_sender(sender)
    try:
        async for msg in ws:
            if msg.type in (web.WSMsgType.CLOSE, web.WSMsgType.ERROR):
                break
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
    finally:
        agent.detach_sender()
        if not ws.closed:
            await ws.close()
        logger.info("Output Media WebSocket closed")
    return ws


# ── /audio — Recall.ai raw audio input ──
async def handle_audio(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    logger.info("Audio WebSocket connected from Recall.ai")

    audio_msg_count = 0
    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                try:
                    event = json.loads(msg.data)
                    if event.get("event") == "audio_mixed_raw.data":
                        b64 = event["data"]["data"]["buffer"]
                        pcm_bytes = base64.b64decode(b64)
                        if pcm_bytes:
                            audio_msg_count += 1
                            if audio_msg_count <= 3 or audio_msg_count % 500 == 0:
                                logger.info(
                                    f"Recall.ai audio #{audio_msg_count}: "
                                    f"{len(pcm_bytes)} bytes"
                                )
                            await agent.send_audio(pcm_bytes)
                except Exception as e:
                    logger.error(f"Audio processing error: {e}")

            elif msg.type in (
                web.WSMsgType.CLOSE,
                web.WSMsgType.CLOSING,
                web.WSMsgType.CLOSED,
            ):
                logger.info("Recall.ai closing connection")
                break

    except Exception as e:
        logger.error(f"Audio WebSocket error: {e}")
    finally:
        logger.info(
            f"Audio WebSocket closed (processed {audio_msg_count} audio messages)"
        )
        return ws


# ── Main ──
async def main():
    app = web.Application()
    app.router.add_get("/", handle_http)
    app.router.add_get("/ws", handle_ws)
    app.router.add_get("/audio", handle_audio)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()

    logger.info(f" Voice Agent on http://0.0.0.0:{PORT}")
    logger.info("  GET /       → index.html")
    logger.info("  GET /ws     → Gemini audio output to bot")
    logger.info("  GET /audio  → Recall.ai raw audio input from meeting")
    logger.info(f"Model: {MODEL}")

    asyncio.create_task(agent.run())
    logger.info("Gemini agent started — waiting for connections")

    await asyncio.Future()


if __name__ == "__main__":
    time.sleep(10)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Shutting down")
