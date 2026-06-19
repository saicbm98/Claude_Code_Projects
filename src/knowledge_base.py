"""
Round Treasury knowledge base — static facts used to seed the system prompt.
Update this file whenever Round releases new features, changes pricing, or
publishes new compliance disclosures.
"""

ROUND_KNOWLEDGE_BASE = """
## ABOUT ROUND TREASURY

Round Treasury (roundtreasury.com) is an AI-powered treasury and finance automation platform
for high-growth businesses — primarily founders, CFOs, and VC-backed operators.

**Legal entity:** Round Financial Limited
- Company number: 14609702 (England & Wales)
- FCA reference number: FRN 995009
- Appointed Representative of Wealthkernel Limited (FRN 723719)
- Registered address: Laundry Studios, 2 Warburton Road, London, E8 3RT
- ISO 27001:2022 certified
- Works exclusively with FCA-regulated financial partners
- FSCS protection available via multi-bank savings diversification

---

## PRODUCTS (FOUR MODULES)

### 1. Treasury
- Connects 2,000+ UK and EU bank accounts via Open Banking
- Earns up to 5% yield through BlackRock Money Market Funds (MMFs)
- Yield is typically 4× what a standard business savings account pays
- MMFs offer next-day liquidity — cash is never locked away
- Vault feature spreads cash across 100+ FSCS-protected savings accounts
- Assets are held with regulated custodians, segregated from Round's balance sheet
- Clients retain direct access to funds at all times

### 2. Accounts Payable (AP)
- Reduces invoice processing effort by approximately 75%
- Automates payment scheduling, approvals, and reconciliation
- Integrates with Xero and NetSuite via two-way sync (real-time reconciliation)

### 3. Payroll
- Scheduled payment execution with full audit trail
- Multi-currency support
- Autonomous payroll runs with approval controls

### 4. Multi-Entity
- Consolidated view across multiple business entities and accounts
- Cross-entity reporting and cash management
- Single platform for group-level treasury visibility

---

## KEY METRICS & CLAIMS

- Up to 5% yield via BlackRock Money Market Funds
- 4× the yield of a standard business savings account
- 75% reduction in invoice processing workload
- 2,000+ UK and EU banks connected via Open Banking
- 5 days from signup to majority automation
- 1-click signup; bank connection takes ~30 seconds per bank via Open Banking
- 30 seconds per bank connection
- Same-day clearing for withdrawals submitted by 10:30am
- Next-day liquidity for Money Market Funds

---

## INTEGRATIONS

- 2,000+ UK and EU banks via Open Banking
- Xero (two-way sync, real-time reconciliation)
- NetSuite (two-way sync, real-time reconciliation)
- Slack (approval workflows, alerts, notifications)
- WhatsApp (alert-style notifications)
- Email (approvals, alerts)

---

## ONBOARDING TIMELINE (SEGMENTS B CLIENTS)

**Day 1:**
- Connect bank accounts via Open Banking (~30 seconds per bank)
- Connect ERP (Xero or NetSuite)
- Make first yield-earning deposit
- KYB (Know Your Business) verification submitted

**Week 1:**
- Approval workflows become active
- First automated payment runs
- Ledger sync begins
- Configure: approval thresholds, payment schedules, cash minimums
- Set up Slack approval workflow

**Week 2:**
- Majority automation achieved
- Improved yield returns visible
- Reduced manual workload confirmed

**KYB:**
- Typically approved within 3 business days
- If 4+ business days have elapsed without approval, this requires human escalation (HIGH priority)

---

## WITHDRAWALS & LIQUIDITY

- Requests submitted by 10:30am typically clear same-day
- Money Market Funds: next-day liquidity
- Funds are never locked away

---

## COMPLIANCE & REGULATION

- Round Financial Limited is an Appointed Representative of Wealthkernel Limited
- Wealthkernel Limited FRN: 723719 (FCA-authorised)
- Round Financial Limited FRN: 995009
- ISO 27001:2022 certified (information security)
- Immutable audit trails on all transactions
- Multi-layer permission controls
- Approval threshold configuration
- Team access management

---

## DATA & SECURITY

- Bank login credentials are NEVER stored by Round
- Open Banking compliant (credentials go directly to the bank)
- Two-factor authentication (2FA) required
- No user data sold to third parties
- FSCS protection available via multi-bank diversification through the Vault feature

---

## PRICING

- Free tier available for unified treasury view
- Full pricing details at roundtreasury.com/pricing
- Enterprise/custom deals handled by a human representative

---

## WHO ROUND IS FOR

- Founders and CEOs of high-growth UK/EU companies
- CFOs and finance teams
- VC-backed operators
- Companies looking to optimise idle cash, automate payments, and reduce finance team workload

---

## AGENTIC WORKFLOW BUILDER

- Clients describe workflows in plain English
- Round builds the automation
- Client reviews and approves
- Autonomous 24/7 execution
- Notifications via Slack, WhatsApp, or email
- All actions logged with immutable audit trail

---

## SUPPORT MODEL

- Each client gets a dedicated Slack channel with the Round team
- Human-led onboarding support
- Escalations resolved via Slack within a few hours typically

---

## OUT-OF-SCOPE FOR RONDO (ALWAYS ESCALATE OR DECLINE)

- Specific investment advice ("should I invest X in MMFs?")
- Commentary on competitors
- Commitments on custom pricing or contract terms
- Discussion of ongoing legal or regulatory investigations
- Access to client account-specific data (balances, transactions)
"""
