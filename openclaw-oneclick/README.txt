OpenClaw One-Click package
==========================

Use on a new Windows machine:

1. Copy this whole openclaw-oneclick folder to the machine.
2. Double-click Install-OpenClaw-OneClick.cmd.
3. When asked, paste a Gemini API key, or press Enter to skip AI setup.
4. Wait until it finishes and opens the dashboard.
5. Later, use the Desktop shortcut named OpenClaw Dashboard.

What it installs/configures:

- Node.js LTS if missing, using winget.
- OpenClaw latest, using npm global install.
- Local loopback Gateway on 127.0.0.1:18789.
- Fast local computer-control profile:
  google, browser, device-pair, file-transfer, document-extract, web-readability.
- Unrelated model providers, voice, image, video, music, phone, canvas, and memory plugins are disabled by default.
- Desktop shortcut: OpenClaw Dashboard.
- Desktop shortcut: Configure OpenClaw AI.

Important:

- This package does not include your OpenAI/Gemini/Anthropic API key.
- On each new machine, you can paste a Gemini key during install.
- If you skip that step, configure the AI provider once by running
  Configure OpenClaw AI, or run:

  openclaw.cmd configure

- The dashboard itself can open without an online OpenClaw account.
  Chat replies still need a configured model provider/API key.

Files installed locally:

- %LOCALAPPDATA%\OpenClawOneClick
- %USERPROFILE%\.openclaw
