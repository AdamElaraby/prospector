"""WhatsApp click-to-send queue.

No API, no automation, no ban risk. The pipeline writes a local HTML page with
one card per lead; each card has a wa.me link that opens WhatsApp with the
message already typed. You read it, hit send, tick the box. When you are done,
the page hands you a single command to mark those leads sent in the database.

Why not automate it: Meta's Cloud API requires pre-approved templates and prior
opt-in for business-initiated messages, and the unofficial libraries get numbers
banned. Clicking is the only version of this that survives contact with reality.
"""

from __future__ import annotations

import html
import json
import re
from pathlib import Path
from urllib.parse import quote


def normalize_phone(raw: str | None, region: str | None = None) -> str | None:
    """Digits for a wa.me link, or None when the number is not provably valid.

    `region` is an ISO country like "AE", resolved from where the lead was
    found. Parsing and validation are libphonenumber's job — see countries.py
    for why hand-rolling this was abandoned.
    """
    from ..countries import to_e164

    return to_e164(raw, region)


def wa_link(phone: str, message: str) -> str:
    return f"https://wa.me/{phone}?text={quote(message)}"


def render_queue(
    rows: list[dict],
    out_path: Path,
    command_prefix: str = "python -m prospector.cli",
) -> Path:
    """rows: [{lead_id, name, category, city, rating, review_count, phone,
    wa_link, body, lead_type}]

    command_prefix is whatever the caller actually runs the CLI with, so the
    mark-sent command the page hands back can be pasted without editing."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # Mobiles first: the queue gets worked top down, and landlines are the ones
    # least likely to go anywhere.
    rows = sorted(rows, key=lambda r: r.get("line_type") == "landline")
    cards = "\n".join(_card(r) for r in rows)
    page = _TEMPLATE.replace("{{CARDS}}", cards)
    page = page.replace("{{COUNT}}", str(len(rows)))
    page = page.replace("{{CMD_PREFIX}}", json.dumps(command_prefix))
    landline = sum(1 for r in rows if r.get("line_type") == "landline")
    page = page.replace("{{LANDLINE}}", str(landline))
    page = page.replace("{{MOBILE}}", str(len(rows) - landline))
    out_path.write_text(page, encoding="utf-8")
    return out_path


def _card(r: dict) -> str:
    e = html.escape
    meta = " · ".join(
        part
        for part in [
            e(r.get("category") or ""),
            e(r.get("city") or ""),
            f"{e(str(r['rating']))}★ ({e(str(r['review_count']))})"
            if r.get("rating") and r.get("review_count")
            else "",
        ]
        if part
    )
    badge = (
        "<span class='badge no-site'>no website</span>"
        if r.get("lead_type") == "NO_SITE"
        else "<span class='badge bad-site'>weak website</span>"
    )
    if r.get("line_type") == "landline":
        badge += " <span class='badge landline'>landline — try calling</span>"
    lead_id = int(r["lead_id"])
    return f"""
    <article class="card{" is-landline" if r.get("line_type") == "landline" else ""}" data-id="{lead_id}">
      <header>
        <div>
          <h2>{e(r['name'])} {badge}</h2>
          <p class="meta">{meta}</p>
          <p class="meta">{e(r.get('phone') or '')}</p>
        </div>
        <label class="done">
          <input type="checkbox" class="sent-box" data-id="{lead_id}">
          <span>sent</span>
        </label>
      </header>
      <pre class="msg">{e(r['body'])}</pre>
      <a class="btn" href="{e(r['wa_link'])}" target="_blank" rel="noopener">Open in WhatsApp</a>
    </article>
    """


_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>WhatsApp queue</title>
<style>
  :root {
    --bg: #f6f7f8; --card: #fff; --ink: #16181d; --muted: #6b7280;
    --line: #e3e5e9; --accent: #1f8a4c; --accent-ink: #fff;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #14161a; --card: #1c1f25; --ink: #e9eaec; --muted: #9aa1ab;
      --line: #2a2e36; --accent: #29a35c; --accent-ink: #fff;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 24px 16px 140px; background: var(--bg); color: var(--ink);
    font: 15px/1.5 -apple-system, "Segoe UI", Helvetica, Arial, sans-serif;
  }
  .wrap { max-width: 760px; margin: 0 auto; }
  h1 { font-size: 20px; margin: 0 0 4px; }
  .lede { color: var(--muted); margin: 0 0 24px; }
  .card {
    background: var(--card); border: 1px solid var(--line); border-radius: 12px;
    padding: 16px; margin-bottom: 14px;
  }
  .card.is-done { opacity: .45; }
  .card header { display: flex; justify-content: space-between; gap: 16px; align-items: flex-start; }
  h2 { font-size: 16px; margin: 0 0 4px; }
  .meta { color: var(--muted); font-size: 13px; margin: 0; }
  .badge {
    font-size: 11px; padding: 2px 7px; border-radius: 999px; vertical-align: middle;
    border: 1px solid var(--line); color: var(--muted); font-weight: 500;
  }
  .badge.no-site { border-color: var(--accent); color: var(--accent); }
  .badge.landline { border-color: #b4762b; color: #b4762b; }
  .card.is-landline .btn { background: var(--muted); }
  .msg {
    white-space: pre-wrap; word-wrap: break-word; font: inherit;
    background: var(--bg); border: 1px solid var(--line); border-radius: 8px;
    padding: 12px; margin: 12px 0;
  }
  .btn {
    display: inline-block; background: var(--accent); color: var(--accent-ink);
    text-decoration: none; padding: 9px 16px; border-radius: 8px; font-weight: 600;
  }
  .done { display: flex; align-items: center; gap: 6px; color: var(--muted); font-size: 13px; white-space: nowrap; }
  .bar {
    position: fixed; left: 0; right: 0; bottom: 0; background: var(--card);
    border-top: 1px solid var(--line); padding: 12px 16px;
  }
  .bar .wrap { display: flex; gap: 12px; align-items: center; }
  code {
    flex: 1; background: var(--bg); border: 1px solid var(--line); border-radius: 8px;
    padding: 10px 12px; font-size: 12.5px; overflow-x: auto; white-space: nowrap;
  }
  button {
    background: var(--ink); color: var(--bg); border: 0; border-radius: 8px;
    padding: 10px 14px; font-weight: 600; cursor: pointer;
  }
</style>
</head>
<body>
<div class="wrap">
  <h1>WhatsApp queue — {{COUNT}} leads</h1>
  <p class="lede">Open each one, read it before you send, tick the box. Then copy the command at the bottom and run it to record the sends.<br>
  <b>{{MOBILE}} of these are mobiles.</b> The {{LANDLINE}} marked <i>landline</i> are almost certainly not on WhatsApp — call those instead.</p>
  {{CARDS}}
</div>
<div class="bar"><div class="wrap">
  <code id="cmd">nothing ticked yet</code>
  <button id="copy">Copy</button>
</div></div>
<script>
  const KEY = 'wa-queue-sent';
  const CMD_PREFIX = {{CMD_PREFIX}};
  let sent = [];
  try { sent = JSON.parse(localStorage.getItem(KEY) || '[]'); } catch (e) { sent = []; }

  function refresh() {
    const cmd = document.getElementById('cmd');
    cmd.textContent = sent.length
      ? CMD_PREFIX + ' mark-sent ' + sent.join(' ')
      : 'nothing ticked yet';
    document.querySelectorAll('.card').forEach(c => {
      c.classList.toggle('is-done', sent.includes(Number(c.dataset.id)));
    });
  }

  document.querySelectorAll('.sent-box').forEach(box => {
    const id = Number(box.dataset.id);
    box.checked = sent.includes(id);
    box.addEventListener('change', () => {
      sent = box.checked ? [...new Set([...sent, id])] : sent.filter(x => x !== id);
      try { localStorage.setItem(KEY, JSON.stringify(sent)); } catch (e) {}
      refresh();
    });
  });

  document.getElementById('copy').addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(document.getElementById('cmd').textContent);
      const b = document.getElementById('copy');
      b.textContent = 'Copied'; setTimeout(() => b.textContent = 'Copy', 1200);
    } catch (e) {}
  });

  refresh();
</script>
</body>
</html>
"""
