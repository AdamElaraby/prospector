# Prospector

Finds local businesses whose web presence is missing or broken, writes each one
a short message, and gets it in front of them — by email where an address can be
found, by WhatsApp click-to-send where one can't.

State lives in SQLite. Every command is safe to re-run: nothing is discovered,
drafted, or messaged twice.

---

> **Use responsibly.** This tool contacts real people at real businesses. You are
> responsible for complying with Google's Terms of Service, WhatsApp's policies,
> and the anti-spam and privacy laws where you and your recipients are. See
> [Responsible use](#responsible-use).

## The pipeline

```
find  ──▶  audit  ──▶  draft  ──▶  preview  ──▶  send  (email)
                                            └──▶  wa    (WhatsApp queue)
```

| Stage | What it does |
|---|---|
| `find` | Pulls businesses from Google Places API, or one of two Google Maps scrapers if there's no key. |
| `audit` | Loads each business's site, scores what's wrong with it, and harvests a contact email. Businesses with no site skip straight to qualified. |
| `draft` | One LLM call per lead. Facts in, short message out. Routes each lead to email or WhatsApp. |
| `preview` | Read the drafts before anything leaves the machine. |
| `send` | Sends the emails through Resend. **Dry run unless you pass `--yes`.** |
| `wa` | Writes a local HTML page of wa.me links you click through by hand. |

### Three lead sources

`--source` (or `search.source` in config) picks one; `auto` walks down the list.

| Source | What it is | Trade-off |
|---|---|---|
| `places_api` | Google Places API (New) | Structured, stable, supported. Needs a key and costs per search. |
| `gosom` | [gosom/google-maps-scraper](https://github.com/gosom/google-maps-scraper) (MIT, Go) | Free, and maintained by people who track Google's markup changes. Needs Go and a one-time build. **Best free option.** |
| `scraper` | Built-in Playwright scraper | Zero extra tooling. Reads Google's obfuscated DOM directly, so it's the first thing to break. |

`auto` = `places_api` if a key is set, else `gosom` if the binary is built, else
`scraper`.

Build the Go scraper once:

```bash
brew install go && make install-gosom
```

That clones the MIT source and compiles it into `./bin/` — nothing prebuilt is
downloaded. It returns better data than the built-in scraper: full addresses,
real Google `place_id`s (so its results dedupe cleanly against Places API ones
instead of colliding by name), and no missing fields in practice.

Size `search.max_results_per_query` honestly. Scroll depth is derived from it,
and scrolling deeper than you need is the difference between a 30-second run and
a 9-minute one.

### Why two channels

A business with **no website** has **no email address to find** — Google never
gives you one, and there's no site to scrape. Those are the highest-intent leads
in the whole list, and email cannot reach them. So they go to the WhatsApp
queue, using the phone number from their Maps listing.

Businesses with a **bad** website do have a site, which is where the email
address comes from. Those go out by email.

The routing is automatic. You don't pick.

---

## The web UI (easiest way to run it)

```bash
make ui
```

Opens a local control panel in your browser: fill in your details and keys,
click through the four pipeline steps, watch the output live, read and edit
every draft before it goes out, and open each WhatsApp chat with one click.

It runs the same commands documented below — nothing separate to learn, and you
can mix the two freely.

It binds to `127.0.0.1` only and prints a one-time token in its URL. Keep that
link to yourself: it unlocks your API keys and the ability to send mail. Requests
from any other origin are refused, so a random web page can't drive it behind
your back.

## Setup

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

```bash
.venv/bin/playwright install chromium
```

Or just `make setup`, which does all of the above.

Optionally, for the better free lead source:

```bash
brew install go && make install-gosom
```

```bash
cp .env.example .env
```

Then fill in `.env`:

| Key | Needed for | Notes |
|---|---|---|
| `OPENAI_API_KEY` | drafting | The only one you actually need. |
| `RESEND_API_KEY` | email | Only if you turn email on. |
| `GOOGLE_PLACES_API_KEY` | discovery | Optional — the free scraper works without it. |

Then make your own private copy of the config and put your name in it:

```bash
cp config.yaml config.local.yaml
```

`config.local.yaml` is gitignored and takes priority over `config.yaml`, which
stays as the committed template. **Put your real name, phone number and campaign
in the local file only** — anything in `config.yaml` is published with the repo.
The web UI always saves to the local file. Your name is the only field drafting
requires.

**You don't need a business to use this.** No company name, no domain, no
address. Leave those blank and the messages read as one person offering to build
a website — which is both the honest version and the one that gets replies.
Email is the only part that needs more, and it is off by default.

```bash
.venv/bin/python -m prospector.cli init
```

That creates the database and tells you which keys and config fields are still
missing. Or skip straight to `make ui` and do all of it in the browser.

---

## Running it

Set your niches and cities in `config.local.yaml` under `search:`, then:

```bash
.venv/bin/python -m prospector.cli find
```

```bash
.venv/bin/python -m prospector.cli audit
```

```bash
.venv/bin/python -m prospector.cli draft
```

```bash
.venv/bin/python -m prospector.cli preview --limit 5
```

Read them. Actually read them — this is the step that stops a bad batch.

```bash
.venv/bin/python -m prospector.cli send
```

That's a **dry run**: it prints who would get what and sends nothing. When the
list looks right:

```bash
.venv/bin/python -m prospector.cli send --yes --limit 20
```

**Use WhatsApp.** It needs no domain, no address, and no account:

```bash
.venv/bin/python -m prospector.cli wa
```

Opens a page of cards. Each has the message and an **Open in WhatsApp** button
that launches WhatsApp with the text already typed. Send, tick the box, and when
you're done the page hands you a `mark-sent` command to paste back.

---

## Day-to-day commands

```bash
.venv/bin/python -m prospector.cli stats
```

Counts by stage, qualified-lead breakdown, reply rate, and the top reasons leads
were skipped — that last one is how you tune your targeting.

```bash
.venv/bin/python -m prospector.cli show 42
```

Everything known about one lead: audit findings, contacts, drafted message.

```bash
.venv/bin/python -m prospector.cli mark-replied 42 51
```

**Run this the moment someone replies.** It permanently stops all further
contact with that lead.

```bash
.venv/bin/python -m prospector.cli suppress someone@example.com --reason "asked to stop"
```

Never contact this address, domain, or phone again. Checked before every draft
and every send. When someone asks you to stop, this is the command.

```bash
.venv/bin/python -m prospector.cli list --status SENT
.venv/bin/python -m prospector.cli export
```

---

## Tuning what counts as a lead

In `config.local.yaml`:

```yaml
qualify:
  bad_site_threshold: 4     # audit points needed to count as "bad"
  min_review_count: 3       # skip businesses nobody has reviewed
```

The audit assigns points per defect (`prospector/audit.py`, `ISSUE_WEIGHTS`).
A dead site is 5, no mobile viewport is 4, plain http is 3, a stale copyright
year is 2. Raise the threshold for fewer, more obviously broken leads; lower it
to cast wider.

Every issue also has plain-English phrasing in `ISSUE_TEXT`, and that phrasing —
not the raw score — is what gets handed to the drafting prompt. The message can
say *"your site loads over http so Chrome flags it as not secure"* because the
pipeline actually checked.

### On not making false claims

The whole pitch rests on the audit being right. Emailing someone that their site
is broken when it isn't is worse than never emailing them. Two classes of false
positive are specifically defended against, both found by testing this against
real businesses:

- **Certificate verification uses the OS trust store**, not Python's bundled CA
  list, which lags and produces `CERTIFICATE_VERIFY_FAILED` on sites every real
  browser loads. A trust gap on our end is never reported as their broken
  certificate. Genuine failures are still classified precisely — expired,
  self-signed, hostname mismatch, and incomplete chain are four different
  findings with four different severities, not one vague "bad SSL".
- **A site that blocks the audit is marked inconclusive, not broken.** Plenty of
  small-business sites sit behind Cloudflare and return 403 to anything without
  a browser fingerprint. Those leads are skipped with
  `site blocked the audit — check by hand`, because the response body is the
  WAF's page and parsing it would invent findings about a site never actually
  seen.

---

## Message quality

The drafting prompt is fed only verified facts: business name, category, Google
rating, review count, and the specific audit findings. It is told repeatedly not
to invent anything else, and drafts are rejected before they reach the database
if they contain an unfilled `[placeholder]` or run far over the word target.

Tune the voice in `config.local.yaml` under `llm.style_rules` and `llm.call_to_action`.
Swap providers with `llm.provider` (`openai` or `anthropic`) — both are wired up.

---

## Responsible use

Prospector is a tool for finding businesses you could genuinely help and writing
to them honestly. It is not a mass-messaging or spam tool, and using it as one is
against its purpose and, in most places, the law.

By using it you accept that **you alone are responsible** for how you use it,
including:

- **Google's Terms of Service.** Scraping Google Maps is against Google's terms.
  The Places API is the supported route. The scraper sources exist for
  personal, low-volume research; running them at scale is your risk to take.
- **WhatsApp and Meta's policies.** Messages are sent by hand, one at a time, on
  purpose. Do not automate sending, and stop the moment someone asks you to.
- **Anti-spam and privacy law** wherever you and your recipients are — for
  example CAN-SPAM (US), GDPR and PECR (EU/UK), CASL (Canada), and the UAE's
  PDPL and TDRA rules. Some require consent before any commercial message.
- **Honesty.** Never send a claim about someone's business that you haven't
  checked. The audit exists to make that possible; reading every draft before it
  goes out is still on you.
- **Respecting "no".** Use `mark-replied` and `suppress` immediately. A person who
  says stop is never contacted again.

The software is provided "as is", without warranty of any kind — see
[LICENSE](LICENSE). The authors accept no liability for any use of it.

## Legal and deliverability

Cold outreach is legal in most places when done correctly, and illegal when done
carelessly. Being one person rather than a company changes none of that.

**The short version: use WhatsApp and most of this doesn't apply to you.**
CAN-SPAM governs email, so if you never turn email on you never need a postal
address or a sending domain. That is why email ships disabled.

What's built in for when you do use email:

- **Unsubscribe.** Every email carries an opt-out line plus `List-Unsubscribe`
  headers, which Gmail and Yahoo now require of bulk senders.
- **Postal address.** CAN-SPAM requires a real physical address in commercial
  email — from individuals too, not only companies. It comes from
  `email_channel.postal_address` and there is no way to send without it. A home
  address or a cheap PO box both satisfy it.
- **Suppression list.** Checked before drafting and again before sending.
- **Dry run by default.** `send` shows you the batch; `send --yes` sends it.

What you have to handle yourself:

- **Use a separate sending domain**, not the one your real mail runs on. Verify
  SPF, DKIM and DMARC in Resend before the first send. Cold mail from an
  unauthenticated domain lands in spam and burns the domain permanently.
- **Warm up.** Start at ~10/day and climb over two weeks. `--limit` is your
  throttle.
- **GDPR** applies to businesses in the EU/UK. Legitimate-interest B2B outreach
  is generally defensible; consumer addresses are not.
- **WhatsApp is manual on purpose.** Meta's Cloud API needs pre-approved
  templates and prior opt-in, and the unofficial libraries get numbers banned.
  The click-to-send queue keeps you inside the rules.
- **Stop when asked.** `mark-replied`, or `suppress` for anything stronger.

---

## Costs

| Thing | Cost |
|---|---|
| Places API text search | ~$0.032 per request (20 results); the standing free credit covers thousands |
| gosom scraper | free; ~30s per query at a 15-result cap |
| Built-in Playwright scraper | free, slower, breaks when Google changes its markup |
| Drafting | roughly $0.01 per lead on a small model |
| Resend | free to 3,000 emails/month |
| WhatsApp | free |

---

## When something breaks

**Built-in scraper returns nothing or empty fields.** Google changed its markup,
or is showing a consent wall. Run `find --source scraper --headful` to watch it,
and update `CARD_SELECTORS` in `prospector/sources/maps_scraper.py`. Or sidestep
it: `make install-gosom` and use `--source gosom`, which someone else maintains.

**gosom runs take many minutes.** Scroll depth is sized from
`search.max_results_per_query`, so an inflated cap costs real time. If
`search.gosom.depth` is set to a non-zero value it overrides that sizing — set
it back to `0` for automatic.

**Everything gets skipped as "site is acceptable".** Your threshold is too high,
or — more often — the niche genuinely has good websites. Established trades in
big metros mostly do. Check `stats` for the skip breakdown, and try niches and
towns where a website is less of a given before you lower `bad_site_threshold`;
loosening the filter mostly buys you weaker pitches.

**Lots of "blocked the audit" skips.** Those sites sit behind a WAF. They are
not necessarily bad leads, they just can't be judged automatically — `list
--status SKIPPED` and eyeball them.

**Drafts fail validation.** The model is padding. Tighten `llm.style_rules` or
raise the model quality in `llm.model`.

**Emails land in spam.** Domain authentication, almost always. Check SPF/DKIM/
DMARC in Resend, cut daily volume, and shorten the messages.

## License

[MIT](LICENSE) © 2026 Rehan Ali
