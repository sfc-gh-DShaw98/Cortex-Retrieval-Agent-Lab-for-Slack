# Phase 2 –  Cortex Search & Transcript Insights

Extend your Data Agent to mine unstructured customer‑meeting transcripts with Snowflake Cortex Search. 

After this phase, `/askcortex Which meetings for Delta Enterprises had low sentiment?` will return answers that blend structured CRM data and semantic text search.

## 2.1  Upload transcripts to the TRANSCRIPTS stage
- Switch to role AICOLLEGE.
- In Snowsight, open Data ▸ Databases ▸ AICOLLEGE ▸ PUBLIC ▸ Stages ▸ TRANSCRIPTS.
- Click + Files and upload the [six PDFs](transcripts) + two JPG assets supplied
  - [Acme Corp Services Discovery Meeting Transcript.pdf](https://github.com/sfc-gh-DShaw98/Cortex-Retrieval-Agent-Lab-for-Slack/blob/main/transcripts/Acme%20Corp%20Services%20Discovery%20Meeting%20Transcript.pdf)
  - [BetaTech Global Discovery Meeting Transcript.pdf](https://github.com/sfc-gh-DShaw98/Cortex-Retrieval-Agent-Lab-for-Slack/blob/main/transcripts/BetaTech%20Global%20Discovery%20Meeting%20Transcript.pdf)
  - [Delta Enterprises Discovery Meeting Transcript.pdf](https://github.com/sfc-gh-DShaw98/Cortex-Retrieval-Agent-Lab-for-Slack/blob/main/transcripts/Delta%20Enterprises%20Discovery%20Meeting%20Transcript.pdf)
  - [Gamma LLC Discovery Meeting Transcript.pdf](https://github.com/sfc-gh-DShaw98/Cortex-Retrieval-Agent-Lab-for-Slack/blob/main/transcripts/Gamma%20LLC%20Discovery%20Meeting%20Transcript.pdf)
  - [Omega Industries Discovery Meeting Transcript.pdf](https://github.com/sfc-gh-DShaw98/Cortex-Retrieval-Agent-Lab-for-Slack/blob/main/transcripts/Omega%20Industries%20Discovery%20Meeting%20Transcript.pdf)
  - [Zeta Solutions Discovery Meeting Transcript.pdf](https://github.com/sfc-gh-DShaw98/Cortex-Retrieval-Agent-Lab-for-Slack/blob/main/transcripts/Zeta%20Solutions%20Discovery%20Meeting%20Transcript.pdf)
  - [phase2‑1.jpg](https://github.com/sfc-gh-DShaw98/Cortex-Retrieval-Agent-Lab-for-Slack/blob/main/image/Phase1.jpg),
  - [phase2‑2.jpg](https://github.com/sfc-gh-DShaw98/Cortex-Retrieval-Agent-Lab-for-Slack/blob/main/image/Phase2.jpg)
- Wait for the Upload complete notification.

## 2.2  Run the Cortex Search notebook
- Notebook [Data Agent Cortex Search Exercise Notebook.ipynb](https://github.com/sfc-gh-DShaw98/Cortex-Retrieval-Agent-Lab-for-Slack/blob/main/notebooks/Data%20Agent%20Cortex%20Search%20Exercise%20Notebook.ipynb)
- Import the notebook into Snowflake Notebooks (same warehouse & DB).
- Install missing packages: snowflake‑ml‑python and snowflake.core when prompted.
- Work through each cell, replacing all XXX placeholders as instructed (e.g. model name, function name, prompt).
- The notebook will:
  - Parse every PDF/JPG with `PARSE_DOCUMENT` and chunk text via `SPLIT_TEXT_RECURSIVE_CHARACTER`.
  - Enrich chunks with `SENTIMENT`, `COMPLETE` (risk, key‑phrases, next‑steps).
  - Build `TRANSCRIPT_FACTS` table and create `TRANSCRIPTS_SEARCH_SERVICE`.
  - Print ✓ Notebook finished when done.

## 2.3 Augment the semantic model

### 2.3.1 Add a new logical table

|Property|Value|
|--------|--------|
|Name|`TRANSCRIPT_FACTS`|
|Source table|`AICOLLEGE.PUBLIC.TRANSCRIPT_FACTS`|
|Synonyms|transcript, call_notes|
|Primary key|`CHUNK_ID`|
|Metrics|`total_chunks` = `COUNT(*)`|
|Named filter|`negative_sentiment` = `TRANSCRIPT_SENTIMENT < 0.5`|

### 2.3.2 Add a relationship
- `meeting_transcripts` (one‑to‑one, INNER JOIN):
  - `CUSTOMER_MEETINGS.CUSTOMER_NAME` → `TRANSCRIPT_FACTS.CUSTOMER_NAME`

### 2.3.3 Add verified queries

|Name|Question|
|--------|--------|
|`high_sentiment_low_risk`|Which customers have high transcript sentiment and low risk?|
|`customer_key_phrases_agenda`|Show me key phrases by customer and agenda topics.|

- Save the model. Studio should now show ✓ 6 logical tables, 2 metrics, 2 named filters.

### 2.4 Enable Dynamic Literal Retrieval (DLR)

Dynamic Literal Retrieval lets Cortex Analyst look up real‑world strings (customer names, agendas, key phrases) on the fly via your search service.

#### Option 1  GUI
- Open `TRANSCRIPT_FACTS` ▸ Dimensions.
- For each column below, click + Search Service and connect TRANSCRIPTS_SEARCH_SERVICE:
  - customer_name, relative_path, agenda, next_step, key_phrases
- Save.

#### Option 2  SQL

```sql
CREATE OR REPLACE CORTEX SEARCH SERVICE TRANSCRIPT_FACTS_SEARCH
ON CHUNK
ATTRIBUTES customer_name, agenda, next_step, key_phrases
WAREHOUSE = AICOLLEGE
AS (
  SELECT customer_name, agenda, next_step, key_phrases,
         TO_VARCHAR(transcript_sentiment) AS CHUNK
  FROM AICOLLEGE.PUBLIC.TRANSCRIPT_FACTS
);
```

- Then reference it in data_agent_semantic_model.yaml:

```yaml
- literal_retrieval_services:
  - service_name: TRANSCRIPT_FACTS_SEARCH
    match_columns: [customer_name, agenda, key_phrases]
```

### 2.5 Test the enhanced model in Slack

- Restart the bot: python3 slackapp.py.
- Try these sample prompts in #collegeai‑dataagent:
- Which meetings for Delta Enterprises had low sentiment?
- Show me Cortex-related meetings with a low risk score and demo next step.
- Confirm that answers include transcript‑derived fields (sentiment, risk, agenda).

### 2.6 DORA Evaluation #56
- Run the Phase 2 check in Slack:
  
```bash
/askcortex SEAI56
```
A green ✅ means your hybrid model is live and searchable. Proceed to Phase 3 – Agent Orchestration!
