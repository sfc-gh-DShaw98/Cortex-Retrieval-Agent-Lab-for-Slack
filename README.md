# 🧠 Cortex Retrieval Agent Lab for Slack

**Enable Slack-native analytics from structured and unstructured data with Cortex Agents**

## Overview

This hands-on lab shows you how to build a natural-language agent in Slack using Snowflake Cortex. You'll create a solution that analyzes structured CRM-style data and unstructured meeting transcripts—delivering fast, auditable insights directly in Slack, without writing SQL.

You’ll build a Python Slack bot that dynamically decides whether to run SQL, search a transcript, or summarize meeting outcomes—all using Snowflake-native capabilities.

By the end of this hands-on lab, you'll build a Cortex Agent that:
- Understands natural language questions
- Retrieves the right insights using Cortex Search and Cortex Analyst
- Delivers clear, auditable answers from Snowflake

![[HOL Workflow]](image/OverallFlow.jpg)

## Lab Workflow
This lab walks through a 3-phase agent orchestration flow:

### Phase 1 – Structured analytics with Cortex Analyst
- Load mock CRM-style data into Snowflake
- Define a semantic model with synonyms, filters, metrics, and joins
- Set up Slack integration for natural-language querying
- Run verified queries and see SQL + answers in Slack

### Phase 2 – Add Unstructured Retrieval with Cortex Search
- Upload mock meeting transcripts (PDFs) to a Snowflake stage
- Parse and chunk content using `PARSE_DOCUMENT` + `SPLIT_TEXT_RECURSIVE_CHARACTER`
- Analyze with Cortex Complete: extract sentiment, risk, next steps, key phrases
- Add transcript facts to the semantic model + verify queries across both data types
- Enable Dynamic Literal Retrieval for smarter question matching

### Phase 3 – Full Cortex Agent Orchestration
- Deploy a Python-based Slack agent that: 
  - Selects the right tool (Search, Analyst, or both) 
  - Generates SQL, runs it, and summarizes results 
  - Handles ambiguity and open-ended questions
  - Run the agent locally

## Repository Contents

| Path | Purpose |
|------|---------|
| `notebooks/` | Phase 1 & 2 setup notebooks |
| `data/` | Mock CRM SQL DDL + six PDF transcripts |
| `semantic-model/` | data_agent_semantic_model.yaml (P1) • data_agent_semantic_model2.yaml (P2) |
| `scripts/` | slackapp.py Lightweight bot for Phases 1-2  • agent.py Final Cortex Agent (Phase 3) |
| `config/` | check_deps.py to verify Python packages • manifest.json Slack-app manifest • .env.example template for Snowflake / Slack creds |

> ℹ️ This repo ships mock data & code only. Internal Snowflake grading (DORA-xx) and Seismic links remain on the private enablement portal.

## Prerequisites
- Snowflake account with Cortex features enabled and warehouse with USAGE & OPERATE on Cortex
- Python 3.10+ environment
- Slack workspace with admin privileges
- Basic understanding of SQL and Python

## Getting Started
1. Clone
2. Create & activate venv
3. pip install -r requirements.txt
4. Configure .env
5. Load data & create model via notebook
6. Run agent.py

## Resources
- [Cortex Analyst Documentation](https://docs.snowflake.com/en/user-guide/snowflake-cortex/cortex-analyst)
- [Cortex Analyst REST API Documentation](https://docs.snowflake.com/en/user-guide/snowflake-cortex/cortex-analyst/rest-api)
- [Create Search Services in Snowflake](https://docs.snowflake.com/en/user-guide/snowflake-cortex/cortex-search/cortex-search-overview)
- [Complete Structured Output Docs](https://docs.snowflake.com/en/user-guide/snowflake-cortex/complete-structured-outputs)
- [Cortex Agents Documentation](https://docs.snowflake.com/en/user-guide/snowflake-cortex/cortex-agents)
- [Snowflake Cortex Agents Tutorials](https://docs.snowflake.com/en/user-guide/snowflake-cortex/cortex-agents-tutorials)
