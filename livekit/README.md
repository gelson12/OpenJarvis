# 🎤 OpenJarvis — LiveKit voice channel

This directory contains the LiveKit bridge worker that lets OpenJarvis answer
voice calls. The worker is the *transport*; all intelligence (tools, memory,
multi-LLM routing, learning) lives in OpenJarvis core.

## 📂 Files

| File | What it is |
|---|---|
| [`worker.py`](./worker.py) | LiveKit Agents worker — VAD + STT (Deepgram) + voice loop + TTS (Deepgram). Forwards to OpenJarvis `/v1/chat/completions` with HTTP Basic Auth override. Also sends structured UI commands back over a LiveKit data channel (e.g. camera on/off). |
| [`Dockerfile`](./Dockerfile) | Container image for the worker. |
| [`railway.json`](./railway.json) | Railway deploy config. |
| [`requirements.txt`](./requirements.txt) | Python deps for the worker. |
| [`start.sh`](./start.sh) | Entrypoint script. |
| [**`COMPARISON.md`**](./COMPARISON.md) | 📐 Deep-dive comparing this OpenJarvis LiveKit channel to a minimal reference stack ([gelson12/friday_jarvis2](https://github.com/gelson12/friday_jarvis2)) — coding, infra, worker design, versatility, and self-learning. |

## 🚀 Quick start

```bash
# Required env vars (see worker.py docstring for the full list):
export LIVEKIT_URL="wss://your.livekit.cloud"
export LIVEKIT_API_KEY=…
export LIVEKIT_API_SECRET=…
export DEEPGRAM_API_KEY=…
export OPENJARVIS_URL="http://openjarvis.railway.internal:8000"
export OPENJARVIS_BASIC_AUTH_USER=…       # if OpenJarvis runs the Basic Auth gate
export OPENJARVIS_BASIC_AUTH_PASSWORD=…
export OPENJARVIS_MODEL="openrouter/auto"   # or any MultiEngine-accepted id

python worker.py start
```

## 🤔 Wondering how this compares to a 100-line tutorial agent?

See **[COMPARISON.md](./COMPARISON.md)** — it walks through three sections:

1. 🐍 The minimal Python LiveKit worker (`br` branch of friday_jarvis2).
2. ⚛️ The minimal Next.js custom UI (`LiveKit` branch of friday_jarvis2).
3. 🏛️ How those two compare, as a whole, to the entire OpenJarvis platform —
   with scorecards, an architecture Mermaid diagram, and a verdict panel for
   when to pick which.
