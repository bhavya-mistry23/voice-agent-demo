"""
PM Voice Agent — Gemini Live API + Recall.ai
Wake-word gated: agent only responds when "PM Agent" is mentioned
"""

import time
import asyncio
import json
import logging
import os
import base64
import pathlib
from collections import deque
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

# Main agent — high quality, only runs when triggered
AGENT_MODEL = "gemini-2.5-flash-native-audio-preview-12-2025"

# Keyword listener — cheap, always on
LISTENER_MODEL = "gemini-2.5-flash-native-audio-preview-12-2025"

if not GEMINI_API_KEY:
    raise ValueError("GEMINI_API_KEY must be set in .env file")

BASE_DIR = pathlib.Path(__file__).parent
CLIENT_HTML = BASE_DIR.parent / "client" / "index.html"

# ── How long the gate stays open after trigger (seconds) ──
GATE_DURATION = 20

# ── Rolling audio buffer: ~3 seconds at ~50 chunks/sec ──
BUFFER_MAXLEN = 150

AGENT_SYSTEM_PROMPT = """
You are an AI representative attending this Microsoft Teams meeting on behalf of the project manager. Make SURE TO CRACK CODING JOKES.
Only respond when someone has specifically called for you (you will only receive audio when you are being addressed).
Keep responses concise and relevant to what was just asked.
"""

LISTENER_SYSTEM_PROMPT = """
You are a keyword detector listening to a live meeting audio stream.
Your ONLY job is to detect if the phrase "Hey Agent" or "Hey agent" or "Hi agent" or "Hi Agent" is spoken.
If you hear it, respond with exactly one word: TRIGGER
If you do not hear it, respond with nothing at all — empty string only.
Do not transcribe. Do not summarise. Do not respond to anything else.
"""

AGENT_LIVE_CONFIG = types.LiveConnectConfig(
    response_modalities=["AUDIO"],
    system_instruction=types.Content(
        parts=[types.Part(text=AGENT_SYSTEM_PROMPT)],
        role="user",
    ),
    speech_config=types.SpeechConfig(
        voice_config=types.VoiceConfig(
            prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name="Charon")
        )
    ),
)

LISTENER_LIVE_CONFIG = types.LiveConnectConfig(
    response_modalities=["AUDIO"],
    system_instruction=types.Content(
        parts=[types.Part(text=LISTENER_SYSTEM_PROMPT)],
        role="user",
    ),
    output_audio_transcription=types.AudioTranscriptionConfig(),
)


# ────────────────────────────────────────────────────────────────
# Keyword Listener — always-on, cheap, watches for "PM Agent"
# ────────────────────────────────────────────────────────────────


class KeywordListener:
    def __init__(self, on_trigger):
        """
        on_trigger: async callable — called when "PM Agent" is detected.
        """
        self.client = genai.Client(api_key=GEMINI_API_KEY)
        self._on_trigger = on_trigger
        self._session = None
        self._session_ready = asyncio.Event()
        self._chunk_count = 0

    async def run(self):
        """Long-running listener session with auto-reconnect."""
        while True:
            try:
                logger.info("Opening Keyword Listener session...")
                async with self.client.aio.live.connect(
                    model=LISTENER_MODEL, config=LISTENER_LIVE_CONFIG
                ) as session:
                    self._session = session
                    self._session_ready.set()
                    logger.info("Keyword Listener ready — watching for 'PM Agent'")

                    async for response in session.receive():
                        # Check transcription of audio response
                        transcript = None
                        if (
                            hasattr(response, "server_content")
                            and response.server_content
                            and hasattr(response.server_content, "output_transcription")
                            and response.server_content.output_transcription
                        ):
                            transcript = (
                                response.server_content.output_transcription.text
                            )

                        if transcript:
                            text = transcript.strip()
                            if text:
                                logger.info(f"Listener transcript: '{text}'")
                            if "TRIGGER" in text.upper():
                                logger.info(
                                    "*** TRIGGER DETECTED — waking PM Agent ***"
                                )
                                await self._on_trigger()

            except Exception as e:
                logger.error(f"Keyword Listener error: {e}")
            finally:
                self._session_ready.clear()
                self._session = None

            logger.info("Keyword Listener reconnecting in 2s...")
            await asyncio.sleep(2)

    async def send_audio(self, pcm_bytes: bytes):
        """Forward audio to the listener session."""
        if not self._session_ready.is_set() or self._session is None:
            return
        try:
            await self._session.send_realtime_input(
                audio=types.Blob(data=pcm_bytes, mime_type="audio/pcm;rate=16000")
            )
            self._chunk_count += 1
            if self._chunk_count <= 3 or self._chunk_count % 500 == 0:
                logger.info(
                    f"Listener audio chunk #{self._chunk_count}: {len(pcm_bytes)} bytes"
                )
        except Exception as e:
            logger.warning(f"Listener send failed: {e}")
            self._session_ready.clear()


# ────────────────────────────────────────────────────────────────
# PM Voice Agent — the real responder, gated by keyword
# ────────────────────────────────────────────────────────────────


class PMVoiceAgent:
    def __init__(self):
        self.client = genai.Client(api_key=GEMINI_API_KEY)
        self.gemini_session = None
        self._session_ready = asyncio.Event()
        self._ws_sender = None
        self._input_chunk_count = 0
        self._output_chunk_count = 0

        # ── Gate state ──
        self._gate_open = False
        self._gate_task = None  # timer task to auto-close gate

        # ── Rolling audio buffer (~3 seconds) ──
        # Stores recent PCM chunks so we can flush them to Gemini
        # when the gate opens — catching words spoken just before trigger
        self._audio_buffer = deque(maxlen=BUFFER_MAXLEN)

    # ── WebSocket sender management ──

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

    # ── Gate control ──

    async def open_gate(self):
        """Called by KeywordListener when 'PM Agent' is detected."""
        if self._gate_open:
            # Already open — just reset the timer
            logger.info("Gate already open — resetting timer")
            if self._gate_task:
                self._gate_task.cancel()
        else:
            logger.info(f"Gate OPENED — agent is listening (timeout: {GATE_DURATION}s)")
            self._gate_open = True

            # Flush the audio buffer so Gemini hears what was said
            # just before and around the trigger word
            await self._flush_buffer()

        # (Re)start the auto-close timer
        self._gate_task = asyncio.create_task(self._auto_close_gate())

    async def _auto_close_gate(self):
        """Automatically close the gate after GATE_DURATION seconds of no new trigger."""
        await asyncio.sleep(GATE_DURATION)
        self._gate_open = False
        self._gate_task = None
        logger.info("Gate CLOSED — agent back to standby")

    async def _flush_buffer(self):
        """Send buffered audio to Gemini so it hears the context around the trigger."""
        if not self._session_ready.is_set() or self.gemini_session is None:
            return
        chunks = list(self._audio_buffer)
        self._audio_buffer.clear()
        logger.info(f"Flushing {len(chunks)} buffered audio chunks to Gemini")
        for chunk in chunks:
            try:
                await self.gemini_session.send_realtime_input(
                    audio=types.Blob(data=chunk, mime_type="audio/pcm;rate=16000")
                )
            except Exception as e:
                logger.warning(f"Buffer flush error: {e}")
                break

    # ── Main Gemini session ──

    async def run(self):
        """Long-running Gemini session with auto-reconnect."""
        first_connect = True

        while True:
            try:
                logger.info("Opening Gemini Live session (PM Agent)...")
                async with self.client.aio.live.connect(
                    model=AGENT_MODEL, config=AGENT_LIVE_CONFIG
                ) as session:
                    self.gemini_session = session
                    self._session_ready.set()
                    logger.info("PM Agent Gemini session open — waiting for trigger")

                    if first_connect:
                        await session.send_client_content(
                            turns=[
                                types.Content(
                                    role="user",
                                    parts=[
                                        types.Part(
                                            text="Please introduce yourself to the meeting briefly with a joke. Let them know they can get your attention by saying 'PM Agent'."
                                        )
                                    ],
                                )
                            ],
                            turn_complete=True,
                        )
                        logger.info("Greeting sent to Gemini")
                        first_connect = False

                    self._output_chunk_count = 0
                    async for response in session.receive():
                        if response.data:
                            self._output_chunk_count += 1
                            if (
                                self._output_chunk_count <= 3
                                or self._output_chunk_count % 50 == 0
                            ):
                                logger.info(
                                    f"Agent audio chunk #{self._output_chunk_count}: "
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

                        if response.text:
                            logger.info(f"Agent text: {response.text}")

                        if (
                            hasattr(response, "server_content")
                            and response.server_content
                        ):
                            if getattr(response.server_content, "turn_complete", False):
                                logger.info(
                                    f"Agent turn complete ({self._output_chunk_count} chunks)"
                                )
                                self._output_chunk_count = 0
                                await self._send(
                                    json.dumps({"type": "response.audio.done"})
                                )
                                # Close gate after agent finishes speaking
                                if self._gate_task:
                                    self._gate_task.cancel()
                                self._gate_open = False
                                logger.info("Gate CLOSED — agent finished responding")

                logger.warning("Agent receive() ended — session closed by server")

            except Exception as e:
                logger.error(f"Agent Gemini session error: {e}")
            finally:
                self._session_ready.clear()
                self.gemini_session = None

            logger.info("Agent reconnecting to Gemini in 2s...")
            await asyncio.sleep(2)

    async def send_audio(self, pcm_bytes: bytes):
        """
        Always buffer recent audio.
        Only forward to Gemini when the gate is open.
        """
        # Always keep a rolling buffer regardless of gate state
        self._audio_buffer.append(pcm_bytes)

        # Only forward to Gemini if gate is open
        if not self._gate_open:
            return

        if not self._session_ready.is_set() or self.gemini_session is None:
            return

        try:
            await self.gemini_session.send_realtime_input(
                audio=types.Blob(data=pcm_bytes, mime_type="audio/pcm;rate=16000")
            )
            self._input_chunk_count += 1
            if self._input_chunk_count <= 3 or self._input_chunk_count % 200 == 0:
                logger.info(
                    f"Agent audio → Gemini (chunk #{self._input_chunk_count}, "
                    f"{len(pcm_bytes)} bytes)"
                )
        except Exception:
            if self._session_ready.is_set():
                logger.warning(
                    "Agent session gone — pausing audio sends until reconnect"
                )
                self._session_ready.clear()


# ── Single global instances ──
agent = PMVoiceAgent()
listener = KeywordListener(on_trigger=agent.open_gate)


# ── HTTP — serve index.html ──
async def handle_http(request):
    if not CLIENT_HTML.exists():
        return web.Response(text=f"index.html not found at {CLIENT_HTML}", status=404)
    return web.FileResponse(CLIENT_HTML)


# ── /ws — AI audio output to browser ──
async def handle_ws(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    logger.info("Output WebSocket connected")

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
        logger.info("Output WebSocket closed")
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
                            # Fork: send to BOTH listener and agent simultaneously
                            await asyncio.gather(
                                listener.send_audio(pcm_bytes),
                                agent.send_audio(pcm_bytes),
                            )
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
        logger.info(f"Audio WebSocket closed (processed {audio_msg_count} messages)")
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

    logger.info(f"PM Voice Agent on http://0.0.0.0:{PORT}")
    logger.info("  GET /       → index.html")
    logger.info("  GET /ws     → AI audio output to browser")
    logger.info("  GET /audio  → Recall.ai raw audio input")
    logger.info(f"  Agent model:   {AGENT_MODEL}")
    logger.info(f"  Listener model: {LISTENER_MODEL}")
    logger.info(f"  Gate duration: {GATE_DURATION}s")

    # Start both sessions as parallel background tasks
    asyncio.create_task(agent.run())
    asyncio.create_task(listener.run())
    logger.info("Agent + Listener started — say 'PM Agent' to trigger")

    await asyncio.Future()


if __name__ == "__main__":
    time.sleep(10)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Shutting down")
