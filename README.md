# ETL Pipeline — DataWarehouse (Printer Sales)

Two ETL projects analyzing the commercial activity of a printer sales company, loading data into a star-schema PostgreSQL DataMart.

---

## Projects

### dtw1 — Single-file ETL
A self-contained pipeline in `dtw1/etl_pipeline.py` with a Power BI dashboard (`dtw1/vis.pbix`) for data visualization.

### dtw2 — Modular ETL
A three-stage pipeline split across dedicated scripts:
- `dtw2/load_staging.py` — raw data ingestion
- `dtw2/clean_load.py` — data cleaning
- `dtw2/transform_load.py` — transformations and loading

---

## Data Sources (4 heterogeneous sources)

| # | File | Type | Content |
|---|------|------|---------|
| 1 | `01_source_mysql_printer_sales.sql` | MySQL dump | Vendors, clients, products, orders (~1100 rows) |
| 2 | `02_route_logs.txt` | Pipe-delimited TXT | Sales rep travel logs (~1000 rows) |
| 3 | `03_sales_promises.xlsx` | Excel | Sales promises (~780 rows) |
| 4 | `04_fuel_expenses.json` | JSON | Fuel and travel expenses (~900 objects) |

---

## Star Schema (Target — PostgreSQL)

**Dimensions:** `DIM_TEMPS`, `DIM_VENDEUR`, `DIM_CLIENT`, `DIM_PRODUIT`, `DIM_GEO`

**Fact table grain:** one row per `day + vendor + client + product`

**Key metrics:** net sales amount, estimated margin, net profitability, km traveled, fuel consumption

---

## Key Transformations

- Normalize codes (`trim`, `upper`)
- Unify date formats → `DATE`
- Convert text decimals → `NUMERIC`
- Flatten nested JSON objects
- Aggregate travel data by vendor and day
- Resolve surrogate keys across all dimensions

---

## Setup

```bash
pip install openpyxl psycopg2-binary

# Run ETL then load into PostgreSQL
python3 etl_pipeline.py
psql -U postgres -d dwh_imprimantes -f sources/05_load_postgres.sql
```