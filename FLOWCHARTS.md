# Round Treasury — Rondo Chatbot: Flowchart Architecture

> Render with any Mermaid-compatible viewer (GitHub, VS Code Mermaid Preview, mermaid.live)

---

## Diagram 1 — Master Architecture (Top-Level Flow)

```mermaid
flowchart TD
    START([🟢 User opens chat]) --> GREET[Rondo greets user\nand presents routing menu]

    GREET --> ROUTE{Who are you?}

    ROUTE -->|A — Exploring Round| PROSPECT[MODULE A\nProspect Flow]
    ROUTE -->|B — New client setup| ONBOARD[MODULE B\nActive Onboarding Flow]
    ROUTE -->|C — Existing client| ESTABLISHED[MODULE C\nEstablished Client Flow]

    PROSPECT --> RESOLVE_P{Query resolved?}
    ONBOARD --> RESOLVE_O{Query resolved?}
    ESTABLISHED --> RESOLVE_E{Query resolved?}

    RESOLVE_P -->|Yes| CSAT
    RESOLVE_O -->|Yes| CSAT
    RESOLVE_E -->|Yes| CSAT

    RESOLVE_P -->|No — escalate| ESCALATION[⚠️ ESCALATION ENGINE]
    RESOLVE_O -->|No — escalate| ESCALATION
    RESOLVE_E -->|No — escalate| ESCALATION

    ESCALATION --> NOTIFY[Notify human rep\nvia Slack + email]
    NOTIFY --> HANDOFF[Bot hands off with\nfull context summary]
    HANDOFF --> CSAT

    CSAT([⭐ Post-session CSAT survey\nThen END])

    style START fill:#22c55e,color:#fff
    style ESCALATION fill:#f97316,color:#fff
    style CSAT fill:#6366f1,color:#fff
    style PROSPECT fill:#0ea5e9,color:#fff
    style ONBOARD fill:#0ea5e9,color:#fff
    style ESTABLISHED fill:#0ea5e9,color:#fff
```

---

## Diagram 2 — Module A: Prospect Flow

```mermaid
flowchart TD
    A_START([Enter Prospect Module]) --> A_TOPIC{What does the user\nwant to know?}

    A_TOPIC -->|How Round works| A_OVERVIEW[Explain 4 products:\nTreasury · AP · Payroll · Multi-Entity\n+ key metrics]
    A_TOPIC -->|Yield / returns| A_YIELD[Quote up to 5% via BlackRock MMF\n4× vs standard savings\nAppend capital-at-risk disclaimer]
    A_TOPIC -->|Pricing| A_PRICE[Explain free tier\nDirect to /pricing page]
    A_TOPIC -->|Security & compliance| A_SECURITY[FCA · ISO 27001 · FSCS\nOpen Banking · data handling]
    A_TOPIC -->|Integrations| A_INTEGRATIONS[2000+ banks · Xero · NetSuite\nSlack · WhatsApp · email]
    A_TOPIC -->|Who is Round for?| A_WHO[Founders · CFOs · VC operators\nHigh-growth companies]
    A_TOPIC -->|Setup speed| A_SPEED[1-click signup\n30-sec bank connection\n5 days to automation]
    A_TOPIC -->|Comparison vs bank| A_COMPARE[Multi-bank aggregation\nYield optimisation\nAI automation]

    A_OVERVIEW --> A_CTA
    A_YIELD --> A_CTA
    A_PRICE --> A_CTA
    A_SECURITY --> A_CTA
    A_INTEGRATIONS --> A_CTA
    A_WHO --> A_CTA
    A_SPEED --> A_CTA
    A_COMPARE --> A_CTA

    A_CTA{Follow-up action?}
    A_CTA -->|Another question| A_TOPIC
    A_CTA -->|Ready to sign up| A_SIGNUP[Direct to 1-click signup]
    A_CTA -->|Book a demo| A_DEMO[Capture name · company · email\nNotify rep for demo booking]
    A_CTA -->|Pricing negotiation\nor enterprise deal| A_ESC[⚠️ Escalate to human rep\nCapture lead details]
    A_CTA -->|Unresolvable query| A_ESC

    A_SIGNUP --> A_END([Return to Master Flow])
    A_DEMO --> A_END
    A_ESC --> A_END

    style A_START fill:#0ea5e9,color:#fff
    style A_END fill:#0ea5e9,color:#fff
    style A_ESC fill:#f97316,color:#fff
    style A_YIELD fill:#fbbf24
```

---

## Diagram 3 — Module B: Active Onboarding Flow

```mermaid
flowchart TD
    B_START([Enter Onboarding Module]) --> B_PHASE{Which onboarding\nphase?}

    B_PHASE -->|Day 1 — Connecting banks| B_BANKS
    B_PHASE -->|Day 1 — First deposit| B_DEPOSIT
    B_PHASE -->|Week 1 — Workflows| B_WORKFLOWS
    B_PHASE -->|Week 1 — ERP sync| B_ERP
    B_PHASE -->|KYB question| B_KYB
    B_PHASE -->|Week 2 — Automation| B_AUTOMATION
    B_PHASE -->|Something else| B_OTHER

    B_BANKS[Step-by-step Open Banking\nconnection ~30 sec per bank\n2000+ UK/EU banks]
    B_DEPOSIT[Explain first yield-earning deposit\nVault diversification across\n100+ FSCS-protected accounts]
    B_WORKFLOWS[Configure approval thresholds\nPayment schedules · cash minimums\nSlack approval setup]
    B_ERP[Xero / NetSuite two-way sync\nReal-time reconciliation\nStep-by-step guide]
    B_AUTOMATION[Confirm majority automation\nCheck workflows active\nReview yield performance]
    B_OTHER[Free-text query handling\nAttempt 1]

    B_KYB --> B_KYB_CHECK{How many business\ndays elapsed?}
    B_KYB_CHECK -->|1–3 days| B_KYB_WAIT[Confirm 3-day SLA\nExplain what happens next]
    B_KYB_CHECK -->|4+ days| B_KYB_ESC[⚠️ Flag as high-priority\nEscalate to human rep\nCapture company + email]

    B_BANKS --> B_RESOLVED
    B_DEPOSIT --> B_RESOLVED
    B_WORKFLOWS --> B_RESOLVED
    B_ERP --> B_RESOLVED
    B_AUTOMATION --> B_RESOLVED
    B_KYB_WAIT --> B_RESOLVED
    B_OTHER --> B_ATTEMPT{Resolved\nafter attempt 1?}

    B_ATTEMPT -->|Yes| B_RESOLVED
    B_ATTEMPT -->|No — attempt 2| B_RETRY[Rephrase answer\nOffer alternative approach]
    B_RETRY --> B_ATTEMPT2{Resolved\nafter attempt 2?}
    B_ATTEMPT2 -->|Yes| B_RESOLVED
    B_ATTEMPT2 -->|No| B_ESC[⚠️ Escalate to human rep]

    B_RESOLVED{Follow-up\nneeded?}
    B_RESOLVED -->|Another question| B_PHASE
    B_RESOLVED -->|No — done| B_END([Return to Master Flow])

    B_KYB_ESC --> B_END
    B_ESC --> B_END

    style B_START fill:#0ea5e9,color:#fff
    style B_END fill:#0ea5e9,color:#fff
    style B_KYB_ESC fill:#ef4444,color:#fff
    style B_ESC fill:#f97316,color:#fff
```

---

## Diagram 4 — Module C: Established Client Flow

```mermaid
flowchart TD
    C_START([Enter Established Client Module]) --> C_TOPIC{Topic area?}

    C_TOPIC -->|Fund security| C_SECURITY
    C_TOPIC -->|Compliance & regulation| C_COMPLIANCE
    C_TOPIC -->|Withdrawals & liquidity| C_WITHDRAW
    C_TOPIC -->|Agentic Workflow Builder| C_WORKFLOWS
    C_TOPIC -->|Autonomous Payroll| C_PAYROLL
    C_TOPIC -->|Multi-Entity| C_MULTI
    C_TOPIC -->|FX & multi-currency| C_FX
    C_TOPIC -->|Data & privacy| C_DATA
    C_TOPIC -->|Permissions & approvals| C_PERMS
    C_TOPIC -->|Pricing / billing| C_BILLING
    C_TOPIC -->|Complaint or dispute| C_COMPLAINT
    C_TOPIC -->|Investment advice| C_INVEST

    C_SECURITY[Assets segregated with\nregulated custodians\nFSCS multi-bank protection\nDirect access retained]

    C_COMPLIANCE[FCA appointed rep of Wealthkernel\nFRN 995009 / 723719\nISO 27001:2022\nImmutable audit trails]

    C_WITHDRAW[Same-day clearing\nif submitted by 10:30am\nNext-day liquidity for MMFs]

    C_WORKFLOWS[Plain-English workflow description\n→ Round builds it\n→ User approves\n→ Autonomous 24/7 execution\nSlack/WhatsApp/email alerts]

    C_PAYROLL[Scheduled payment execution\nMulti-currency support\nFull audit trail]

    C_MULTI[Consolidated view across\nmultiple entities and accounts\nCross-entity reporting]

    C_FX[Fair FX pricing\nMulti-currency payments\nReal-time rate information]

    C_DATA[Credentials never stored\nOpen Banking compliant\n2FA required\nNo data sold to third parties]

    C_PERMS[Multi-layer permission controls\nApproval threshold configuration\nTeam access management]

    C_BILLING[Direct to pricing page\nfor standard queries] --> C_BILLING_CHECK{Complex\nnegotiation?}
    C_BILLING_CHECK -->|Yes| C_ESC_BILLING[⚠️ Escalate to human rep]
    C_BILLING_CHECK -->|No| C_RESOLVED

    C_COMPLAINT[⚠️ Immediate escalation\nNo further bot handling\nApologise and hand off] --> C_ESC

    C_INVEST[State bot cannot give\ninvestment advice\nRecommend qualified financial advisor\nOffer to connect with rep] --> C_ESC

    C_SECURITY --> C_RESOLVED
    C_COMPLIANCE --> C_RESOLVED
    C_WITHDRAW --> C_RESOLVED
    C_WORKFLOWS --> C_RESOLVED
    C_PAYROLL --> C_RESOLVED
    C_MULTI --> C_RESOLVED
    C_FX --> C_RESOLVED
    C_DATA --> C_RESOLVED
    C_PERMS --> C_RESOLVED

    C_RESOLVED{Follow-up\nneeded?}
    C_RESOLVED -->|Another question| C_TOPIC
    C_RESOLVED -->|Unresolvable after 2 attempts| C_ESC
    C_RESOLVED -->|No — done| C_END([Return to Master Flow])

    C_ESC[⚠️ Escalate to human rep] --> C_END
    C_ESC_BILLING --> C_END

    style C_START fill:#0ea5e9,color:#fff
    style C_END fill:#0ea5e9,color:#fff
    style C_COMPLAINT fill:#ef4444,color:#fff
    style C_ESC fill:#f97316,color:#fff
    style C_ESC_BILLING fill:#f97316,color:#fff
    style C_INVEST fill:#fbbf24
```

---

## Diagram 5 — Escalation Engine (Detailed)

```mermaid
flowchart TD
    ESC_TRIGGER([⚠️ Escalation triggered]) --> ESC_REASON{Reason for\nescalation?}

    ESC_REASON -->|Complaint / dispute| ESC_PRIORITY_HIGH
    ESC_REASON -->|KYB overdue 4+ days| ESC_PRIORITY_HIGH
    ESC_REASON -->|User requested human| ESC_PRIORITY_NORMAL
    ESC_REASON -->|2 failed resolution attempts| ESC_PRIORITY_NORMAL
    ESC_REASON -->|Investment advice requested| ESC_PRIORITY_NORMAL
    ESC_REASON -->|Pricing negotiation / enterprise deal| ESC_PRIORITY_NORMAL
    ESC_REASON -->|Account-specific data needed| ESC_PRIORITY_NORMAL

    ESC_PRIORITY_HIGH[🔴 Priority: HIGH]
    ESC_PRIORITY_NORMAL[🟡 Priority: NORMAL]

    ESC_PRIORITY_HIGH --> ESC_CAPTURE
    ESC_PRIORITY_NORMAL --> ESC_CAPTURE

    ESC_CAPTURE[Collect user details\nName · Company · Email\nif not already known]

    ESC_CAPTURE --> ESC_SUMMARY[Generate conversation summary\nLast 3 turns + issue description]

    ESC_SUMMARY --> ESC_NOTIFY_SLACK[POST to Slack\n#escalations channel\nwith priority tag]
    ESC_SUMMARY --> ESC_NOTIFY_EMAIL[Send email to\nhuman rep\nwith full transcript]

    ESC_NOTIFY_SLACK --> ESC_USER_MSG
    ESC_NOTIFY_EMAIL --> ESC_USER_MSG

    ESC_USER_MSG[Bot confirms handoff to user:\n'Connecting you with the Round team.\nThey respond within a few hours\nvia your dedicated Slack channel.']

    ESC_USER_MSG --> ESC_END([Return to Master Flow → CSAT])

    style ESC_TRIGGER fill:#f97316,color:#fff
    style ESC_PRIORITY_HIGH fill:#ef4444,color:#fff
    style ESC_PRIORITY_NORMAL fill:#fbbf24
    style ESC_END fill:#6366f1,color:#fff
```

---

## Diagram 6 — Compliance & Disclaimer Logic

```mermaid
flowchart TD
    COMP_START([Any response being generated]) --> COMP_CHECK{Does response\nmention yield,\nreturns, or investing?}

    COMP_CHECK -->|Yes| COMP_DISCLAIMER[Append disclaimer:\n'Your capital is at risk.\nConsult a qualified financial advisor.']
    COMP_CHECK -->|No| COMP_REG_CHECK

    COMP_DISCLAIMER --> COMP_REG_CHECK

    COMP_REG_CHECK{Does response\nmention regulation\nor FCA status?}
    COMP_REG_CHECK -->|Yes| COMP_REG_DETAILS[Include:\nRound Financial Ltd FRN 995009\nAppointed rep of Wealthkernel FRN 723719\nISO 27001:2022 certified]
    COMP_REG_CHECK -->|No| COMP_DATA_CHECK

    COMP_REG_DETAILS --> COMP_DATA_CHECK

    COMP_DATA_CHECK{Does response\nmention data or\nbank credentials?}
    COMP_DATA_CHECK -->|Yes| COMP_DATA_STATEMENT[Include:\nCredentials never stored\nOpen Banking compliant · 2FA required]
    COMP_DATA_CHECK -->|No| COMP_OUT

    COMP_DATA_STATEMENT --> COMP_OUT
    COMP_OUT([✅ Response sent to user])

    style COMP_START fill:#6366f1,color:#fff
    style COMP_OUT fill:#22c55e,color:#fff
    style COMP_DISCLAIMER fill:#fbbf24
```

---

## Diagram 7 — User Identification & Session State

```mermaid
stateDiagram-v2
    [*] --> Greeting

    Greeting --> SegmentA : User selects Prospect
    Greeting --> SegmentB : User selects Active Client
    Greeting --> SegmentC : User selects Established Client

    SegmentA --> Resolved : Query answered
    SegmentB --> Resolved : Query answered
    SegmentC --> Resolved : Query answered

    SegmentA --> Escalated : Cannot resolve
    SegmentB --> Escalated : Cannot resolve / KYB overdue / complaint
    SegmentC --> Escalated : Cannot resolve / complaint / investment advice

    Resolved --> FollowUp : User has another question
    Resolved --> CSAT : User is done

    FollowUp --> SegmentA : Re-enters same segment
    FollowUp --> SegmentB : Re-enters same segment
    FollowUp --> SegmentC : Re-enters same segment
    FollowUp --> Greeting : User types 'main menu'

    Escalated --> HumanHandoff : Rep notified via Slack + email
    HumanHandoff --> CSAT : Session complete

    CSAT --> [*]
```

---

*All diagrams use [Mermaid](https://mermaid.js.org/) syntax. Render at [mermaid.live](https://mermaid.live) or in VS Code with the Mermaid Preview extension.*
