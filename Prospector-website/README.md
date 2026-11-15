# Prospector

Finds local businesses whose website is missing, broken, or unable to sell for
them; scores whether each one is actually worth your time; writes each a short
message; gets it in front of them — by email where an address can be found, by
WhatsApp click-to-send where one can't — and then tracks which of those
conversations turned into money.

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
                                                   │
                                                   └──▶  pipeline / funnel  (what it earned)
```

| Stage | What it does |
|---|---|
| `find` | Pulls businesses from Google Places API, an Apify Actor, or one of two Google Maps scrapers. |
| `audit` | Checks each site twice — what is **broken** and what it **can't do for a customer** — then scores the business out of 100 and keeps the ones worth your time. |
| `draft` | One LLM call per lead, best-scoring first. Facts in, short message out. Routes each lead to email or WhatsApp. |
| `preview` | Read the drafts before anything leaves the machine. |
| `send` | Sends the emails through Resend. **Dry run unless you pass `--yes`.** |
| `wa` | Writes a local HTML page of wa.me links you click through by hand. |
| `pipeline` | Records what actually happened: conversation started, proposal requested, quote sent, won, lost. |
| `funnel` | Conversion per stage and revenue won — the only numbers that are money. |

Every stage reports its real position as it goes, so the web UI's progress bar
is an actual fraction of the work rather than a spinner. A stage that genuinely
cannot know its total up front says so, and the bar goes indeterminate instead
of inventing a percentage.

### Four lead sources

`--source` (or `search.source` in config) picks one; `auto` walks down the list.

| Source | What it is | Trade-off |
|---|---|---|
| `places_api` | Google Places API (New) | **The supported default.** Structured, stable, and it returns `websiteUri`, which is the field this whole campaign turns on. Needs a key and costs per search. |
| `apify` | An [Apify](https://docs.apify.com/api/v2) Actor, read through their REST API | Reaches things the Places API doesn't, and lets an Actor you already pay for feed this pipeline instead of a spreadsheet. Billed by Apify, so `auto` never picks it. |
| `gosom` | [gosom/google-maps-scraper](https://github.com/gosom/google-maps-scraper) (MIT, Go) | Free, and maintained by people who track Google's markup changes. Needs Go and a one-time build. **Best free option.** |
| `scraper` | Built-in Playwright scraper | Zero extra tooling. Reads Google's obfuscated DOM directly, so it's the first thing to break. |

`auto` = `places_api` if a key is set, else `gosom` if the binary is built, else
`scraper`. **Apify is never chosen automatically** — every run bills your Apify
account, so it has to be asked for by name.

#### Using an Apify Actor

Put `APIFY_API_TOKEN` in `.env`, then:

```bash
.venv/bin/python -m prospector.cli find --source apify
```

Prospector starts the run, waits for it, reads the dataset, and normalises each
item into the same lead shape every other source produces — so audit, scoring,
drafting and the pipeline all work exactly as before.

```yaml
search:
  apify:
    actor_id: "compass~crawler-google-places"   # username~actor-name
    query_field: "searchStringsArray"           # which input field takes the query
    max_field: "maxCrawledPlacesPerSearch"      # "" if the Actor has no cap
    input: {}                                   # anything else the Actor takes
    dataset_id: ""                              # re-import a run you already paid for
```

Actors disagree about field names, so each value is read from a list of likely
spellings (`title`/`name`, `totalScore`/`rating`, `reviewsCount`/`reviews`, …).
Two things are deliberate: the search query is always written over whatever is
in `input`, so a stale query in config can't silently hijack every run and make
each niche return the same businesses; and a Google Maps link found in `url` is
never mistaken for the business's own website, because that would make a lead
with no site look like one that has one.

Set `dataset_id` to read a run you already did on Apify rather than paying for
a new one. Nothing retries a failed run — a retry loop against a metered API is
how a $5 test becomes a $50 one.

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
click through the four pipeline steps, watch a real progress bar and the live
output, browse leads grouped by the day they were found or summarised by day
and category, read and edit every draft before it goes out, open each WhatsApp
chat with one click, and record what each conversation turned into on the
Pipeline tab.

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
| `GOOGLE_PLACES_API_KEY` | discovery | The supported default — the free scraper works without it. |
| `APIFY_API_TOKEN` | discovery | Only if you pick `--source apify`. Billed by Apify. |

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

Counts by stage, lead grades, reply rate, the top reasons leads were skipped
(that one is how you tune your targeting) and the revenue funnel.

```bash
.venv/bin/python -m prospector.cli show 42
```

Everything known about one lead: why it scored what it scored, what is broken,
what the site can't do for a customer, contacts, the drafted message, where the
deal has got to, and a full history:

```
history
  2026-10-16 09:12  found
  2026-10-16 09:40  website audited
  2026-10-16 10:02  whatsapp message drafted
  2026-10-17 11:15  whatsapp message sent
  2026-10-17 14:30  contacted
  2026-10-17 16:04  quoted
```

The history is assembled from the timestamps each stage already writes, not
from a separate event log, so it cannot drift out of step with the real state.

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
.venv/bin/python -m prospector.cli list
```

Every lead, newest batch first, under a heading for the day it was found:

```
2026-10-17 (today) ─────────────────────────────────────────────
id  name                category           grade  score  status   time
1   Ace Plumbing        Plumber            A      92     AUDITED  12:00
5   Hill Country Ro…    Roofing contractor A      83     AUDITED  12:00

2026-10-16 (yesterday) ─────────────────────────────────────────
3   Bright Smile Dental Dentist            A      88     AUDITED  13:12
```

Or collapse it to one row per group:

```bash
.venv/bin/python -m prospector.cli list --by date
.venv/bin/python -m prospector.cli list --by category
```

```
category             found  qualified  contacted  avg score
Plumber                 14         11          8         81
Roofing contractor      12          9          6         77
Laundromat              19          2          0         41
```

Sorted by average lead score, so the top rows are the trades worth searching
more of and the bottom rows are the ones to stop searching. The by-date version
answers the same question about a day's run: was Tuesday's search worth it?

Filters stack, and apply to the summaries as well as the list:

```bash
.venv/bin/python -m prospector.cli list --since 2026-10-14
.venv/bin/python -m prospector.cli list --on 2026-10-16 --status AUDITED
.venv/bin/python -m prospector.cli list --category roof --by date
```

Dates are stored UTC and grouped in **local** time, because "which day did I
find these?" is a question about your day — a lead found at 1am in Dubai
belongs to that morning's batch, not the previous one.

```bash
.venv/bin/python -m prospector.cli export
```

The CSV carries `created_at`, `audited_at`, `sent_at`, the pipeline timestamps,
and the `found_day` / `group_category` columns the grouping uses, so a
spreadsheet pivots the same way.

---

## Two different questions about a website

`audit` asks both, and keeps the answers apart on purpose.

**Is it broken?** — technical defects, in `prospector/audit.py` (`ISSUE_WEIGHTS`).
A dead site is 5 points, no mobile viewport is 4, plain http is 3, a stale
copyright year is 2.

**Can it sell?** — commercial opportunity, in `prospector/opportunity.py`. Four
capabilities a small local business actually converts on:

| Capability | Why it is worth money |
|---|---|
| Online booking | A customer can commit to a time at 11pm, which is when people look. Detected from booking-platform links (Calendly, Booksy, Fresha, OpenTable, Jobber, …) as well as buttons. |
| A quote or enquiry route | A customer can ask a price without phoning. A real enquiry form counts; a search box or a newsletter signup does not. |
| Clear service information | The site says plainly what is offered. A nav label alone is not information. |
| An obvious way to make contact | A tap-to-call link, an email, a contact page, or a form. |

The distinction matters in the pitch. *"Your certificate expired"* is a
complaint about a mistake; *"a customer can't book you at 11pm"* is money they
are not collecting. The second one sells, and a site can be technically
flawless and still have every gap. Detection errs towards saying a capability
**is** present, because pitching someone a booking system they already have
destroys the message.

## What makes something a lead

**Having no website is not a qualification.** A closed barber shop with two
reviews also has no website, and messaging it costs the same as messaging a
busy one — but it will never reply, and if it does it cannot pay. So every
business is scored out of 100 across four dimensions (`prospector/qualify.py`)
and has to clear a bar on three of them *independently*, because a high total
built entirely out of one dimension is not a lead:

| Dimension | Out of | What it reads |
|---|---|---|
| Activity | 30 | Is this a going concern? Review count, rating, a working phone, a claimed listing. |
| Problem | 30 | Is there something demonstrated worth fixing — real technical defects, missing commercial capability, or no site at all? |
| Ability to pay | 25 | Review volume as a revenue proxy, plus the trade: a roofer buys a website out of one job, a laundromat does not. |
| Deliverable | 15 | Could one person realistically build this? A live store is a migration, not a brochure site. A hospital is not a job at all. |

```yaml
qualify:
  min_lead_score: 55        # overall bar, out of 100
  min_activity_score: 12    # out of 30 — is anyone home?
  min_problem_score: 10     # out of 30 — is there anything to fix?
  min_review_count: 3       # skipped before a single request is spent
```

Raise `min_lead_score` for fewer, better leads. The trade lists are overridable
(`high_value_categories`, `low_value_categories`, `undeliverable_patterns`);
leave them out to use the built-in defaults.

`show 42` prints the whole breakdown — every component, every reason it scored,
and every reason it did not — so a skipped lead always tells you *which* of the
four things was missing rather than a single unhelpful number.

Two cases deliberately score **zero** on the problem dimension and are therefore
never leads: a site that works and does its job, and a site that blocked the
audit. Nothing was proven about the second one, and a pitch built on an
unproven problem is a false claim.

Every issue and every gap also has plain-English phrasing (`ISSUE_TEXT`,
`GAP_TEXT`), and that phrasing — not the raw score — is what reaches the
drafting prompt. The message can say *"your site loads over http so Chrome
flags it as not secure"* because the pipeline actually checked.

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

## Measuring money, not leads

Leads collected is a number that only ever goes up. It tells you nothing. The
pipeline records what actually happened, and nothing in it is inferred — you
type it, because nothing the machine can observe tells it a deal closed.

```
reviewed ──▶ conversation started ──▶ proposal requested ──▶ quote sent ──▶ won
                                                  └──▶ lost (from anywhere)
```

```bash
.venv/bin/python -m prospector.cli pipeline contacted 42
.venv/bin/python -m prospector.cli pipeline proposal 42
.venv/bin/python -m prospector.cli pipeline quoted 42 --amount 450
.venv/bin/python -m prospector.cli pipeline won 42 --amount 500
.venv/bin/python -m prospector.cli pipeline lost 51 --reason "went with a cousin"
```

```bash
.venv/bin/python -m prospector.cli funnel
```

```
stage                  reached   of previous
reviewed                     3             —
conversation started         3          100%
proposal requested           2           67%
quote sent                   1           50%
won                          1          100%

won 1 deals, $500 · average $500
close rate 33.3% of conversations started
```

The same buttons are on every lead in the web UI, and the Pipeline tab draws
the funnel and the revenue.

Four decisions worth knowing about:

- **Lead status and sales stage are separate fields.** `SENT` says a message
  left the machine; `conversation started` says a human replied. Conflating
  them would make the funnel count outreach as traction.
- **Reaching a stage backfills the earlier ones.** A deal you only remember to
  log once it is won must not leave the funnel claiming nobody was contacted.
- **Automatic updates only ever move a lead forward.** Sending a message or
  marking a reply can push a lead to `contacted`; neither can drag a deal you
  have already quoted back down the ladder.
- **The funnel counts who ever reached each stage**, not who is sitting there
  now. Counting current stage would empty the `contacted` column the moment
  those conversations became quotes, which reads as though outreach stopped
  working.

Set the currency once, in `config.local.yaml`:

```yaml
pipeline:
  currency: "$"
```

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
| Apify Actor run | whatever that Actor charges — check its page before the first run; `dataset_id` re-reads a run you already paid for |
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

**Everything gets skipped.** Run `stats` and read the skip breakdown — it names
which of the four dimensions failed, and each one has a different fix. *"No
problem worth pitching"* means the niche genuinely has good websites, which
established trades in big metros mostly do; try somewhere a website is less of
a given. *"Not enough sign of an active business"* means you are searching a
category full of dead listings. *"Too big or too regulated"* means the niche
itself is wrong. Lowering `min_lead_score` is the last resort, not the first —
it mostly buys you weaker pitches.

**The progress bar sits still.** If it says `23 so far` with no total, the
stage genuinely cannot know how much work there is (a scraper paging until
Google stops) and is still working. If it is stuck on one business, that site
is timing out — `audit.timeout_seconds` caps it.

**Lots of "blocked the audit" skips.** Those sites sit behind a WAF. They are
not necessarily bad leads, they just can't be judged automatically — `list
--status SKIPPED` and eyeball them.

**Apify: "run is still RUNNING after 900s".** The run keeps going and keeps
billing on Apify's side — it was not cancelled. Raise
`search.apify.timeout_seconds`, or abort it in the Apify console and re-import
the partial dataset with `search.apify.dataset_id`.

**Drafts fail validation.** The model is padding. Tighten `llm.style_rules` or
raise the model quality in `llm.model`.

**Emails land in spam.** Domain authentication, almost always. Check SPF/DKIM/
DMARC in Resend, cut daily volume, and shorten the messages.

## License

[MIT](LICENSE) © 2026 Rehan Ali
