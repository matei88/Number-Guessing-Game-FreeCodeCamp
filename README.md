# Data Assistant

A conversational AI application for **synthetic data generation** and **natural-language data querying**, built with Gemini 2.0 Flash, Streamlit, PostgreSQL, and Langfuse.

---

## Table of Contents

- [Architecture](#architecture)
- [Prerequisites](#prerequisites)
- [Setup](#setup)
- [Running the App](#running-the-app)
- [Usage Guide](#usage-guide)
- [Testing](#testing)
- [Environment Variables](#environment-variables)
- [Project Structure](#project-structure)

---

## Architecture

```
┌─────────────────────────────────────────────────┐
│                  Streamlit UI                   │
│   Data Generation tab │ Talk to your data tab   │
└────────────┬──────────────────┬─────────────────┘
             │                  │
     ┌───────▼──────┐   ┌───────▼──────────┐
     │ DataGenerator│   │ TalkToDataManager│
     │ (Gemini JSON │   │ (Gemini function │
     │  structured  │   │   calling + SQL) │
     │   output)    │   └───────┬──────────┘
     └───────┬──────┘           │
             │           ┌──────▼──────┐
     ┌───────▼───────────▼──┐  │ Plotly  │
     │   DatabaseManager    │  │ Charts  │
     │   (PostgreSQL via    │  └─────────┘
     │    SQLAlchemy)       │
     └──────────────────────┘
             │
     ┌───────▼──────┐
     │   Langfuse   │  ← observability (optional)
     └──────────────┘
```

**Key design decisions:**

| Concern | Approach |
|---|---|
| DDL parsing | Regex-based parser handles ENUM, FK, CHECK, AUTO_INCREMENT, ALTER TABLE; topological sort ensures FK-correct generation order |
| Structured output | `response_mime_type="application/json"` for deterministic row generation; 50-row batches to stay within token limits |
| Function calling | `execute_sql` Gemini tool in Talk-to-data — model picks SQL + best chart type |
| FK integrity | Parent tables are always generated before child tables; FK column values are sampled from already-generated data |
| Circular deps | Detected and broken gracefully; circular FK columns are left nullable and filled in post-generation |

---

## Prerequisites

- **Docker & Docker Compose** — for the full stack (recommended)
- **Python 3.11+** — for local development without Docker
- **Google Cloud credentials** — either a GCP project with Vertex AI enabled, or a Google AI Studio API key

---

## Setup

### 1. Clone and enter the repo

```bash
git clone https://github.com/matei88/Number-Guessing-Game-FreeCodeCamp.git
cd Number-Guessing-Game-FreeCodeCamp
```

### 2. Configure environment variables

```bash
cp .env.example .env
```

Edit `.env` and fill in **one** of the two Google AI options:

```dotenv
# Option A: Vertex AI (production)
GOOGLE_CLOUD_PROJECT=your-gcp-project-id
GOOGLE_CLOUD_LOCATION=us-central1

# Option B: Google AI Studio (local dev, simpler)
GOOGLE_API_KEY=your-api-key-from-aistudio.google.com
```

Langfuse keys are optional — the app runs fine without them (tracking is silently skipped):

```dotenv
LANGFUSE_BASE_URL=http://localhost:3000
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
```

### 3. Vertex AI authentication (Option A only)

If using Vertex AI, authenticate the Docker container via Application Default Credentials:

```bash
gcloud auth application-default login
# This writes credentials to ~/.config/gcloud/application_default_credentials.json
```

Then add to `docker-compose.yml` under the `app` service:

```yaml
volumes:
  - ~/.config/gcloud:/root/.config/gcloud:ro
```

---

## Running the App

### With Docker Compose (recommended)

```bash
docker compose up --build
```

This starts three services:

| Service | URL | Purpose |
|---|---|---|
| `app` | http://localhost:8501 | Streamlit UI |
| `postgres` | localhost:5432 | Data storage |
| `langfuse` | http://localhost:3000 | Observability dashboard |

Wait ~30 seconds for all services to become healthy, then open http://localhost:8501.

To stop:

```bash
docker compose down          # keep data
docker compose down -v       # also delete the postgres volume
```

### Local development (no Docker)

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Start a local PostgreSQL instance (or point DATABASE_URL at an existing one)
# e.g. via Homebrew: brew services start postgresql@15

# 3. Create the app database
psql -U postgres -c "CREATE DATABASE data_assistant;"

# 4. Run the app
streamlit run app.py
```

---

## Usage Guide

### Phase 1 — Data Generation

1. **Load a schema** — either click one of the sample schema buttons in the sidebar (`company_employee`, `library_mgm`, `restaurants`) or click **Upload DDL Schema** to upload your own `.sql` / `.ddl` / `.txt` file.

2. **Write a prompt** *(optional)* — describe any special requirements, e.g.:
   - *"Use US cities only"*
   - *"Set salaries between $40k and $120k"*
   - *"Make 30% of orders cancelled"*

3. **Set parameters:**
   - **Temperature** — higher = more varied/creative data
   - **Max Tokens** — maximum tokens per Gemini call (default 8192)
   - **Rows per table** — 10–1000 (default 100)

4. **Click Generate** — a progress bar tracks each table. Generated data is automatically saved to PostgreSQL.

5. **Preview** — select a table from the dropdown to browse up to 20 rows.

6. **Download** — export a single table as CSV or all tables as a ZIP archive.

7. **Quick edit** — type an instruction in the text box and click **Submit** to modify the current table, e.g.:
   - *"Set all ratings to 4 or 5"*
   - *"Change all employment_status to Full-time"*

### Phase 2/3 — Talk to Your Data

Switch to the **Talk to your data** tab and ask questions in plain English:

```
How many employees are in each department?
What is the average salary by job title?
Show me the top 5 most ordered menu items.
Which restaurants have an average rating above 4?
Plot the distribution of order statuses.
```

Results are shown as:
- **Tables** — for multi-column results
- **Bar / line / pie / scatter charts** — Gemini picks the best type automatically
- **Inline text** — for single-value answers

Each response includes an expandable **SQL** block showing the exact query that was run.

---

## Testing

### Verify the DDL parser

```bash
python3 -c "
from src.schema_parser import parse_ddl, topological_sort
from pathlib import Path

for f in sorted(Path('schemas').glob('*.ddl')):
    tables = parse_ddl(f.read_text())
    order = topological_sort(tables)
    print(f'{f.stem}: {len(tables)} tables, order={order}')
"
```

Expected output — all three schemas parsed with correct dependency order:

```
company_employee: 7 tables, order=['Companies', 'Departments', 'Employees', ...]
library_mgm: 9 tables, order=['Authors', 'Publishers', 'Books', ...]
restaurants: 7 tables, order=['Restaurants', 'Customers', 'Orders', ...]
```

### Verify the database connection

```bash
python3 -c "
from src.database import DatabaseManager
db = DatabaseManager.from_env()
print('Connected. Tables:', db.get_table_names())
"
```

### End-to-end smoke test (requires Google credentials)

```bash
python3 -c "
from pathlib import Path
from src.schema_parser import parse_ddl
from src.data_generator import DataGenerator

ddl = Path('schemas/restaurants.ddl').read_text()
tables = parse_ddl(ddl)
gen = DataGenerator(temperature=0.7)

for name, df in gen.generate_all(tables, num_rows=5):
    print(f'{name}: {len(df)} rows, columns={list(df.columns)}')
"
```

### Docker health check

```bash
docker compose ps          # all services should show "healthy" or "running"
curl http://localhost:8501/_stcore/health   # should return "ok"
```

---

## Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `GOOGLE_CLOUD_PROJECT` | One of A/B | — | GCP project ID for Vertex AI |
| `GOOGLE_CLOUD_LOCATION` | No | `us-central1` | Vertex AI region |
| `GOOGLE_API_KEY` | One of A/B | — | Google AI Studio API key |
| `GEMINI_MODEL` | No | `gemini-2.0-flash` | Gemini model ID |
| `DATABASE_URL` | Yes | `postgresql://postgres:postgres@localhost:5432/data_assistant` | PostgreSQL connection string |
| `LANGFUSE_BASE_URL` | No | `http://localhost:3000` | Langfuse server URL |
| `LANGFUSE_PUBLIC_KEY` | No | — | Langfuse public key (tracing disabled if unset) |
| `LANGFUSE_SECRET_KEY` | No | — | Langfuse secret key |

---

## Project Structure

```
.
├── app.py                    # Main Streamlit application
├── src/
│   ├── schema_parser.py      # DDL → Table dataclasses + topological sort
│   ├── data_generator.py     # Gemini-based row generation (JSON mode, batched)
│   ├── database.py           # PostgreSQL read/write via SQLAlchemy
│   ├── talk_to_data.py       # NL → SQL via Gemini function calling + Plotly charts
│   └── langfuse_client.py    # Observability wrapper (graceful no-op if unconfigured)
├── schemas/
│   ├── company_employee.ddl  # 7-table HR/project schema
│   ├── restaurants.ddl       # 7-table restaurant/delivery schema
│   └── library_mgm.ddl       # 9-table library management schema
├── docker-compose.yml        # App + PostgreSQL + Langfuse
├── Dockerfile                # Python 3.11-slim image
├── init-db.sql               # Creates data_assistant and langfuse databases
├── requirements.txt
└── .env.example              # Template for environment variables
```
