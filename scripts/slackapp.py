# slackapp.py

import os
import json
import logging
import warnings
from typing import Any, Dict, List

import requests
import pandas as pd
import snowflake.connector
from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.backends import default_backend

# ─── Suppress Slack SDK “missing text” warning ───────────────────────────────
warnings.filterwarnings(
    "ignore",
    message="The top-level text argument is missing",
    category=UserWarning
)

# ─── Load environment variables ───────────────────────────────────────────────
load_dotenv("dataagent.env")

SLACK_BOT_TOKEN   = os.environ["SLACK_BOT_TOKEN"]
SLACK_APP_TOKEN   = os.environ["SLACK_APP_TOKEN"]

SNOWFLAKE_USER         = os.environ["SNOWFLAKE_USER"]
SNOWFLAKE_ACCOUNT      = os.environ["SNOWFLAKE_ACCOUNT"]
PRIVATE_KEY_PATH       = os.environ["SNOWFLAKE_PRIVATE_KEY_PATH"]
PRIVATE_KEY_PASSPHRASE = os.environ.get("SNOWFLAKE_PRIVATE_KEY_PASSPHRASE")

SNOWFLAKE_PAT = os.environ["SNOWFLAKE_PAT"]

SF_DATABASE   = os.environ["SF_DATABASE"]
SF_SCHEMA     = os.environ["SF_SCHEMA"]
SF_ROLE       = os.environ["SF_ROLE"]
SF_WAREHOUSE  = os.environ["SF_WAREHOUSE"]
SF_STAGE      = os.environ["SF_STAGE"]
SF_MODEL_FILE = os.environ["SF_MODEL_FILE"]

ANALYST_ENDPOINT = (
    f"https://{SNOWFLAKE_ACCOUNT}.snowflakecomputing.com"
    "/api/v2/cortex/analyst/message"
)
SEMANTIC_MODEL_FILE = f"@{SF_DATABASE}.{SF_SCHEMA}.{SF_STAGE}/{SF_MODEL_FILE}"

# ─── DORA grading SQL steps ───────────────────────────────────────────────────
DORA_SQL: Dict[str, str] = {
    "SEAI55": f"""
        SELECT util_db.public.se_grader(
            'SEAI55',
            (actual = expected),
            actual,
            expected,
            '✅ Semantic model loaded – 6 distinct customers found!'
        ) AS graded_results
        FROM (
          SELECT
            (SELECT COUNT(DISTINCT CUSTOMER_NAME)
             FROM {SF_DATABASE}.{SF_SCHEMA}.CUSTOMER_INDUSTRY) AS actual,
            6 AS expected
        );
    """,
    "SEAI56": f"""
        SELECT util_db.public.se_grader(
            'SEAI56',
            EXISTS(
              SELECT 1
                FROM {SF_DATABASE}.{SF_SCHEMA}.TRANSCRIPT_FACTS
               WHERE NEXT_STEP IS NOT NULL
                 AND TRANSCRIPT_SENTIMENT IS NOT NULL
               LIMIT 1
            ),
            -- use total rows as both actual & expected so we get a number back
            (SELECT COUNT(*) FROM {SF_DATABASE}.{SF_SCHEMA}.TRANSCRIPT_FACTS),
            (SELECT COUNT(*) FROM {SF_DATABASE}.{SF_SCHEMA}.TRANSCRIPT_FACTS),
            '✅ Phase 2 check: TRANSCRIPT_FACTS is loaded with NEXT_STEP & TRANSCRIPT_SENTIMENT'
        ) AS graded_results;
    """,
}

# ─── Initialize Slack Bolt ───────────────────────────────────────────────────
app = App(token=SLACK_BOT_TOKEN)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ─── Slack Listeners ─────────────────────────────────────────────────────────
@app.message("hello")
def greet(message, say):
    say(text=f"Hey there <@{message['user']}>! :snowflake:")
    say(text="Ask me anything—either with `/askcortex` or just type your question.\n"
             "To grade your lab step, just type *SEAI55*.")

@app.command("/askcortex")
def command_ask(ack, body, say):
    ack()
    prompt = body.get("text", "").strip()
    if not prompt:
        return say(text="Please include a question after `/askcortex`.")
    _process(prompt, say)

@app.event("message")
def catch_all(ack, body, say):
    # ignore bot echoes
    if body.get("event", {}).get("bot_id"):
        return ack()
    ack()

    raw_text = body["event"]["text"].strip()
    upper_text = raw_text.upper()

    # ── If the user typed exactly a DORA step ID, run its SQL ──
    if upper_text in DORA_SQL:
        try:
            conn = _sf_conn()
            df = pd.read_sql(DORA_SQL[upper_text], conn)
            conn.close()
            return say(text=f"```{df.to_string(index=False)}```")
        except Exception as e:
            logger.exception("Error running DORA step %s", upper_text)
            return say(text=f":x: Failed to run grading step *{upper_text}*: {e}")

    # ── Otherwise, normal Cortex Analyst workflow ──
    _process(raw_text, say)

# ─── Core Processing ─────────────────────────────────────────────────────────
def _process(prompt: str, say) -> None:
    say(text=f"*Question:* {prompt}")
    say(text="Snowflake Cortex Analyst is thinking… :hourglass_flowing_sand:")
    resp = _query_analyst(prompt)
    _render_response(resp["message"]["content"], say)

def _query_analyst(prompt: str) -> Dict[str, Any]:
    payload = {
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": prompt}]}
        ],
        "semantic_model_file": SEMANTIC_MODEL_FILE
    }
    headers = {
        "X-Snowflake-Authorization-Token-Type": "PROGRAMMATIC_ACCESS_TOKEN",
        "Authorization":               f"Bearer {SNOWFLAKE_PAT}",
        "Content-Type":                "application/json",
        "Accept":                      "application/json",
        "X-Snowflake-User":            SNOWFLAKE_USER,
        "X-Snowflake-Account":         SNOWFLAKE_ACCOUNT,
        "X-Snowflake-Database":        SF_DATABASE,
        "X-Snowflake-Schema":          SF_SCHEMA,
        "X-Snowflake-Role":            SF_ROLE,
    }

    logger.info("Cortex request payload:\n%s", json.dumps(payload, indent=2))
    logger.info("Cortex request headers:\n%s", json.dumps(headers, indent=2))

    r = requests.post(ANALYST_ENDPOINT, json=payload, headers=headers)
    if not r.ok:
        logger.error("Cortex response %s:\n%s", r.status_code, r.text)
        r.raise_for_status()
    return r.json()

def _render_response(content: List[Dict[str, str]], say) -> None:
    sqls: List[str] = []
    for block in content:
        t = block["type"]
        if t == "text":
            say(text=f"*Snowflake Cortex Analyst Interpretation:*\n> {block['text']}")
        elif t == "sql":
            stmt = block["statement"]
            sqls.append(stmt)
            # use low-level connector to avoid SQLAlchemy-2.0 issues
            conn = _sf_conn()
            try:
                df = pd.read_sql(stmt, conn)
            finally:
                conn.close()
            say(text=f"*Answer:*\n```{df.to_string(index=False, max_cols=5, max_rows=10)}```")
        elif t == "suggestions":
            bullets = "\n• ".join(block["suggestions"])
            say(text=f"*Try these follow-ups:*\n• {bullets}")

    if sqls:
        joined = "\n\n".join(sqls)
        say(
            text="Here’s the SQL I ran:",  # fallback text
            blocks=[
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": "Would you like to see the generated SQL?"}
                },
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type":     "button",
                            "text":     {"type": "plain_text", "text": "Show SQL"},
                            "action_id":"show_sql",
                            "value":    joined
                        }
                    ]
                }
            ]
        )

@app.action("show_sql")
def show_sql(ack, body, say):
    ack()
    sql  = body["actions"][0]["value"]
    user = body["user"]["id"]
    say(text=f"<@{user}>, here’s the SQL I ran:\n```{sql}```")

# ─── Snowflake Connector Helper ─────────────────────────────────────────────
def _sf_conn():
    # Load & decrypt PEM private key
    with open(PRIVATE_KEY_PATH, "rb") as f:
        pkey = serialization.load_pem_private_key(
            f.read(),
            password=(PRIVATE_KEY_PASSPHRASE.encode() if PRIVATE_KEY_PASSPHRASE else None),
            backend=default_backend()
        )
    pkb = pkey.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption()
    )

    conn = snowflake.connector.connect(
        user=        SNOWFLAKE_USER,
        private_key= pkb,
        account=     SNOWFLAKE_ACCOUNT,
        authenticator="SNOWFLAKE_JWT",
        warehouse=   SF_WAREHOUSE,
        database=    SF_DATABASE,
        schema=      SF_SCHEMA,
        role=        SF_ROLE
    )
    conn.cursor().execute(f"USE WAREHOUSE {SF_WAREHOUSE}")
    return conn

# ─── Entrypoint ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("🚀  CollegeAI Data-Agent is running!")
    SocketModeHandler(app, SLACK_APP_TOKEN).start()
