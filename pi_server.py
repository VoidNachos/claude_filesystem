#!/usr/bin/env python3
"""
Pi Bridge Server v3
- Async queue: one operation at a time, waits for FILEOP_DONE before next
- No file picker: uploads sent as text/base64 in the prompt
"""

import asyncio, base64, json, os, threading, time
from flask import Flask, request, jsonify
from flask_cors import CORS
from playwright.async_api import async_playwright

CLAUDE_URL      = "https://claude.ai"
PINNED_CHAT_URL = None   # e.g. "https://claude.ai/chat/YOUR_CHAT_ID"
SERVER_PORT     = 5001
MAX_INLINE_SIZE = 500_000

app  = Flask(__name__)
CORS(app)

state = {
    "pw": None, "browser": None, "page": None,
    "ready": False, "loop": None,
    "current_op": None,   # description of what's running now
    "queue_depth": 0,     # how many ops are waiting
}

op_queue = None   # asyncio.Queue, created inside the event loop

# ── PLAYWRIGHT ────────────────────────────────────────────────────────

async def get_page():
    if state["page"] and not state["page"].is_closed():
        return state["page"]
    pw      = await async_playwright().start()
    browser = await pw.chromium.launch(headless=False, args=["--start-maximized"])
    ctx     = await browser.new_context(
        viewport={"width": 1280, "height": 900},
        storage_state="claude_session.json" if os.path.exists("claude_session.json") else None
    )
    page = await ctx.new_page()
    url  = PINNED_CHAT_URL or CLAUDE_URL
    print(f"Opening {url}...")
    await page.goto(url)
    try:
        await page.wait_for_selector(
            '[data-testid="chat-input"], .ProseMirror, [contenteditable="true"]',
            timeout=30000)
        print("✓ Claude.ai ready")
    except:
        print("⚠ Log in manually then the server will continue")
    await ctx.storage_state(path="claude_session.json")
    state["pw"] = pw; state["browser"] = browser
    state["page"] = page; state["ready"] = True
    return page

async def find_input(page):
    for sel in ['[data-testid="chat-input"]', '.ProseMirror', '[contenteditable="true"]', 'textarea']:
        el = await page.query_selector(sel)
        if el: return el
    return None

async def wait_for_done(page, timeout=180):
    """Wait until Claude's response contains FILEOP_DONE or FILEOP_ERROR."""
    start = time.time()
    await asyncio.sleep(2)   # let generation start
    while time.time() - start < timeout:
        # Check if last assistant message contains our signal
        result = await page.evaluate("""() => {
            const sels = [
                '[data-testid="assistant-message"]',
                '.font-claude-message',
                '[class*="assistant"]',
            ];
            for (const sel of sels) {
                const els = document.querySelectorAll(sel);
                if (els.length) {
                    const last = els[els.length - 1];
                    const t = last.textContent || '';
                    if (t.includes('FILEOP_DONE'))  return 'done';
                    if (t.includes('FILEOP_ERROR')) return 'error';
                }
            }
            // Fallback: check if stop button is gone
            const stop = document.querySelector('[aria-label="Stop"], [data-testid="stop-button"]');
            return stop ? 'generating' : 'idle';
        }""")
        if result in ('done', 'error'):
            print(f"  Claude signalled: {result}")
            return result == 'done'
        if result == 'idle':
            # No stop button and no signal — wait a moment to be sure
            await asyncio.sleep(1.5)
            result2 = await page.evaluate("""() => {
                const sels = ['[data-testid="assistant-message"]', '.font-claude-message', '[class*="assistant"]'];
                for (const sel of sels) {
                    const els = document.querySelectorAll(sel);
                    if (els.length) {
                        const t = els[els.length-1].textContent || '';
                        if (t.includes('FILEOP_DONE'))  return 'done';
                        if (t.includes('FILEOP_ERROR')) return 'error';
                    }
                }
                return 'idle';
            }""")
            if result2 in ('done', 'error'): return result2 == 'done'
            # Generation ended but no signal — assume done
            return True
        await asyncio.sleep(0.5)
    print("  ⚠ Timeout waiting for FILEOP_DONE")
    return False

async def send_prompt(page, prompt: str):
    inp = await find_input(page)
    if not inp: raise RuntimeError("Could not find Claude input")
    await inp.click()
    await asyncio.sleep(0.2)
    await page.evaluate("""(text) => {
        const el = document.activeElement;
        if (el.isContentEditable) {
            el.innerText = text;
            el.dispatchEvent(new InputEvent('input', {bubbles: true}));
        } else {
            el.value = text;
            el.dispatchEvent(new Event('input', {bubbles: true}));
        }
    }""", prompt)
    await asyncio.sleep(0.3)
    await page.keyboard.press("Enter")
    print(f"  ✓ Sent ({len(prompt):,} chars)")

# ── PROMPT BUILDERS ───────────────────────────────────────────────────

def build_prompt(op_type, payload):
    if op_type == "edit":
        return f"FILEOP:EDIT:{payload['path']}\n<<<CONTENT>>>\n{payload['content']}\n<<<END>>>"
    elif op_type == "delete":
        return f"FILEOP:DELETE:{payload['path']}"
    elif op_type == "rename":
        return f"FILEOP:RENAME:{payload['old_path']}:{payload['new_path']}"
    elif op_type == "mkdir":
        return f"FILEOP:MKDIR:{payload['path']}"
    elif op_type == "upload":
        enc = payload.get("encoding", "text")
        op  = "WRITE_B64" if enc == "base64" else "EDIT"
        return f"FILEOP:{op}:{payload['dest_path']}\n<<<CONTENT>>>\n{payload['content']}\n<<<END>>>"
    elif op_type == "refresh":
        return "FILEOP:REFRESH"
    else:
        raise ValueError(f"Unknown op: {op_type}")

# ── QUEUE WORKER ──────────────────────────────────────────────────────

async def queue_worker():
    global op_queue
    op_queue = asyncio.Queue()
    print("✓ Queue worker started")
    while True:
        item = await op_queue.get()
        op_type = item["type"]
        payload = item["payload"]
        desc    = item.get("desc", op_type)

        state["current_op"]  = desc
        state["queue_depth"] = op_queue.qsize()

        print(f"\n→ Running: {desc}")
        try:
            page   = await get_page()
            prompt = build_prompt(op_type, payload)
            await send_prompt(page, prompt)
            ok = await wait_for_done(page)
            print(f"  {'✓ done' if ok else '✗ error'}: {desc}")
        except Exception as e:
            print(f"  ✗ Exception: {e}")
        finally:
            state["current_op"]  = None
            state["queue_depth"] = op_queue.qsize()
            op_queue.task_done()

def enqueue(op_type, payload, desc=None):
    """Thread-safe: add operation to the async queue."""
    if op_queue is None:
        raise RuntimeError("Queue not ready")
    desc = desc or f"{op_type} {payload.get('path', payload.get('dest_path', payload.get('old_path', '')))}"
    asyncio.run_coroutine_threadsafe(
        op_queue.put({"type": op_type, "payload": payload, "desc": desc}),
        state["loop"]
    )
    state["queue_depth"] = (state["queue_depth"] or 0) + 1

# ── FLASK ROUTES ──────────────────────────────────────────────────────

@app.route("/api/status")
def status():
    return jsonify({
        "ready":       state["ready"],
        "busy":        state["current_op"] is not None,
        "current_op":  state["current_op"],
        "queue_depth": state["queue_depth"],
    })

@app.route("/api/operation", methods=["POST"])
def operation():
    if not state["ready"]:
        return jsonify({"error": "not ready"}), 503
    data    = request.json or {}
    op_type = data.get("type")
    payload = data.get("payload", {})
    desc    = data.get("desc")
    try:
        enqueue(op_type, payload, desc)
        return jsonify({"ok": True, "queued": state["queue_depth"]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/upload", methods=["POST"])
def upload():
    if not state["ready"]:
        return jsonify({"error": "not ready"}), 503
    dest_path = request.form.get("dest_path", "/tmp/upload")
    file      = request.files.get("file")
    if not file:
        return jsonify({"error": "no file"}), 400
    raw = file.read()
    print(f"  upload: {file.filename} ({len(raw):,} bytes) → {dest_path}")
    try:
        content  = raw.decode("utf-8")
        encoding = "text"
    except UnicodeDecodeError:
        content  = base64.b64encode(raw).decode()
        encoding = "base64"
    payload = {"dest_path": dest_path, "content": content, "encoding": encoding}
    try:
        enqueue("upload", payload, f"upload {file.filename} → {dest_path}")
        return jsonify({"ok": True, "queued": state["queue_depth"]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── BOOT ──────────────────────────────────────────────────────────────

def start_loop():
    loop = asyncio.new_event_loop()
    state["loop"] = loop
    asyncio.set_event_loop(loop)
    loop.run_until_complete(get_page())
    loop.run_until_complete(queue_worker())  # runs forever

if __name__ == "__main__":
    print(f"""
╔══════════════════════════════════════╗
║     Claude Pi Bridge Server v3.0     ║
╠══════════════════════════════════════╣
║  Port:   {SERVER_PORT}                          ║
║  Queue:  one op at a time            ║
║  Wait:   FILEOP_DONE signal          ║
╚══════════════════════════════════════╝
Set PINNED_CHAT_URL at top of file.
Then: ngrok http {SERVER_PORT}
""")
    threading.Thread(target=start_loop, daemon=True).start()
    for _ in range(30):
        if state["ready"]: break
        time.sleep(1)
    app.run(host="0.0.0.0", port=SERVER_PORT, debug=False)
