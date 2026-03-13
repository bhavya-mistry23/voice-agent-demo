People talking in Teams
        ↓
Recall.ai captures mixed audio → sends to /audio as base64 PCM chunks
        ↓
Our server decodes it → forwards raw bytes to Gemini Live API
        ↓
Gemini listens, thinks, generates a spoken response
        ↓
Gemini streams audio chunks back to us
        ↓
We base64-encode them → push through /ws back to Recall.ai
        ↓
Recall.ai plays that audio into the meeting
        ↓
Meeting participants hear the AI bot respond