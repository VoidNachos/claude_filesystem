# 🗂 Claude Filesystem

> *Have you ever wanted to know what's inside Claude? Or control your own data in a data center?*

This project lets you browse, edit, create, and delete files **directly inside a live Claude AI container** — through a website that looks like Windows Explorer.

---

## How it works

```
You (browser)
    ↕  click files, edit, hit Send
GitHub Pages (this repo)
    ↕  HTTP request
Raspberry Pi (sitting at home)
    ↕  Playwright browser automation
Claude.ai (open in a real browser on the Pi)
    ↕  Claude reads the command and runs it
Linux container at Anthropic's data center
    ↕  git push
GitHub (this repo updates)
    ↕  file manager refreshes
You see the change
```

Everything you do in the file manager gets typed into Claude's chat by a Raspberry Pi, Claude executes it in its actual container, then pushes the result back to this repo so the website updates.

---

## What you can do

- 📁 Browse Claude's real Linux filesystem (`/home/claude`, `/etc`, `/tmp`, `/mnt` and more)
- 📝 Open and edit any text file — C source code, Python scripts, config files, shell scripts
- ➕ Create new files and folders anywhere in the container
- 🗑 Delete files
- ✏️ Rename and move files
- ⬆️ Upload files from your computer directly into the container
- 🔍 Search and filter files
- ☰ Switch between Details and Icons view
- 📋 Copy file paths

Changes are batched — you can make multiple edits and send them all at once with the **Send changes** button.

---

## What's actually in there

Claude runs in an Ubuntu 24 container with:
- Python, GCC, Node.js, Playwright, ImageMagick, rclone, FUSE
- A custom C FUSE filesystem that generates files on the fly
- Claude's own source files, scripts, and build artifacts
- Around 4GB of cached packages and browser binaries (not shown)

The container resets between conversations, so anything written here won't survive forever — but it's real while the conversation is active.

---

## Built with

- **GitHub Pages** — hosts the file manager UI
- **Raspberry Pi** — bridge between the website and Claude
- **Playwright** — controls a real Chromium browser (bypasses bot detection)
- **Flask** — Pi's local server with an async operation queue
- **FUSE (C)** — custom live filesystem mounted in the container
- **GitHub API** — stores the file tree snapshot and the send lock

---

*Made by [VoidNachos](https://github.com/VoidNachos) and Claude
