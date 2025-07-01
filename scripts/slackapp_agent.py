# slackapp_agent.py

import os, re, time, logging, warnings
from typing import Any, Dict, List

import requests
import pandas as pd
from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from snowflake.snowpark.session import Session
from snowflake.core import Root
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.backends import default_backend

# ─── suppress Slack SDK missing-text warning ─────────────────────────────────
warnings.filterwarnings(
    "ignore",
    message="The top-level text argument is missing",
    category=UserWarning
)

# ─── load env vars ───────────────────────────────────────────────────────────
load_dotenv("dataagent.env")
logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
SLACK_APP_TOKEN = os.environ["SLACK_APP_TOKEN"]

SF_ACCOUNT         = os.environ["SNOWFLAKE_ACCOUNT"]
SF_DATABASE        = os.environ["SF_DATABASE"]
SF_SCHEMA          = os.environ["SF_SCHEMA"]
SF_STAGE           = os.environ["SF_STAGE"]
SF_MODEL_FILE      = os.environ["SF_MODEL_FILE"]
SEMANTIC_MODEL_URI = f"@{SF_DATABASE}.{SF_SCHEMA}.{SF_STAGE}/{SF_MODEL_FILE}"
SNOWFLAKE_PAT      = os.environ["SNOWFLAKE_PAT"]
ANALYST_ENDPOINT   = f"https://{SF_ACCOUNT}.snowflakecomputing.com/api/v2/cortex/analyst/message"
MODEL_NAME         = "mistral-large2"

SF_USER          = os.environ["SNOWFLAKE_USER"]
SF_KEY_FILE      = os.environ["SNOWFLAKE_PRIVATE_KEY_PATH"]
SF_KEY_PASSPH    = os.environ.get("SNOWFLAKE_PRIVATE_KEY_PASSPHRASE", "")
SF_ROLE          = os.environ["SF_ROLE"]
SF_WAREHOUSE     = os.environ["SF_WAREHOUSE"]
SEARCH_SERVICE   = "TRANSCRIPTS_SEARCH_SERVICE"

# ─── load & decrypt your RSA key ─────────────────────────────────────────────
with open(SF_KEY_FILE, "rb") as f:
    pem = f.read()
pkey = serialization.load_pem_private_key(
    pem,
    password=SF_KEY_PASSPH.encode() if SF_KEY_PASSPH else None,
    backend=default_backend()
)
der_key = pkey.private_bytes(
    encoding=serialization.Encoding.DER,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption()
)

# ─── build a Snowpark session ────────────────────────────────────────────────
session = Session.builder.configs({
    "account":       SF_ACCOUNT,
    "user":          SF_USER,
    "authenticator": "SNOWFLAKE_JWT",
    "private_key":   der_key,
    "role":          SF_ROLE,
    "warehouse":     SF_WAREHOUSE,
    "database":      SF_DATABASE,
    "schema":        SF_SCHEMA,
}).create()

# ─── DORA: log agent check-in ────────────────────────────────────────────────
session.sql("""
    CREATE TABLE IF NOT EXISTS AICOLLEGE.PUBLIC.AGENT_STATUS (
        AGENT_NAME   STRING,
        USERNAME     STRING,
        LAST_CHECKIN TIMESTAMP_NTZ
    )
""").collect()

session.sql(f"""
    MERGE INTO AICOLLEGE.PUBLIC.AGENT_STATUS t
    USING (
        SELECT 
            'slackapp_agent.py' AS agent_name,
            CURRENT_USER()      AS username,
            CURRENT_TIMESTAMP() AS last_checkin
    ) s
    ON t.AGENT_NAME = s.agent_name AND t.USERNAME = s.username
    WHEN MATCHED THEN UPDATE SET LAST_CHECKIN = s.last_checkin
    WHEN NOT MATCHED THEN INSERT (AGENT_NAME, USERNAME, LAST_CHECKIN)
    VALUES (s.agent_name, s.username, s.last_checkin)
""").collect()

# ─── wire up Cortex Search ───────────────────────────────────────────────────
root       = Root(session)
search_svc = (
    root
    .databases[SF_DATABASE]
    .schemas[SF_SCHEMA]
    .cortex_search_services[SEARCH_SERVICE]
)

# ─── utility helpers ────────────────────────────────────────────────────────
def _english_list(items: list[str]) -> str:
    """Return 'A', 'A and B', or 'A, B and C'."""
    if not items:
        return ""
    return ", ".join(items[:-1]) + (" and " if len(items) > 1 else "") + items[-1]

def _summarise_df(df: pd.DataFrame) -> str | None:
    """
    Return a one-sentence natural-language summary of a small result set.

    • 1-column → counts + list
    • 2-columns → '<B> for <A>' pairs
    """
    if df.empty:
        return None

    cols = list(df.columns)
    if len(cols) == 1:
        vals = df[cols[0]].dropna().astype(str).tolist()
        if not vals:
            return None
        plural = "s" if len(vals) != 1 else ""
        return f"You have {len(vals)} {cols[0].lower()}{plural}: {_english_list(vals)}."
    if len(cols) == 2:
        a, b = cols
        phrases = [
            f"{str(row[b]).lower()} for {str(row[a])}"
            for _, row in df.iterrows()
            if pd.notna(row[a]) and pd.notna(row[b])
        ]
        if phrases:
            phr = "; ".join(phrases).rstrip(".")
            # Capitalise first letter
            return phr[0].upper() + phr[1:] + "."
    return None

# ─── pre-verified starter SQL to FLATTEN key_phrases ───────────────────────
KEY_PHRASE_HINT = """
-- key phrases are stored as an ARRAY → explode them
SELECT
       cm.customer_name,
       kp.value::string AS key_phrase
FROM transcript_facts tf,
     LATERAL FLATTEN(input => tf.key_phrases) kp
JOIN customer_meetings cm
     ON tf.meeting_id = cm.meeting_id
"""

# ─── natural-language supplement helpers ────────────────────────────────────
_MAX_TABLE_CHARS = 3500         # keep Analyst payload safe

def _clip(txt: str, limit: int = _MAX_TABLE_CHARS) -> str:
    return txt if len(txt) <= limit else txt[:limit] + "\n…[truncated]"

def _english_list(items: list[str]) -> str:
    if not items:
        return ""
    return ", ".join(items[:-1]) + (" and " if len(items) > 1 else "") + items[-1]

def _local_short_summary(df: pd.DataFrame) -> str | None:
    """Very small (≤10 × 2) tables → quick deterministic summary"""
    if df.empty or len(df) > 10 or df.shape[1] > 2:
        return None
    if df.shape[1] == 1:                       # one column list
        vals = df.iloc[:, 0].dropna().astype(str).tolist()
        if vals:
            return f"{len(vals)} found: {_english_list(vals)}."
    else:                                      # two columns “B for A”
        a, b = df.columns[:2]
        bits = [f"{row[b]} for {row[a]}" for _, row in df.iterrows()]
        if bits:
            return _english_list(bits) + "."
    return None

def _llm_question_and_answer(question: str, table_txt: str) -> str | None:
    """Ask Analyst/Complete to write a two-paragraph supplement."""
    payload = {
        "model": MODEL_NAME,
        "messages": [
            {"role": "system",
             "content": [{"type": "text", "text":
                 ("Rewrite the user’s question in plain language (1 sentence). "
                  "Then answer it in 1-2 sentences using ONLY the SQL result. "
                  "Highlight key insights; do not list every row.")}]},
            {"role": "user",
             "content": [{"type": "text", "text":
                 f"Question: {question}\n\nSQL result:\n{_clip(table_txt)}"}]},
        ],
    }
    try:
        out = call_analyst(payload)    # run via existing inner helper
        texts = [b["text"] for b in out["message"]["content"] if b["type"] == "text"]
        return "\n\n".join(t.strip() for t in texts if t.strip()) or None
    except Exception:
        return None

def search_chunks(question: str, customer: str, k: int = 5) -> List[Dict[str,Any]]:
    hits = search_svc.search(
        query=question,
        columns=["CHUNK","CUSTOMER_NAME","RELATIVE_PATH"],
        limit=k
    ).results
    cust = [h for h in hits if h.get("CUSTOMER_NAME","").lower() == customer.lower()]
    return cust or hits

# ─── Cortex Analyst Agent Logic ──────────────────────────────────────────────
def run_agent(question: str, customer: str) -> Dict[str,Any]:
    def call_analyst(payload):
        start = time.time()
        r = requests.post(
            ANALYST_ENDPOINT,
            json=payload,
            headers={
                "Authorization":                        f"Bearer {SNOWFLAKE_PAT}",
                "X-Snowflake-Authorization-Token-Type": "PROGRAMMATIC_ACCESS_TOKEN",
                "Content-Type":                         "application/json",
                "Accept":                               "application/json",
            },
            timeout=60,
        )
        r.raise_for_status()
        elapsed = time.time() - start
        data = r.json()
        logger.info("Analyst call (%.1fs): got %d blocks", elapsed, len(data["message"]["content"]))
        return data

    if re.search(r"\b(count|list|how many|average)\b", question, flags=re.I) \
        or re.search(r"\bshow\s+(?:me|the|\d+)", question, flags=re.I):
        sql_req = {
            "model": MODEL_NAME,
            "semantic_model_file": SEMANTIC_MODEL_URI,
            "messages": [
                { "role": "system",
                  "content": [{"type": "text", "text":
                      "You are an expert Snowflake data assistant. "
                      "Return one sql block, then a 1-2 sentence answer."}]},
                { "role": "user",
                  "content": [{"type": "text", "text": question}]}
            ],
        }

        if re.search(r"key[_\s-]?phrases?", question, flags=re.I):
            sql_req["messages"].insert(1, {
                "role": "system",
                "content": [{"type": "text",
                             "text": "Start from this SQL:\n" + KEY_PHRASE_HINT}]
            })

        resp_json = call_analyst(sql_req)
        sql_blocks = [b for b in resp_json["message"]["content"] if b["type"]=="sql"]
        if sql_blocks:
            sql_stmt = sql_blocks[0]["statement"]
            df = session.sql(sql_stmt).to_pandas()
            answer_table = df.to_string(index=False, max_rows=50)

            # ① Analyst’s own interpretation (if any)
            interp = next(
                (b["text"] for b in resp_json["message"]["content"]
                 if b["type"] == "text" and b["text"].strip()), ""
            )

            # ② Our summary (local for tiny tables, LLM for larger)
            df_summary = _local_short_summary(df) or \
                         _llm_question_and_answer(question, answer_table)

            summary_parts = [s for s in (interp, df_summary) if s]
            summary = "\n\n".join(summary_parts) or None

            return {"answer": answer_table, "sql": sql_stmt, "summary": summary}


    # Otherwise → RAG
    ctx_chunks = search_chunks(question, customer, k=5)
    context = "\n\n---\n\n".join(c["CHUNK"] for c in ctx_chunks)

    rag_req = {
        "model":               MODEL_NAME,
        "semantic_model_file": SEMANTIC_MODEL_URI,
        "messages":[ {
            "role":"user",
            "content":[
                {"type":"text","text": question},
                {"type":"text","text": f"Context:\n<<<\n{context}\n>>>"}
            ]
        }]
    }
    rag_json = call_analyst(rag_req)

    texts = [b for b in rag_json["message"]["content"] if b["type"]=="text"]
    sqls  = [b for b in rag_json["message"]["content"] if b["type"]=="sql"]
    logger.info("RAG branch: %d text, %d sql", len(texts), len(sqls))

    if sqls:
        sql_stmt = sqls[0]["statement"]
        df = session.sql(sql_stmt).to_pandas()
        answer_table = df.to_string(index=False, max_rows=50)

        interp = next(
            (b["text"] for b in rag_json["message"]["content"]
             if b["type"] == "text" and b["text"].strip()), ""
        )

        df_summary = _local_short_summary(df) or \
                     _llm_question_and_answer(question, answer_table)

        summary_parts = [s for s in (interp, df_summary) if s]
        summary = "\n\n".join(summary_parts) or None

        return {"answer": answer_table, "sql": sql_stmt, "summary": summary}

    all_text = [b["text"] for b in texts]
    if all_text and all_text[0].lower().startswith("this is our interpretation"):
        all_text = all_text[1:]
    free_text = "\n\n".join(all_text).strip() or "❓ no text returned"
    return {"answer": free_text, "sql": None, "summary": None}

def ask_agent(user_q: str) -> Dict[str,Any]:
    m = re.search(
        r"(Acme Corp|BetaTech|Gamma LLC|Delta Enterprises|Omega Industries|Zeta Solutions)",
        user_q, flags=re.I
    )
    cust = m.group(0) if m else ""
    return run_agent(user_q, cust)

# ─── Slack wiring ─────────────────────────────────────────────────────────────
app = App(token=SLACK_BOT_TOKEN)

@app.message("hello")
def greet(msg, say):
    say(f"Hey <@{msg['user']}>! :snowflake:")
    say("Ask me anything, or type *SEAI55*/*SEAI56* to grade Phase 2.")

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
    up  = txt.upper()
    if up in ("SEAI55","SEAI56"):
        say(f":hourglass: grading {up} …")
        return
    _handle(txt, say)

def _handle(msg: str, say):
    say(text=f"*Question:* {msg}")
    say(text="Snowflake Cortex Agent is thinking… :hourglass_flowing_sand:")

    try:
        # ⛳️ Shortcut: if user typed SEAI57, run the grading SQL directly
        if msg.strip().upper() == "SEAI57":
            df = session.sql("""
                SELECT util_db.public.se_grader(
                  'SEAI57',
                  (
                    SELECT COUNT(*)
                    FROM AICOLLEGE.PUBLIC.AGENT_STATUS
                    WHERE AGENT_NAME = 'slackapp_agent.py'
                      AND USERNAME = CURRENT_USER()
                      AND LAST_CHECKIN >= DATEADD('hour', -4, CURRENT_TIMESTAMP())
                  ) >= 1,
                  (
                    SELECT COUNT(*)
                    FROM AICOLLEGE.PUBLIC.AGENT_STATUS
                    WHERE AGENT_NAME = 'slackapp_agent.py'
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

        # Default: normal RAG / SQL response
        res = ask_agent(msg)
        if res.get("summary"):
            say(text=f"*Summary:* {res['summary']}")
        code_wrapped = f"```{res['answer']}```" if res.get("sql") else res["answer"]
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
    say(f"<@{user}>, here’s the SQL I ran:\n```{stmt}```")

if __name__ == "__main__":
    print("🚀 CollegeAI Data-Agent up")
    SocketModeHandler(app, SLACK_APP_TOKEN).start()
