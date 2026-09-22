# VAM LLM Alpha Test - Evaluation Framework

> **Note added September 2026.** This framework was written when the
> application hosted four services, validators included. Since the repository
> split, the MFI Dataset Validator and the Price Data Validator are evaluated
> in the MarketAIssist context (<https://github.com/WFP-VAM/MarketAIssist>);
> everything below applies to the three report drafters of this app. The text
> itself is left unchanged as a dated record.

## Document for WFP Decision Makers

---

## 1. Objective

Collect structured data during a 1-month alpha test (10-20 VAM analysts) to answer three questions:

1. **Does the tool produce reliable outputs?** - Data accuracy, analytical correctness, absence of critical errors
2. **Does the tool save time?** - Quantitative comparison with the current manual process
3. **Would analysts adopt it?** - Usability, trust in output, willingness to adopt

---

## 2. What is being tested

The VAM LLM system includes 4 agents, each evaluated separately:

| Agent | Function | Main risk to evaluate |
|--------|----------|------------------------|
| **MFI Validator** | Validates raw MFI datasets (structure, schema, business rules) | False positives/negatives in validation |
| **Price Validator** | Validates price data and classifies products vs WFP list | Product classification errors |
| **MFI Drafter** | Generates MFI reports with dimension analysis and recommendations | Data/narrative inconsistencies, sub-score inversion |
| **Market Monitor** | Generates price bulletins with trends, events, and analysis | Unsupported speculation, terminology errors |

### Operational assumptions

- External endpoints and API keys (ReliefWeb, Seerist, TradingEconomics, Databridges) are managed via deployment secrets. Testers do not enter keys.
- Market Monitor and MFI Drafter retrieve processed operational data directly from Databridges.

---

## 3. Methodology: 3 pillars

### Pillar 1: Bug & Defect Reporting (continuous)
- **What**: Online form to report issues in real time
- **When**: Every time a tester finds a problem
- **Output**: Prioritized bug list with severity and reproducibility
- **Tool**: Google Form / Microsoft Form

### Pillar 2: Holistic Feedback Survey (2 administrations)
- **What**: Structured questionnaire on accuracy, usability, trust, feature gaps
- **When**: End of week 2 (mid-test) and end of week 4 (end-test)
- **Output**: Quantitative scores by evaluation dimension, NPS, feature priority
- **Tool**: Google Form / Microsoft Form with routing logic

### Pillar 3: Time Savings Measurement (continuous)
- **What**: Structured tracking of time saved per task
- **When**: After each tool use (quick post-task entry)
- **Output**: Estimated average time saved per agent and per task type
- **Tool**: Shared Google Sheet

---

## 4. Monthly test timeline

```
Week 0 (pre-test)
  - Tester onboarding: guide, live demo, tool access, eval tools
  - Baseline collection: estimated time for manual tasks (retrospective)

Week 1-2
  - Free tool use with continuous bug reporting
  - Time tracking for every use
  - End of week 2: Mid-test survey

Week 3-4
  - Continued use, focus on less-tested agents
  - Time tracking for every use
  - End of week 4: End-test survey + qualitative interviews (optional, 3-5 testers)

Week 5 (post-test)
  - Data analysis and final report production
```

---

## 5. Key metrics (KPI)

### Output quality

| Metric | Alpha target | How it is measured |
|---------|--------------|--------------------|
| Open blocking bugs | < 5 at end of test | Bug report form |
| Perceived accuracy (1-5) | >= 3.5 average | Holistic survey |
| "Usable without edits" rate | >= 40% | Holistic survey |
| "Usable with minor edits" rate | >= 70% (cumulative) | Holistic survey |

### Efficiency

| Metric | Alpha target | How it is measured |
|---------|--------------|--------------------|
| Time saved per task (%) | >= 30% | Time tracking sheet |
| Estimated annualized savings (hours/analyst) | Calculated | Time tracking + task frequency |

### Adoption

| Metric | Alpha target | How it is measured |
|---------|--------------|--------------------|
| NPS (Net Promoter Score) | >= 0 (positive) | Holistic survey |
| "I would use it regularly" (1-5) | >= 3.5 | Holistic survey |
| Spontaneous usage frequency | Implicit tracking | Time tracking entry count |

---

## 6. Post-test decision criteria

The collected data will support a structured decision:

**Proceed with development** if:
- Time savings >= 30% on at least 2 agents
- Perceived accuracy >= 3.5/5
- NPS >= 0
- No unresolved blocking bugs with no viable workaround

**Proceed with reservations** if:
- Time savings 15-30%
- Perceived accuracy 2.5-3.5
- Blocking bugs present but solvable

**Re-evaluate** if:
- Time savings < 15%
- Perceived accuracy < 2.5
- Strongly negative NPS
- Structural blocking bugs

---

## 7. Required resources

| Resource | Estimated tester effort |
|---------|--------------------------|
| Onboarding | 30 minutes (one time) |
| Tool use + bug reporting | Normal work usage |
| Time tracking (post-task) | 1-2 minutes per task |
| Mid-test survey | 15-20 minutes |
| End-test survey | 15-20 minutes |
| **Total monthly overhead** | **~2-3 hours distributed across the month** |

---

## 8. Final deliverables

At the end of the test month, the team will produce:

1. **Metrics dashboard**: scores per agent and evaluation dimension
2. **Prioritized bug list**: severity, frequency, and impact
3. **Time savings analysis**: estimated savings per agent, with confidence intervals and methodological caveats
4. **Feature priority matrix**: requested features ranked by frequency and impact
5. **Go/no-go recommendation**: based on the decision criteria above

---

*Framework inspired by "Demystifying evals for AI agents" (Anthropic, 2025). Adapted for alpha testing with non-technical human evaluators.*
