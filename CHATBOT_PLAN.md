# Round Treasury UK — Client Onboarding Chatbot: Detailed Design Plan

> **Version:** 1.0  
> **Date:** May 2026  
> **Scope:** Conversational chatbot covering pre-sales, onboarding, and post-onboarding support for Round Treasury UK

---

## 1. Executive Summary

This plan describes a text-based chatbot embedded on the Round Treasury website (and optionally in Slack/WhatsApp) that serves three user segments:

| Segment | Who they are | Primary need |
|---|---|---|
| **Prospects** | Founders, CFOs, VC operators new to Round | Understand services, pricing, and fit |
| **Active Clients** | Signed-up users in onboarding (Day 1–Week 2) | Guided setup, troubleshooting, next steps |
| **Established Clients** | Users post-onboarding | Feature queries, compliance questions, escalation |

The bot handles routine queries autonomously and escalates complex or sensitive issues to a human representative (the owner) via a Slack channel notification or email handoff.

---

## 2. Chatbot Objectives

1. Reduce time-to-first-response for inbound queries from hours to seconds.
2. Guide prospects through Round's value proposition and funnel them toward signup or a demo.
3. Walk new clients through the Day 1 → Week 2 onboarding timeline without requiring human hand-holding.
4. Answer compliance and regulatory questions accurately using Round's documented framework.
5. Escalate unresolvable, high-sensitivity, or sales-critical conversations to the human representative with full context attached.

---

## 3. User Identification & Routing

At session start, the bot presents a short routing menu to personalise the conversation:

```
Hi! I'm Rondo, Round Treasury's onboarding assistant.

Are you:
  [A] Exploring Round for the first time
  [B] A new client getting set up
  [C] An existing client with a question
```

The answer sets the conversation context and unlocks the relevant knowledge branch. Users can re-route at any time by typing "main menu" or "start over".

---

## 4. Core Functional Modules

### 4.1 Prospect Module (Segment A)

**Purpose:** Convert curious visitors into signups or demo bookings.

**Capabilities:**
- Explain Round's four core products (Treasury, Accounts Payable, Payroll, Multi-Entity).
- Describe key metrics (4× average yield, 75% invoice processing reduction, 5 days to automation).
- Clarify who Round is for (founders, CFOs, VC operators of high-growth companies).
- Explain the free tier and refer to the pricing page for full details.
- Present a comparison of Round vs. traditional treasury management.
- Capture lead details (name, company, email) and book a demo or start signup.
- Handle objections (security, cost, integration complexity).

**Example questions handled:**

| User question | Bot approach |
|---|---|
| "What does Round actually do?" | Explain the four-module platform with concrete examples |
| "How is Round different from just using a bank?" | Highlight multi-bank aggregation, yield optimisation, and automation |
| "Is Round suitable for a 10-person startup?" | Confirm fit; mention founders/CFOs as primary users |
| "What yield can I expect on my idle cash?" | Quote "up to 5% via BlackRock Money Market Funds with next-day liquidity" |
| "How quickly can we get started?" | Explain the 5-day onboarding timeline and one-click signup |
| "Do you have a free plan?" | Confirm free tier for unified treasury view; direct to /pricing for full details |
| "Which banks do you connect to?" | State "2,000+ UK and EU banks via Open Banking" |

---

### 4.2 Active Onboarding Module (Segment B)

**Purpose:** Guide clients through the three-phase onboarding journey step by step.

**Onboarding Timeline Reference:**

```
Day 1   → Connect banks & ERPs; first yield-earning deposit
Week 1  → Approval workflows active; first automated payment runs; ledger sync starts
Week 2  → Majority automation achieved; improved returns; reduced manual workload
```

**Capabilities:**
- Confirm which onboarding phase the client is in.
- Provide step-by-step instructions for bank connection via Open Banking (~30 seconds per bank).
- Explain KYB (Know Your Business) verification and expected timeline (typically 3 business days).
- Walk through Xero / NetSuite ERP integration setup.
- Guide Slack approval workflow configuration.
- Explain how to set approval thresholds, payment schedules, and cash minimums.
- Troubleshoot common setup issues (bank connection failures, ERP sync delays).
- Escalate to human rep if blockers are unresolvable within two bot turns.

**Example questions handled:**

| User question | Bot approach |
|---|---|
| "How do I connect my HSBC account?" | Step-by-step Open Banking connection walkthrough |
| "My KYB hasn't been approved yet — it's been 2 days" | Confirm 3-business-day SLA; offer to escalate if Day 3 passes |
| "How do I link Xero?" | Walk through two-way sync setup; link to integration docs |
| "How do I set up Slack approvals?" | Explain the approval workflow builder; step-by-step config |
| "When will my first automated payment run?" | Reference Week 1 milestone; check if workflows are active |
| "I can't see my second bank account" | Troubleshoot Open Banking reconnection; escalate if unresolved |
| "What's the deadline to submit a withdrawal for same-day clearing?" | "Requests submitted by 10:30am typically clear same-day" |

---

### 4.3 Established Client Module (Segment C)

**Purpose:** Answer ongoing operational, compliance, and feature questions for live clients.

**Capabilities:**
- Answer compliance and regulatory questions (FCA, FSCS, ISO 27001, audit trails).
- Explain fund security and segregation model.
- Clarify FX processing and multi-currency payment flows.
- Describe how to build new Agentic Workflows.
- Explain the Autonomous Payroll feature.
- Answer questions about the Multi-Entity module.
- Direct billing and pricing queries to the appropriate page or human rep.
- Escalate account-specific, sensitive, or unresolvable queries to the human rep.

**Example questions handled:**

| User question | Bot approach |
|---|---|
| "Are my funds protected if Round closes?" | Explain asset segregation with regulated custodians; FSCS multi-bank protection |
| "What happens if I need to withdraw money urgently?" | Confirm same-day clearing if submitted before 10:30am; next-day liquidity for MMFs |
| "Is Round FCA regulated?" | Explain appointed representative status under Wealthkernel (FRN: 723719); ISO 27001 |
| "How does the audit trail work?" | Describe real-time immutable logs; multi-layer permission controls |
| "Can I run payroll in multiple currencies?" | Confirm multi-currency support; describe payroll automation flow |
| "What is the Agentic Workflow Builder?" | Explain plain-English workflow description → Round builds → approval → autonomous execution |
| "Who can approve payments in our team?" | Explain multi-layer permission controls and approval threshold configuration |
| "Is my banking data shared with third parties?" | Explain Open Banking standards; credentials never stored; data not sold |

---

## 5. Escalation Logic

The bot escalates to the human representative when:

1. **The query is account-specific** — requires access to client account data the bot cannot see.
2. **The query is unresolved after 2 attempts** — bot explicitly offers handoff.
3. **The query involves a complaint or a dispute** — always escalate immediately.
4. **The query involves investment advice** — bot states it cannot advise; directs to a financial advisor and offers human rep contact.
5. **The user explicitly requests a human** — honour immediately, no friction.
6. **KYB delays beyond SLA** — escalate with client name and company for rep follow-up.
7. **Pricing negotiation or enterprise deal** — capture lead details and flag for rep.

**Escalation handoff message (bot to user):**
```
I want to make sure you get the best answer here — let me connect you with 
a member of the Round team. They typically respond within a few hours via 
your dedicated Slack channel.

Can I take your name and company so they have context when they reach out?
```

**Escalation notification (bot to human rep — via Slack or email):**
```
🔔 Escalation from Rondo Chatbot
User: [name], [company]
Segment: [Prospect / Active Client / Established Client]
Issue: [last 3 bot turns summarised]
Urgency: [Normal / High — complaint/KYB overdue]
Contact: [email if provided]
```

---

## 6. Compliance & Sensitive Topic Handling

The bot must handle these topics carefully and consistently:

### Capital at Risk Disclaimer
Whenever yield or investment returns are discussed, the bot appends:
> "Please note: your capital is at risk when investing. Round recommends consulting a qualified financial advisor before making investment decisions."

### Regulatory Questions
- Round Financial Limited: FRN 995009, registered England & Wales, Company #14609702.
- Appointed Representative of Wealthkernel Limited (FRN: 723719).
- ISO 27001:2022 certified.
- Works exclusively with FCA-regulated financial partners.
- FSCS protection available via multi-bank savings diversification.

### Data & Privacy
- Bank login credentials are never stored.
- Open Banking standards compliant.
- Two-factor authentication required.
- No selling of user data to third parties.

### Out-of-Scope Deflections
The bot will not:
- Provide specific investment advice ("should I invest X in MMFs?").
- Comment on competitors.
- Make commitments on pricing or contract terms (escalates to rep).
- Discuss ongoing legal or regulatory investigations.

---

## 7. Conversation Design Principles

| Principle | Application |
|---|---|
| **Concise first** | Bot answers should be ≤ 3 sentences unless a step-by-step is required |
| **Action-oriented** | Every response ends with a next step or a question |
| **Jargon-aware** | Define terms on first use (e.g., "KYB — Know Your Business verification") |
| **Human-voiced** | Friendly, professional tone; no robotic phrasing |
| **Graceful fallback** | If the bot doesn't know, it says so and offers to escalate — never fabricates |
| **Context-persistent** | Bot remembers the user's segment and prior answers within the session |

---

## 8. Sample Full Conversation Flows

### Flow 1 — Prospect converting to signup

```
Bot:  Hi! I'm Rondo, Round Treasury's onboarding assistant.
      Are you: [A] Exploring Round  [B] New client getting set up  [C] Existing client

User: A

Bot:  Great! Round is an AI-powered treasury and finance automation platform 
      for high-growth businesses. In a nutshell: it puts your idle cash to work, 
      automates payments and payroll, and gives you a real-time view across all 
      your bank accounts. What would you like to know more about?

      [1] How Round works  [2] Pricing  [3] Security & compliance  [4] Book a demo

User: How much yield can I earn?

Bot:  Round earns you up to 5% yield through BlackRock Money Market Funds — 
      that's typically 4× what a standard business savings account pays — with 
      next-day liquidity so your cash is never locked away.
      ⚠️ Your capital is at risk when investing. We recommend consulting a 
      financial advisor for investment decisions.
      Want to see how quickly you could get set up? [Yes / Book a demo / Ask something else]

User: Yes

Bot:  Signing up takes one click and bank connections take around 30 seconds each 
      via Open Banking. Most clients reach majority automation within 5 days.
      Ready to start? → [Sign up free] or would you prefer [Book a demo call]?

User: Book a demo

Bot:  Perfect! Can I take your name, company, and email address and I'll arrange 
      for a member of the Round team to reach out?
```

---

### Flow 2 — Active client stuck on KYB

```
Bot:  Hi! I'm Rondo. Are you: [A] Exploring  [B] New client  [C] Existing client

User: B

Bot:  Welcome! Where are you in the setup process?
      [1] Just signed up — connecting banks  
      [2] Banks connected — setting up workflows  
      [3] KYB / verification question  
      [4] Something else

User: 3 — my KYB hasn't been approved and it's been 4 days

Bot:  I'm sorry for the wait — KYB verification typically completes within 
      3 business days, so 4 days is outside our usual timeline. 
      Let me connect you with someone from the Round team to look into this directly.
      Can you confirm your company name and the email you signed up with?

User: Acme Ltd, hello@acme.com

Bot:  Thank you! I'm flagging this now as high priority for our team — 
      you should hear back within a few hours via your dedicated Slack channel or email.
      Is there anything else I can help you with in the meantime?
```

---

### Flow 3 — Established client compliance query

```
User: What happens to my money if Round shuts down?

Bot:  Your funds are fully protected regardless of Round's operational status. 
      Here's how:
      • Assets are held with regulated custodians and segregated from Round's 
        own balance sheet — they are always legally yours.
      • Through our Vault feature, cash can be spread across 100+ FSCS-protected 
        savings accounts, giving you protection at each institution.
      • You retain direct access to your funds at all times.
      Round Financial Limited operates as an appointed representative of 
      Wealthkernel Limited (FRN: 723719), an FCA-regulated firm.
      Does that answer your question, or would you like more detail on any part?
```

---

## 9. Technical Architecture Recommendations

### Deployment Channels
| Channel | Priority | Use Case |
|---|---|---|
| Website widget (roundtreasury.com) | Primary | Prospect capture, general queries |
| Dedicated Slack channel per client | Secondary | Onboarding support (Round already uses this model) |
| WhatsApp | Optional | Alert-style notifications only |

### Core Components
```
┌─────────────────────────────────────────────────┐
│                  RONDO CHATBOT                  │
├─────────────────┬───────────────────────────────┤
│  NLU Engine     │  Intent classification         │
│                 │  Entity extraction (company,   │
│                 │  email, KYB status, etc.)       │
├─────────────────┼───────────────────────────────┤
│  Knowledge Base │  Round services & features     │
│                 │  Compliance & regulatory FAQs   │
│                 │  Onboarding step library        │
│                 │  Escalation triggers            │
├─────────────────┼───────────────────────────────┤
│  Session Memory │  User segment (A/B/C)          │
│                 │  Conversation history           │
│                 │  Collected details (name, co.)  │
├─────────────────┼───────────────────────────────┤
│  Escalation     │  Slack webhook → human rep     │
│  Engine         │  Email fallback                 │
│                 │  Priority tagging               │
└─────────────────┴───────────────────────────────┘
```

### Recommended Stack
- **LLM backbone:** Claude (Anthropic) with a system prompt seeded from this knowledge base — provides nuanced, accurate responses without rigid decision trees.
- **Orchestration:** LangChain or a lightweight custom agent loop.
- **Website widget:** Intercom, Crisp, or a custom React embed.
- **Escalation:** Slack Incoming Webhooks + email via SendGrid.
- **Analytics:** Track intents, escalation rate, CSAT per session.

---

## 10. Knowledge Base Training Sources

The following sources should be used to populate and keep the knowledge base current:

| Source | Content |
|---|---|
| roundtreasury.com | Product pages, pricing, features, onboarding timeline |
| roundtreasury.com/security | Compliance, ISO 27001, FCA status, data handling |
| Wealthkernel FCA register (FRN 723719) | Regulatory relationship details |
| FCA register (FRN 995009) | Round Financial Limited status |
| Round's existing FAQ / support docs | Common troubleshooting, Open Banking steps |
| BlackRock Money Market Fund documentation | Yield product details, risk disclosures |
| FSCS guidance | Deposit protection limits and eligibility |

**Update cadence:** Knowledge base should be reviewed and updated whenever Round releases new features, changes pricing, or publishes new compliance disclosures.

---

## 11. Success Metrics

| Metric | Target |
|---|---|
| First-response time | < 5 seconds |
| Query containment rate (no escalation needed) | ≥ 70% |
| Escalation handoff time (bot → rep notification) | < 60 seconds |
| Prospect-to-signup conversion rate from chatbot sessions | Track baseline → improve 10% MoM |
| CSAT score (post-session survey) | ≥ 4.2 / 5.0 |
| KYB escalation SLA breach rate | 0% (all flagged within 24 hrs of breach) |

---

## 12. Out-of-Scope (Phase 1)

The following are explicitly out of scope for the initial release and should be considered for Phase 2:

- Authenticated session integration (bot accessing live account data).
- Automated KYB status lookup.
- In-chat document upload for KYB submission.
- Multi-language support (French, German for EU expansion).
- Voice interface.

---

*Prepared for Round Treasury UK. All regulatory details sourced from roundtreasury.com and the FCA register as of May 2026.*
