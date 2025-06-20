# slackapp_agent.py
import os, re, time, logging, warnings
from typing import Any, Dict, List
import json

import requests
import pandas as pd
from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from snowflake.snowpark.session import Session
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.backends import default_backend

# ─── logging & warnings ─────────────────────────────────────────────────────
LOGLEVEL = os.environ.get("LOGLEVEL", "INFO").upper()
logging.basicConfig(level=LOGLEVEL)
logger = logging.getLogger(__name__)
warnings.filterwarnings("ignore", message="The top-level text argument is missing", category=UserWarning)

# ─── load env vars ───────────────────────────────────────────────────────────
load_dotenv("dataagent.env")

SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
SLACK_APP_TOKEN = os.environ["SLACK_APP_TOKEN"]
SNOWFLAKE_PAT   = os.environ["SNOWFLAKE_PAT"]

SF_USER       = os.environ["SNOWFLAKE_USER"]
SF_KEY_FILE   = os.environ["SNOWFLAKE_PRIVATE_KEY_PATH"]
SF_KEY_PASSPH = os.environ.get("SNOWFLAKE_PRIVATE_KEY_PASSPHRASE", "")
SF_ROLE       = os.environ["SF_ROLE"]
SF_WAREHOUSE  = os.environ["SF_WAREHOUSE"]

SF_ACCOUNT    = os.environ["SNOWFLAKE_ACCOUNT"]
SF_DATABASE   = os.environ["SF_DATABASE"]
SF_SCHEMA     = os.environ["SF_SCHEMA"]
SF_STAGE      = os.environ["SF_STAGE"]
SF_MODEL_FILE = os.environ["SF_MODEL_FILE"]
AGENT_NAME    = os.environ["AGENT_NAME"]

# ─── Cortex REST endpoints ──────────────────────────────────────────────────
AGENT_ENDPOINT   = f"https://{SF_ACCOUNT}.snowflakecomputing.com/api/v2/cortex/agent:run"
ANALYST_ENDPOINT = f"https://{SF_ACCOUNT}.snowflakecomputing.com/api/v2/cortex/analyst/message"
SEARCH_SERVICE   = "TRANSCRIPTS_SEARCH_SERVICE"
SEMANTIC_MODEL_URI = f"@{SF_DATABASE}.{SF_SCHEMA}.{SF_STAGE}/{SF_MODEL_FILE}"

# ─── decrypt your RSA key ────────────────────────────────────────────────────
with open(SF_KEY_FILE, "rb") as f:
    pem = f.read()
pkey = serialization.load_pem_private_key(
    pem, password=SF_KEY_PASSPH.encode() if SF_KEY_PASSPH else None, backend=default_backend()
)
DER_KEY = pkey.private_bytes(
    encoding=serialization.Encoding.DER,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption()
)

# ─── build Snowpark session ─────────────────────────────────────────────────
session = Session.builder.configs({
    "account":       SF_ACCOUNT,
    "user":          SF_USER,
    "authenticator": "SNOWFLAKE_JWT",
    "private_key":   DER_KEY,
    "role":          SF_ROLE,
    "warehouse":     SF_WAREHOUSE,
    "database":      SF_DATABASE,
    "schema":        SF_SCHEMA,
}).create()

# ─── ensure check-in table exists ────────────────────────────────────────────
session.sql("""
CREATE TABLE IF NOT EXISTS AICOLLEGE.PUBLIC.AGENT_STATUS (
  AGENT_NAME STRING,
  USERNAME   STRING,
  LAST_CHECKIN TIMESTAMP_NTZ
)
""").collect()

# Record agent check-in
session.sql("""
MERGE INTO AICOLLEGE.PUBLIC.AGENT_STATUS t 
USING (
  SELECT 'agent.py' AS agent_name, 
         CURRENT_USER() AS username, 
         CURRENT_TIMESTAMP() AS last_checkin
) s
ON t.AGENT_NAME = s.agent_name AND t.USERNAME = s.username
WHEN MATCHED THEN 
  UPDATE SET LAST_CHECKIN = s.last_checkin
WHEN NOT MATCHED THEN 
  INSERT (AGENT_NAME, USERNAME, LAST_CHECKIN) 
  VALUES (s.agent_name, s.username, s.last_checkin)
""").collect()

# ─── utility helpers ────────────────────────────────────────────────────────
def _english_list(items: List[str]) -> str:
    """Return 'A', 'A and B', or 'A, B and C'."""
    if not items: return ""
    return ", ".join(items[:-1]) + (" and " if len(items)>1 else "") + items[-1]

def _local_short_summary(df: pd.DataFrame) -> str | None:
    """Very small (≤10 × 2) tables → quick deterministic summary."""
    if df.empty or len(df)>10 or df.shape[1]>2: return None
    if df.shape[1]==1:
        vals = df.iloc[:,0].dropna().astype(str).tolist()
        return f"{len(vals)} found: {_english_list(vals)}." if vals else None
    a,b = df.columns[:2]
    bits = [f"{row[b]} for {row[a]}" for _,row in df.iterrows()]
    return (_english_list(bits)+"." if bits else None)

def _llm_question_and_answer(q: str, tbl: str) -> str | None:
    """Ask Analyst to write a two-paragraph supplement if local summary isn't enough."""
    prompt = {
        "model": "mistral-large2",
        "messages": [
            {"role":"user",
             "content":[{"type":"text","text":
               f"I ran this SQL:\n```sql\n{q}\n```\n\n"
               f"Result:\n```text\n{tbl}\n```\n\n"
               "Please give me a two-paragraph explanation."
             }]}
        ]
    }
    try:
        r = requests.post(ANALYST_ENDPOINT, json=prompt, headers={
            "Authorization": f"Bearer {SNOWFLAKE_PAT}",
            "X-Snowflake-Authorization-Token-Type": "PROGRAMMATIC_ACCESS_TOKEN",
            "Content-Type":"application/json",
            "Accept":"application/json"
        }, timeout=60)
        r.raise_for_status()
        blocks = r.json().get("message",{}).get("content",[])
        return "\n\n".join(b["text"] for b in blocks if b.get("type")=="text") or None
    except Exception as e:
        logger.warning("LLM Q&A generation failed: %s", e)
        return None

# ─── main agent call ─────────────────────────────────────────────────────────
def ask_agent(user_q: str) -> Dict[str,Any]:
    """
    Send a user question to the Cortex Agent API and return:
      • 'answer':  raw text or a SQL table string
      • 'sql':     the SQL statement if returned by the agent, else None
      • 'summary': a brief natural-language summary (for SQL results), else None
    """
    # Extract customer mention if present
    m = re.search(
        r"(Acme Corp|BetaTech|Gamma LLC|Delta Enterprises|Omega Industries|Zeta Solutions)",
        user_q, flags=re.I
    )
    cust = m.group(0) if m else ""

    # Build the agent:run payload - focus on using the semantic model
    payload = {
        "agent_name": AGENT_NAME,
        "model": "mistral-large2",
        "stream": False,
        "tools": [
            {"tool_spec": {"type": "cortex_analyst_text_to_sql", "name": "ANALYST_SERVICE"}}
        ],
        "tool_resources": {
            "ANALYST_SERVICE": {
                "semantic_model_file": SEMANTIC_MODEL_URI
            }
        },
        "tool_choice": {"type": "auto"},
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": user_q}]}
        ]
    }

    logger.debug("Sending to Cortex Agent API: %s", json.dumps(payload, indent=2))

    try:
        # Call the Cortex Agent API
        resp = requests.post(
            AGENT_ENDPOINT, 
            json=payload, 
            headers={
                "Authorization": f"Bearer {SNOWFLAKE_PAT}",
                "X-Snowflake-Authorization-Token-Type": "PROGRAMMATIC_ACCESS_TOKEN",
                "Content-Type": "application/json",
                "Accept": "application/json"
            }, 
            timeout=60
        )
        
        # Check for HTTP errors
        if not resp.ok:
            logger.error("Agent API error: %s - %s", resp.status_code, resp.text)
            return _simple_fallback_sql(user_q)
        
        # Handle empty responses
        if not resp.text.strip():
            logger.error("Empty response from Agent API")
            return _simple_fallback_sql(user_q)
        
        # Try to parse as JSON
        try:
            data = resp.json()
            blocks = data.get("message", {}).get("content", [])
            texts = [b for b in blocks if b.get("type") == "text"]
            sqls = [b for b in blocks if b.get("type") == "sql"]
            
            # If SQL was returned, execute it
            if sqls:
                stmt = sqls[0].get("statement", "")
                if stmt:
                    try:
                        # Fix for double underscore issue in temporary tables
                        modified_stmt = re.sub(r'WITH\s+__(\w+)\s+AS', r'WITH \1 AS', stmt)
                        
                        df = session.sql(modified_stmt).to_pandas()
                        table_str = df.to_string(index=False, max_rows=50)
                        interp = next((b["text"] for b in texts if b.get("text", "").strip()), "")
                        summary = _local_short_summary(df) or _llm_question_and_answer(stmt, table_str)
                        return {
                            "answer": table_str, 
                            "sql": stmt, 
                            "summary": "\n\n".join(p for p in (interp, summary) if p) or None
                        }
                    except Exception as e:
                        logger.exception("Error executing SQL: %s", e)
                        return {"answer": f"❌ Error executing SQL: {e}", "sql": stmt, "summary": None}
            
            # Otherwise, return text response
            answer_text = " ".join(b["text"] for b in texts).strip() or "❓ No text returned"
            return {"answer": answer_text, "sql": None, "summary": None}
            
        except json.JSONDecodeError:
            # Handle streaming responses
            if "event: message.delta" in resp.text:
                # Try to extract SQL from streaming response
                sql_match = re.search(r'"sql":"(.*?)","verified_query_used"', resp.text)
                if sql_match:
                    stmt = sql_match.group(1).replace('\\n', '\n').replace('\\\"', '"').replace('\\\\', '\\')
                    text_match = re.search(r'"text":"(.*?)"', resp.text)
                    interp = text_match.group(1).replace('\\n', '\n') if text_match else ""
                    
                    # Fix for double underscore issue and execute SQL
                    modified_stmt = re.sub(r'WITH\s+__(\w+)\s+AS', r'WITH \1 AS', stmt)
                    try:
                        df = session.sql(modified_stmt).to_pandas()
                        table_str = df.to_string(index=False, max_rows=50)
                        summary = _local_short_summary(df) or _llm_question_and_answer(stmt, table_str)
                        return {
                            "answer": table_str, 
                            "sql": stmt, 
                            "summary": "\n\n".join(p for p in (interp, summary) if p) or None
                        }
                    except Exception as e:
                        logger.exception("Error executing SQL: %s", e)
                        return {"answer": f"❌ Error executing SQL: {e}", "sql": stmt, "summary": None}
                
                # Try to extract text content
                content_match = re.search(r'"content":\[{"type":"text","text":"(.*?)"}', resp.text)
                if content_match:
                    answer_text = content_match.group(1).replace('\\n', '\n')
                    return {"answer": answer_text, "sql": None, "summary": None}
            
            logger.error("Failed to parse response: %s", resp.text[:200])
            return _simple_fallback_sql(user_q)
            
    except Exception as e:
        logger.exception("Error calling Cortex Agent: %s", e)
        return _simple_fallback_sql(user_q)
    
def _simple_fallback_sql(user_q: str) -> Dict[str, Any]:
    """Simple fallback for when the API fails completely."""
    # Just return a list of customers as a last resort
    stmt = """
    SELECT DISTINCT
      customer_name
    FROM aicollege.public.customer_meetings
    ORDER BY
      customer_name ASC
    LIMIT 1000
    """
    interp = "I couldn't process your specific question through the Cortex Agent API. Here's a list of customers in our database:"
    
    try:
        df = session.sql(stmt).to_pandas()
        table_str = df.to_string(index=False, max_rows=50)
        summary = _local_short_summary(df)
        return {
            "answer": table_str, 
            "sql": stmt, 
            "summary": f"{interp}\n\n{summary}" if summary else interp
        }
    except Exception as e:
        logger.exception(f"Error executing SQL: {e}")
        return {
            "answer": f"❌ Error executing SQL: {e}", 
            "sql": stmt, 
            "summary": None
        }

# ─── Slack wiring ─────────────────────────────────────────────────────────────
app = App(token=SLACK_BOT_TOKEN)

@app.message("hello")
def greet(msg, say):
    say(f"Hey <@{msg['user']}>! :snowflake:")
    say("Ask me anything, or type SEAI55/SEAI56 to grade Phase 2.")

@app.command("/askcortex")
def slash_ask(ack, body, say):
    ack()
    q = body.get("text","").strip()
    if not q:
        return say("Please include a question after /askcortex.")
    _handle(q, say)

@app.event("message")
def catch_all(ack, body, say):
    if body.get("event",{}).get("bot_id"):
        return ack()
    ack()
    txt = body["event"]["text"].strip()
    up = txt.upper()
    if up in ("SEAI55","SEAI56"):
        say(f":hourglass: grading {up}…")
        return
    _handle(txt, say)

def _handle(msg: str, say):
    say(text=f"*Question:* {msg}")
    say(text="Snowflake Cortex Agent is thinking… :hourglass_flowing_sand:")
    try:
        # ⛳️ Shortcut: if user typed SEAI57, run DORA grading logic
        if msg.strip().upper() == "SEAI57":
            df = session.sql("""
                SELECT util_db.public.se_grader(
                  'SEAI57',
                  (
                    SELECT COUNT(*)
                    FROM AICOLLEGE.PUBLIC.AGENT_STATUS
                    WHERE AGENT_NAME = 'agent.py'
                      AND USERNAME = CURRENT_USER()
                      AND LAST_CHECKIN >= DATEADD('hour', -4, CURRENT_TIMESTAMP())
                  ) >= 1,
                  (
                    SELECT COUNT(*)
                    FROM AICOLLEGE.PUBLIC.AGENT_STATUS
                    WHERE AGENT_NAME = 'agent.py'
                      AND USERNAME = CURRENT_USER()
                      AND LAST_CHECKIN >= DATEADD('hour', -4, CURRENT_TIMESTAMP())
                  ),
                  1,
                  '✅ Your Slack-based agent was detected and checked in recently!'
                ) AS graded_results
            """).to_pandas()

            summary = "✅ Your agent was successfully detected and graded."
            answer  = df.to_string(index=False)
            say(text=f"*Summary:* {summary}")
            say(text=f"*Answer:*\n{answer}")
            return

        # For all other questions, use the agent
        res = ask_agent(msg)
        if res.get("summary"):
            say(text=f"*Summary:* {res['summary']}")
        
        code_wrapped = f"```\n{res['answer']}\n```" if res.get("sql") else res["answer"]
        say(text=f"*Answer:*\n{code_wrapped}")
        
        if res.get("sql"):
            say(
                text="Would you like to see the SQL I ran?",
                blocks=[
                    {
                        "type": "section",
                        "text": {"type": "mrkdwn", "text": "Would you like to see the SQL I ran?"}
                    },
                    {
                        "type": "actions",
                        "elements": [{
                            "type": "button",
                            "text": {"type": "plain_text", "text": "Show SQL"},
                            "action_id": "show_sql",
                            "value": res["sql"]
                        }]
                    }
                ]
            )
    except Exception as e:
        logger.exception("agent error")
        say(text=f":x: Whoops, I hit an error: {e}")

@app.action("show_sql")
def show_sql(ack, body, say):
    ack()
    user = body["user"]["id"]
    stmt = body["actions"][0]["value"]
    say(f"<@{user}>, here's the SQL I ran:\n```sql\n{stmt}\n```")

if __name__ == "__main__":
    print("🚀 CollegeAI Data-Agent up")
    SocketModeHandler(app, SLACK_APP_TOKEN).start()
