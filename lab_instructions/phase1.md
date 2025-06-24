# Phase 1 – Cortex Analyst & Slack Setup

Enable natural‑language, governed analytics in Slack by wiring Snowflake Cortex Analyst to a small demo dataset. 

By the end of Phase 1 you’ll be able to type 
`/askcortex Which customers planned a Gen AI POC?` in Slack and receive an auditable answer complete with the generated SQL.

## 1.1 Local project folder
- Create a working directory
```bash
mkdir -p ~/DATAAGENT
cd ~/DATAAGENT
```
- Generate RSA key‑pair (Snowflake key‑pair authentication)
  - In VS Code terminal window, run the following commands to generate a private and public key
```bash
openssl genrsa -out rsa_private_key.pem 2048
openssl rsa -in rsa_private_key.pem -pubout -out rsa_public_key.pem
```

## 1.2 Snowflake environment
- Clone the repo (or download)
```bash
git clone https://github.com/sfc-gh-DShaw98/Cortex-Retrieval-Agent-Lab-for-Slack.git
cd Cortex-Retrieval-Agent-Lab-for-Slack/notebooks
```
- Open [notebooks/Data Agent Cortex Agent Setup Notebook.ipynb](https://github.com/sfc-gh-DShaw98/Cortex-Retrieval-Agent-Lab-for-Slack/blob/main/notebooks/Data%20Agent%20Cortex%20Agent%20Setup%20Notebook.ipynb) in Snowflake Notebooks and run every cell. The notebook will:
  - Create AICOLLEGE database, schema, role, and XS warehouse.
  - Grant the role minimal privileges.
  - Enable cross‑region inference
  - Upload demo CSVs and create the four structured tables plus the CUSTOMER_INSIGHTS view.
  - Generate a Programmatic Access Token (PAT) for Cortex REST – copy it when prompted.
    - You’ll paste this into dataagent.env in Step 1.4.3.
  - Register your public key for the DATAAGENT_USER service user.
  - Verify that SELECT * FROM CUSTOMER_INSIGHTS; returns 6 rows.
    ```sql
    SELECT COUNT(*) FROM CUSTOMER_INSIGHTS;
    ```

## 1.3 Create Semantic model using no‑code Studio
- Follow Snowsight ▸ AI & ML ▸ Studio ▸ Cortex Analyst to create a stage‑backed model.
  
|Wizard Step|What to do|
|------------|------------|
|Location|@AICOLLEGE.PUBLIC.TRANSCRIPTS|
|File name|data_agent_semantic_model.yaml|
|Tables|`CUSTOMER_MEETINGS`, `CUSTOMER_INTERACTIONS`, `CUSTOMER_MEETING_OUTCOMES`, `CUSTOMER_INDUSTRY`, `CUSTOMER_INSIGHTS`|
|Primary keys|Add `MEETING_ID` (meetings/outcomes), `CUSTOMER_NAME` (industry/interactions)|
|Metrics|`total_meetings`  = `COUNT(*)`|
|Named filters|`FY26  = MEETING_DATE >= '2025‑02‑01' AND MEETING_DATE < '2026‑02‑01'`|
|Relationships|See YAML snippet below|
|Custom Instructions|See Text snippet below|

- Add the following `relationships`:
```yaml
relationships:
  - `name: meetings_to_outcomes`
    join_type: left_outer
    relationship_type: one_to_one
    left_table: `CUSTOMER_MEETINGS`
    right_table: `CUSTOMER_MEETING_OUTCOMES`
    relationship_columns:
      - left_column: `MEETING_ID`
        right_column: `MEETING_ID`
  - `name: meetings_to_industry`
    join_type: inner
    relationship_type: many_to_one
    left_table: `CUSTOMER_MEETINGS`
    right_table: `CUSTOMER_INDUSTRY`
    relationship_columns:
      - left_column: `CUSTOMER_NAME`
        right_column: `CUSTOMER_NAME`
  - `name: interactions_to_industry`
    join_type: inner
    relationship_type: many_to_one
    left_table: `CUSTOMER_INTERACTIONS`
    right_table: `CUSTOMER_INDUSTRY`
    relationship_columns:
      - left_column: `CUSTOMER_NAME`
        right_column: `CUSTOMER_NAME`
```

- Add `custom_instructions` using multi‑line string:
```yaml
"- When combining CUSTOMER_MEETINGS and CUSTOMER_MEETING_OUTCOMES, always use a LEFT JOIN to preserve meetings even if no outcome exists.
- Round all DEAL_VALUE and other monetary fields to two decimal places in the final SELECT clause.
- When grouping or filtering by quarter, use the FISCAL_QUARTER column rather than calendar quarter.
- If the user’s question does not include a LIMIT, append LIMIT 1000 to prevent accidentally returning massive result sets.
- If the user asks about Gen AI capabilities, add to your WHERE clause: SNOWFLAKE_FEATURE IN ('Document AI','Snowpark','Cortex AI NLQ')."
```

- Create `verified_queries` for the following:
  
|Name|Question|
|------------|------------|
|InteractionCountByIndustry|How many customer interactions occurred by industry?|
|EngagementByIndustry|What was our customer engagement—unique customers, total meetings, positive feedback count, and total deal value—by industry?|
|FeatureAdoptionBySize|How many meetings discussed each Snowflake feature by customer size?|
|RegionalPerformance|What was the regional performance—unique customers, average meeting duration, and total deal value?|
|CustomerJourneySummary|For each customer, how many meetings did they have, which meeting types and features were covered, and what was their largest deal value?|
|MeetingOutcomeStats|What were the counts, average meeting durations, total deal values, and industries represented for each meeting outcome?|

- Click Save (top‑right) to persist your semantic model. Studio will show ✓ 5 logical tables, 1 metric, 1 named filter.

## 1.4 Slack workspace & app

### 1.4.1 Workspace
- Go to https://slack.com/get-started ▸ Create a Workspace.
- Name it CollegeAI DataAgent (Free plan is fine).

### 1.4.2 App manifest
- Navigate to https://api.slack.com/apps ▸ Create New App › From manifest.
- Choose your workspace and paste [app_manifest.json](https://github.com/sfc-gh-DShaw98/Cortex-Retrieval-Agent-Lab-for-Slack/blob/main/config/app_manifest.json).
- Create.
- Generate App‑level token (connections:write).
- Install in the workspace and copy the Bot OAuth token.
- Under App Home enable Allow users to send Slash commands & messages.

### 1.4.3 Environment file & Python deps
- Create [dataagent.env](https://github.com/sfc-gh-DShaw98/Cortex-Retrieval-Agent-Lab-for-Slack/blob/main/config/dataagent.env.sample) in repo root:
- Update the following in your dataagent.env
#### Slack
`SLACK_BOT_TOKEN`=xoxb-…

`SLACK_APP_TOKEN`=xapp-1-…

#### Snowflake
`SNOWFLAKE_USER`=`DATAAGENT_USER`

`SNOWFLAKE_ACCOUNT`=<your‑account‑without‑underscores>

`SNOWFLAKE_PRIVATE_KEY_PATH`=`rsa_private_key.pem`

`SNOWFLAKE_PAT`=<paste‑PAT>

`SF_DATABASE`=`AICOLLEGE`

`SF_SCHEMA`=`PUBLIC`

`SF_ROLE`=`AICOLLEGE`

`SF_WAREHOUSE`=`AICOLLEGE`

`SF_STAGE`=`TRANSCRIPTS`

`SF_MODEL_FILE`=`data_agent_semantic_model.yaml`

### 1.4.4 Install deps & run bot
- Run the following in your VS Code terminal

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install slack_bolt requests pandas \
    'snowflake-connector-python[pandas]' snowflake-snowpark-python \
    snowflake-core python-dotenv sqlalchemy snowflake-sqlalchemy
```

- Run your Slack app
```bash
python3 check_deps.py   # optional sanity check
python3 slackapp.py
```
Terminal should log:

🚀  CollegeAI Data‑Agent is running!
⚡  Bolt app is running!

### 1.4.5 First questions to try

|Example prompt|Expected behaviour|
|------------|------------|
|`/askcortex What prompts can I ask?`|Returns the 3 “Try this” verified queries.|
|`Which customers planned a Gen AI POC?`|Filters SNOWFLAKE_FEATURE & OUTCOME|
|`What features are customers most interested in?`|Aggregates feature counts|

### 1.4.6 DORA check ✓
- Run SEAI55 inside Slack to mark Phase 1 complete:

```bash
/askcortex SEAI55
```
A green ✅ confirms your semantic model contains 6 unique customers and the bot is live. You may now proceed to [Phase 2 – Cortex Search & Transcript Insights](https://github.com/sfc-gh-DShaw98/Cortex-Retrieval-Agent-Lab-for-Slack/blob/main/lab_instructions/phase2.md).
