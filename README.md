# NCI: Network Context Intelligence

[![Python](https://img.shields.io/badge/Python-3.9%2B-blue?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![Dependencies](https://img.shields.io/badge/Dependencies-stdlib%20only-brightgreen?style=flat-square)](#-quick-start)
[![Storage](https://img.shields.io/badge/Storage-SQLite-lightgrey?style=flat-square&logo=sqlite)](https://www.sqlite.org/)
[![Docker](https://img.shields.io/badge/Docker-ready-2496ED?style=flat-square&logo=docker&logoColor=white)](#-quick-start)
[![Status](https://img.shields.io/badge/Status-Pilot%20%2F%20Demo-orange?style=flat-square)](#%EF%B8%8F-known-limits)

## About
👋 NCI is a **Teams-style full-stack workspace** that turns everyday network-operations chat into **traceable, time-aware knowledge**. It tracks every message, links it to devices and sites, captures expert know-how with verbatim quotes, and answers questions only from sources you are allowed to see.

- 📍 Domain: Network operations / transport (devices, sites, firmware, incidents)
- 🧠 Core idea: An expert's know-how outlives their chat history
- 🔒 Privacy: Access control is applied before retrieval, never after
- 🧰 Runs on: Python 3.9+ standard library, SQLite, optional OpenAI / GAIK

## Featured Capabilities
- 💬 **Teams-style workspace**: Activity, Chat (1:1 and group), Teams channels with threads, Calendar, Calls, Files, and the NCI app
- 🛰️ **NCI tracking on every message**: Links messages to devices (`Tracking · CAB-17`) and captures knowledge (`NCI tracked · C-009`)
- 🤖 **NCI chat**: Answers with Sources, Related information, In conversations, Risks to keep in mind, and a Timeline
- 🕰️ **Timeline**: Inventory changes, conversations, captured knowledge, effective dates and review dates for any device or site
- ⚠️ **Risks**: Validation gaps, unverified causes, stale items, knowledge resting on one person, unanswered questions
- 🔌 **Teams integration**: Outgoing webhook (@mention bot) and Microsoft Graph passive capture

## Quick Start
```bash
python server.py                  # stdlib only, Python 3.9+  ->  http://localhost:8080
python tests/test_workspace.py    # chats, receipts, NCI chat, tracking, timeline, risks (no extra packages)
python tests/test_nci.py          # needs: pip install gaik   (+ tests/test_orchestration.py)
docker build -t nci . && docker run -p 8080:8080 -v nci:/data nci
```

> **Upgrading from an earlier version?** Press **↻ Replay demo chat** in #Transport once, so the threads and demo chats load.

## Tech & Integrations
- 🧰 Languages: Python, JavaScript, HTML/CSS
- 🗄️ Storage: SQLite bitemporal assertion store
- 🤖 LLM (optional): OpenAI (`gpt-4o-mini` default), GAIK curator (`gaik[llm-anthropic]`)
- 🔗 Integrations: Microsoft Teams outgoing webhook, Microsoft Graph change notifications
- 🇫🇮 Language support: FinnishTextProcessor (libvoikko optional)

---

## 🚀 Using It
- **Teams-style workspace**: Activity (mentions, replies, reactions, "NCI tracked your message"), Chat (1:1 and group chats, new chat
  with the pencil), Teams (channel posts with subjects, threaded replies, Start a post), Calendar, Calls, Files, and the NCI app.
  Pick who you are from the avatar (top right). Open a second browser window as another person to chat live.
- **Messages behave like Teams**: Enter sends, Shift+Enter adds a line, `@` mentions people, emoji, hover a message to react.
  Read receipts: ◌ sending, ✓ sent, purple ✓✓ seen by everyone (grey ✓✓ when only some have seen it; hover for who).
  Typing indicators show who is writing.
- **NCI tracking on every message**: a chip under a message shows what NCI linked it to (`Tracking · CAB-17`) or captured
  (`NCI tracked · C-009`). Hover a message and click the NCI icon for details: read receipts, linked devices, captured knowledge.
  Each chat and channel also has an **NCI tracking** tab.
- **NCI chat** (pinned at the top of Chat, or the floating button): ask anything. Each answer shows **Sources** (click a quote to jump
  to the original message, highlighted), **Related information**, **In conversations**, **Risks to keep in mind**, and a
  **Timeline** of the topic. Your questions are logged; unanswered ones show up as knowledge gaps in Risks.
- **NCI app** (left rail): Search, Knowledge (confirm, dispute, expire, reconfirm), **Timeline** (any device or site: inventory changes,
  conversations, captured knowledge, when it takes effect, review dates), **Risks** (organisation level: validation gaps, unverified
  causes, stale items, knowledge resting on one person, unanswered questions), Monitoring, Orchestration.
- Type `@NCI ...` in a channel post or reply for an answer card in the thread.
- **Privacy**: you only see messages from channels and chats you are in. Knowledge from elsewhere appears with roles instead of
  names, and no quotes. Other people's private chats never appear in your search or timelines.

## 🌟 Showcase: Erkki's Know-how Outlives His Chat
**Erkki** (Senior transport expert) is the main knowledge holder. In the demo chat he corroborates the workaround and, before retiring,
shares four rules of thumb about HEL-DC-07 (m14-m17). NCI captures each as an *Expert know-how* assertion (C-005..C-008), scoped to the devices named.

1. Click **Next-year demo** in the Teams sidebar (or: replay the chat, pick **Oskari** from the person menu, set **Time** to +1 year).
2. Oskari was never in #Transport. Ask the floating NCI button: "Why does the node go offline for a while after a restart?"
3. NCI assumes TRN-042, surfaces Erkki's still-valid patch-cord tip, says the firmware-scoped workaround no longer applies (device runs 8.4.2),
   warns that the knowledge is a year old, names people by role only, and shows no raw chat.
4. Ask "What does Erkki know about HEL-DC-07?" for everything he contributed, or "Give me a handover brief for HEL-DC-07" for the full site picture.
5. NCI > Monitoring > **Who holds the knowledge** shows that most assertions rest on Erkki alone. Use NCI > Orchestration to reconfirm stale items.

## 🏗️ Architecture
```
Teams / Graph / Webhook / UI  ->  POST message
  -> FAST PATH (rules, ms): normalise -> resolve entities (+thread context) -> classify -> consolidate
  -> SLOW PATH (optional, GAIK): SourceNormalizer text -> KnowledgeCurator fact units -> quote verified verbatim
     inside ONE message -> bound to entities and time by NCI -> Observed/Corroborated, never above Verified
  -> bitemporal assertion store (SQLite): valid_from, valid_to, learned_at, scope, exclusions, quotes, views
  -> Agent Context API: entity + firmware + time filter, disputed/expired excluded, RelevanceGate -> "not covered"
```
Authors, roles and timestamps always come from chat, never from the model. "Approved for automation" is intentionally unreachable.

## ⚙️ Configuration

### Turn on OpenAI (conversational NCI chat + tracking of new messages)
Copy `.env.example` to `.env`, set `OPENAI_API_KEY` (optionally `OPENAI_MODEL`, default `gpt-4o-mini`), restart. No extra packages.
- **NCI chat**: NCI retrieves sources with access control applied, OpenAI writes the answer from those sources only, and NCI
  discards any answer that cites a source it did not retrieve or names a device, version, number or person not in the sources
  (you then get the rule-based answer, with the reason).
- **Tracking**: every new channel or chat message that the rules do not catch is sent to OpenAI to extract knowledge items. An item is
  kept only if its quote is copied verbatim from the message, and it starts as *Observed* (unreviewed). Without a key, messages are
  still tracked (stored, searchable, linked to devices) and the rules still capture knowledge.

Spend is capped by `NCI_DAILY_TOKEN_BUDGET` and a circuit breaker; NCI > Orchestration shows calls and tokens.

### Turn on the GAIK curator
`pip install "gaik[llm-anthropic]"`, copy `.env.example` to `.env`, set `NCI_LLM_PROVIDER` and the key. After a thread goes quiet
(3 s) the curator runs and NCI posts a card with what it added. `POST /api/curate` runs it on demand. Dashboard shows health.

### Orchestration (what GAIK tools do what)
Open **NCI > Orchestration**: pipeline components with state, token budget and circuit breaker, key checklist (set or not set), retrieval gate calibration, items needing reconfirmation and the visibility policy.
- **Wired**: SourceNormalizer, KnowledgeCurator, quote_in_text, Embedder + VectorStore (with an SQLite embedding cache), Ranker (weighted RRF), RelevanceGate, FinnishTextProcessor, AnswerGenerator, usage records.
- **Not wired on purpose**: PgVectorStore, LLMJudge, RAGWorkflow, Transcriber.

## 🔌 Connect Real Microsoft Teams
1. **@mention bot (easy, per team)**: Teams > team > Manage team > Apps > Create an outgoing webhook. Callback URL
   `https://<public-host>/api/teams/outgoing`. Put its security token in `TEAMS_WEBHOOK_SECRET`. The HMAC signature is mandatory.
   Needs a public HTTPS URL (e.g. a tunnel or a cloud host). Replies are shown in the channel.
2. **Passive capture (Microsoft Graph)**: register an Entra app, get **tenant admin consent** for `ChannelMessage.Read.All`,
   create a subscription on `/teams/{id}/channels/{id}/messages` with notificationUrl `https://<public-host>/api/graph/notify`
   and your `clientState`. The endpoint answers the validation handshake and verifies `clientState`. Subscriptions expire
   (about an hour for chat-style resources without encryption); renewal and token refresh are NOT implemented here.
   Retention, privacy notice and works-council/GDPR review are yours to do before capturing colleagues' messages.

## 📁 Project Structure
```
nci-tool/
├── server.py           # HTTP server, API, rules, entity inventory
├── orchestrator.py     # Pipeline orchestration, budget, circuit breaker
├── gaik_bridge.py      # GAIK curator integration
├── llm.py              # OpenAI client
├── static/index.html   # Teams-style web UI
├── data/               # Demo chats, members, roles, Teams chat
├── tests/              # test_workspace, test_nci, test_orchestration
├── Dockerfile
├── requirements.txt
└── .env.example
```
