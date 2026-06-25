#!/usr/bin/env python3
"""
Pi Bridge Server
Receives file operations from GitHub Pages, automates Claude.ai via Playwright,
and handles file uploads by attaching them to the Claude chat.

Setup:
  pip install flask flask-cors playwright
  playwright install chromium
  python pi_server.py

Then expose with ngrok:
  ngrok http 5001
"""

import asyncio
import base64
import json
import os
import tempfile
import threading
import time
from pathlib import Path

from flask import Flask, request, jsonify
from flask_cors import CORS
from playwright.async_api import async_playwright

# ── CONFIG ────────────────────────────────────────────────────────────
CLAUDE_URL   = "https://claude.ai"
CHAT_URL     = "https://claude.ai/chat"   # or specific chat URL
SERVER_PORT  = 5001
UPLOAD_DIR   = tempfile.mkdtemp(prefix="claude_bridge_")

# Paste your specific Claude chat URL here to always resume the same conversation
# Leave as None to use the most recent chat
PINNED_CHAT_URL = None  # e.g. "https://claude.ai/chat/abc123"

# ── FLASK APP ─────────────────────────────────────────────────────────
app = Flask(__name__)
CORS(app)  # allow requests from GitHub Pages

# shared state
state = {
    "browser":  None,
    "page":     None,
    "ready":    False,
    "busy":     False,
    "last_op":  None,
    "loop":     None,
}

# ── PLAYWRIGHT HELPERS ────────────────────────────────────────────────

async def get_page():
    """Return the Claude.ai page, launching browser if needed."""
    if state["page"] and not state["page"].is_closed():
        return state["page"]

    pw = await async_playwright().start()
    browser = await pw.chromium.launch(
        headless=False,           # visible so you can log in manually
        args=["--start-maximized"]
    )
    context = await browser.new_context(
        viewport={"width": 1280, "height": 900},
        # Reuse stored login session if it exists
        storage_state="claude_session.json" if os.path.exists("claude_session.json") else None
    )
    page = await context.new_page()

    url = PINNED_CHAT_URL or CHAT_URL
    await page.goto(url)

    # Wait for the chat input to appear (means we're logged in and loaded)
    try:
        await page.wait_for_selector('[data-testid="chat-input"], .ProseMirror, [contenteditable="true"]',
                                     timeout=30000)
        print("✓ Claude.ai loaded")
    except Exception:
        print("⚠ Couldn't find input — may need to log in manually")

    # Save session for next time
    await context.storage_state(path="claude_session.json")

    state["browser"] = browser
    state["page"]    = page
    state["ready"]   = True
    return page


async def find_input(page):
    """Find the Claude chat input box."""
    selectors = [
        '[data-testid="chat-input"]',
        '.ProseMirror',
        '[contenteditable="true"]',
        'textarea',
    ]
    for sel in selectors:
        el = await page.query_selector(sel)
        if el:
            return el
    return None


async def wait_for_response_complete(page, timeout=120):
    """Wait until Claude stops generating (send button re-appears as enabled)."""
    start = time.time()
    # Wait for the stop button to disappear (means generation ended)
    while time.time() - start < timeout:
        # Check if there's a stop/generating indicator
        stop_btn = await page.query_selector('[aria-label="Stop"], [data-testid="stop-button"]')
        if not stop_btn:
            await asyncio.sleep(1)
            # Double-check it's really done
            stop_btn2 = await page.query_selector('[aria-label="Stop"], [data-testid="stop-button"]')
            if not stop_btn2:
                return True
        await asyncio.sleep(0.5)
    return False


async def type_command(page, prompt: str, attachment_path: str = None):
    """Type a command into Claude and submit it, optionally with a file attached."""
    # Click somewhere to make sure we're focused
    await page.mouse.click(640, 450)
    await asyncio.sleep(0.3)

    # Find input
    inp = await find_input(page)
    if not inp:
        raise RuntimeError("Could not find Claude input")

    # Attach file first if needed
    if attachment_path:
        # Click the file attachment button (paperclip)
        attach_btn = await page.query_selector(
            '[aria-label="Attach files"], [data-testid="attach-button"], button[aria-label*="file"], button[aria-label*="attach"]'
        )
        if attach_btn:
            async with page.expect_file_chooser() as fc_info:
                await attach_btn.click()
            file_chooser = await fc_info.value
            await file_chooser.set_files(attachment_path)
            await asyncio.sleep(1)
        else:
            # Try drag-and-drop approach
            print("⚠ No attach button found, trying clipboard method")

    # Click the input and type
    await inp.click()
    await asyncio.sleep(0.2)

    # Clear any existing text
    await page.keyboard.press("Control+a")
    await page.keyboard.press("Delete")

    # Type the prompt (use clipboard for speed and reliability)
    await page.evaluate(f"""
        const el = document.activeElement;
        const text = {json.dumps(prompt)};
        if (el.isContentEditable) {{
            el.innerText = text;
            el.dispatchEvent(new Event('input', {{bubbles: true}}));
        }} else {{
            el.value = text;
            el.dispatchEvent(new Event('input', {{bubbles: true}}));
        }}
    """)
    await asyncio.sleep(0.3)

    # Submit
    await page.keyboard.press("Enter")
    print(f"✓ Sent command ({len(prompt)} chars)")


async def run_operation(op_type: str, payload: dict):
    """Execute a file operation by automating Claude."""
    page = await get_page()

    if op_type == "edit":
        path    = payload["path"]
        content = payload["content"]
        prompt  = f"""FILEOP:EDIT:{path}
<<<CONTENT>>>
{content}
<<<END>>>"""

    elif op_type == "delete":
        path   = payload["path"]
        prompt = f"FILEOP:DELETE:{path}"

    elif op_type == "rename":
        prompt = f"FILEOP:RENAME:{payload['old_path']}:{payload['new_path']}"

    elif op_type == "mkdir":
        prompt = f"FILEOP:MKDIR:{payload['path']}"

    elif op_type == "refresh":
        prompt = "FILEOP:REFRESH"

    elif op_type == "upload":
        dest   = payload["dest_path"]
        prompt = f"FILEOP:UPLOAD:{dest}"
        # attachment_path set below

    else:
        raise ValueError(f"Unknown op: {op_type}")

    attachment = payload.get("_attachment_path")
    await type_command(page, prompt, attachment)
    await wait_for_response_complete(page)
    print(f"✓ Operation complete: {op_type}")


# ── BACKGROUND THREAD ─────────────────────────────────────────────────

def run_async(coro):
    """Run a coroutine in the background event loop."""
    future = asyncio.run_coroutine_threadsafe(coro, state["loop"])
    return future.result(timeout=180)


def start_event_loop():
    loop = asyncio.new_event_loop()
    state["loop"] = loop
    asyncio.set_event_loop(loop)
    # Pre-launch the browser
    loop.run_until_complete(get_page())
    loop.run_forever()


# ── FLASK ROUTES ──────────────────────────────────────────────────────

@app.route("/api/status")
def status():
    return jsonify({
        "ready": state["ready"],
        "busy":  state["busy"],
        "last_op": state["last_op"],
    })


@app.route("/api/operation", methods=["POST"])
def operation():
    if state["busy"]:
        return jsonify({"error": "busy"}), 429

    data    = request.json
    op_type = data.get("type")
    payload = data.get("payload", {})

    state["busy"]    = True
    state["last_op"] = f"{op_type} {payload.get('path', payload.get('old_path',''))}"

    try:
        run_async(run_operation(op_type, payload))
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        state["busy"] = False


@app.route("/api/upload", methods=["POST"])
def upload():
    """Receive a file from GitHub Pages, save it, then send to Claude with attachment."""
    if state["busy"]:
        return jsonify({"error": "busy"}), 429

    dest_path = request.form.get("dest_path", "/tmp/uploaded_file")
    file      = request.files.get("file")

    if not file:
        return jsonify({"error": "no file"}), 400

    # Save to temp location on Pi
    suffix   = Path(file.filename).suffix
    tmp_path = os.path.join(UPLOAD_DIR, f"upload_{int(time.time())}{suffix}")
    file.save(tmp_path)
    print(f"✓ Saved upload: {tmp_path} → {dest_path}")

    state["busy"]    = True
    state["last_op"] = f"upload → {dest_path}"

    payload = {"dest_path": dest_path, "_attachment_path": tmp_path}
    try:
        run_async(run_operation("upload", payload))
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        state["busy"] = False
        # Clean up temp file after a delay
        threading.Timer(30, lambda: os.unlink(tmp_path) if os.path.exists(tmp_path) else None).start()


# ── MAIN ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"""
╔══════════════════════════════════════╗
║       Claude Bridge Server v1.0      ║
╠══════════════════════════════════════╣
║  Port:     {SERVER_PORT}                        ║
║  Uploads:  {UPLOAD_DIR[:30]}  ║
╚══════════════════════════════════════╝

1. Starting browser...
2. Expose with:  ngrok http {SERVER_PORT}
3. Set the ngrok URL in the GitHub Pages site config

""")

    # Start playwright loop in background thread
    t = threading.Thread(target=start_event_loop, daemon=True)
    t.start()

    # Wait for browser to be ready
    for _ in range(30):
        if state["ready"]:
            break
        time.sleep(1)

    app.run(host="0.0.0.0", port=SERVER_PORT, debug=False)
