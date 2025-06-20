# check_deps.py
import importlib

# This script verifies that all required dependencies are installed and importable.
packages = {
    "python-dotenv":                      "dotenv",
    "slack-bolt":                         "slack_bolt",
    "requests":                           "requests",
    "pandas":                             "pandas",
    "snowflake-connector-python":         "snowflake.connector",
    "snowflake-connector-python[pandas]": "snowflake.connector.pandas_tools",
    "snowflake-snowpark-python":          "snowflake.snowpark.session",
    "snowflake-core":                     "snowflake.core",
    "SQLAlchemy":                         "sqlalchemy",
    "snowflake-sqlalchemy":               "snowflake.sqlalchemy"
}

missing = []
for pkg_name, module_name in packages.items():
    try:
        importlib.import_module(module_name)
        print(f"✅ {pkg_name}")
    except ImportError:
        print(f"❌ {pkg_name}")
        missing.append(pkg_name)

if missing:
    print("\nMissing packages:", ", ".join(missing))
    print("Install with:\n  pip install " + " ".join(missing))
