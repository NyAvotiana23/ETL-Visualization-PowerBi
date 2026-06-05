#!/usr/bin/env python3
"""
transform_load.py
-----------------
Lit les tables de staging (staging_load.sql), puis génère deux fichiers SQL :

    1. clean_load.sql   — mêmes tables que le staging, données nettoyées
                          (schéma : clean)
                          • hr_vendeurs, ref_clients, ref_produits, sales_orders
                          • stg_route_logs, stg_sales_promises, stg_fuel_expenses

    2. transform_load.sql — dimensions + table de faits
                          (schéma : dw)
                          • dim_date, dim_seller, dim_customer, dim_product
                          • fact_sales_activity

Transformations appliquées au nettoyage :
    - UPPER(TRIM(code))  sur tous les codes métier
    - harmonisation des formats de date  → YYYY-MM-DD
    - conversion des décimaux texte      → NUMERIC  (virgule → point)
    - recalcul de expected_amount si absent
    - calcul de net_sales_amount = quantity * unit_price * (1 - discount_pct)

Corrections appliquées (v2) :
    [FIX 1] normalize_date : formats supplémentaires + datetime avec heure
    [FIX 2] parse_staging_sql : regex robuste aux parenthèses dans les valeurs
    [FIX 3] build_fact : jointure promise sans la date (seller+customer+product)
    [FIX 4] route_agg / fuel_agg : lignes avec clé None ignorées

Usage :
    python transform_load.py
"""

import re
import sys
from pathlib import Path
from datetime import datetime
from collections import defaultdict

# ── Chemins ───────────────────────────────────────────────────────────────────
BASE_DIR      = Path(__file__).parent
STAGING_SQL   = BASE_DIR / "staging_load.sql"
CLEAN_SQL     = BASE_DIR / "clean_load.sql"
TRANSFORM_SQL = BASE_DIR / "transform_load.sql"

CLEAN_SCHEMA  = "clean"
DW_SCHEMA     = "dw"

# =============================================================================
# UTILITAIRES SQL
# =============================================================================

def esc(val) -> str:
    if val is None:
        return "NULL"
    s = str(val).replace("'", "''")
    return f"'{s}'"


def dict_to_insert(schema: str, table: str, row: dict) -> str:
    cols = ", ".join(f'"{k}"' for k in row.keys())
    vals = []
    for k, v in row.items():
        if k == "is_weekend":
            vals.append("TRUE" if v else "FALSE")
        elif isinstance(v, (int, float)):
            vals.append(str(v))
        else:
            vals.append(esc(v))
    return f'INSERT INTO {schema}."{table}" ({cols}) VALUES ({", ".join(vals)});'


def section_header(lines: list, title: str):
    lines.append("")
    lines.append("-- " + "─" * 60)
    lines.append(f"-- {title}")
    lines.append("-- " + "─" * 60)
    lines.append("")


# =============================================================================
# ÉTAPE 1 — RELECTURE DU STAGING
# =============================================================================

def parse_staging_sql(path: Path) -> dict[str, list[dict]]:
    """Relit staging_load.sql et retourne { table: [{ col: val }] }.

    [FIX 2] La regex originale utilisait (.+?) (non-greedy) dans le groupe
    VALUES, ce qui s'arrêtait à la première ')' rencontrée à l'intérieur
    d'une valeur texte (ex : nom de société "Total (Madagascar) SA").
    On remplace par un parseur ligne-par-ligne qui extrait le groupe VALUES
    en cherchant le ';' de fin de statement, sans risque de confusion avec
    les parenthèses internes aux chaînes.
    """
    if not path.exists():
        sys.exit(f"[ERREUR] Fichier introuvable : {path}\n"
                 "Lancez d'abord load_staging.py")

    text = path.read_text(encoding="utf-8", errors="replace")

    # Regex robuste : on capture la liste de colonnes et TOUT ce qui suit
    # VALUES( jusqu'au ';' de fin de ligne, sans DOTALL pour éviter
    # la confusion entre plusieurs INSERT.  Le groupe values est extrait
    # ensuite via _extract_values_block() qui gère les parenthèses imbriquées
    # dans les chaînes entre guillemets simples.
    insert_re = re.compile(
        r"INSERT INTO \w+\.\"(\w+)\"\s*\(([^)]+)\)\s*VALUES\s*",
        re.IGNORECASE,
    )

    tables: dict[str, list[dict]] = defaultdict(list)
    pos = 0

    while pos < len(text):
        m = insert_re.search(text, pos)
        if not m:
            break

        tname = m.group(1)
        cols  = [c.strip().strip('"') for c in m.group(2).split(",")]

        # Avancer juste après "VALUES "
        after_values = m.end()

        # Extraire le bloc (…) en gérant les guillemets simples
        raw_vals, end_pos = _extract_values_block(text, after_values)
        if raw_vals is None:
            pos = m.end()
            continue

        vals = _split_values(raw_vals)
        row  = {cols[i]: _raw_val(vals[i] if i < len(vals) else None)
                for i in range(len(cols))}
        tables[tname].append(row)
        pos = end_pos

    total = sum(len(v) for v in tables.values())
    print(f"[OK] Staging relu : {total} lignes dans {len(tables)} tables")

    # Avertissement si certaines tables attendues sont vides
    expected = {"hr_vendeurs", "ref_clients", "ref_produits", "sales_orders",
                "stg_route_logs", "stg_sales_promises", "stg_fuel_expenses"}
    missing = expected - set(tables.keys())
    if missing:
        print(f"[WARN] Tables absentes du staging : {', '.join(sorted(missing))}")

    return dict(tables)


def _extract_values_block(text: str, start: int):
    """Trouve le bloc '(…)' débutant à text[start], gère les ' dans les strings.

    Retourne (contenu_sans_parenthèses_externes, position_après_le_';').
    Retourne (None, start) si aucun bloc valide trouvé.
    """
    i = start
    # Sauter les espaces éventuels avant '('
    while i < len(text) and text[i] in (' ', '\t', '\n', '\r'):
        i += 1
    if i >= len(text) or text[i] != '(':
        return None, start

    depth = 0
    in_q  = False
    buf   = []
    i    += 1  # sauter le '(' ouvrant

    while i < len(text):
        ch = text[i]
        if ch == "'" and not in_q:
            in_q = True
            buf.append(ch)
        elif ch == "'" and in_q:
            buf.append(ch)
            # guillemet échappé ''
            if i + 1 < len(text) and text[i + 1] == "'":
                buf.append("'")
                i += 2
                continue
            in_q = False
        elif not in_q and ch == '(':
            depth += 1
            buf.append(ch)
        elif not in_q and ch == ')':
            if depth == 0:
                # Parenthèse fermante du VALUES(…)
                # Chercher le ';' de fin de statement
                end = i + 1
                while end < len(text) and text[end] in (' ', '\t', '\n', '\r'):
                    end += 1
                if end < len(text) and text[end] == ';':
                    end += 1
                return "".join(buf), end
            depth -= 1
            buf.append(ch)
        else:
            buf.append(ch)
        i += 1

    return None, start


def _split_values(s: str) -> list[str]:
    """Découpe la chaîne de valeurs SQL en tenant compte des guillemets simples."""
    vals, cur, in_q = [], "", False
    i = 0
    while i < len(s):
        ch = s[i]
        if ch == "'" and not in_q:
            in_q = True;  cur += ch
        elif ch == "'" and in_q:
            if i + 1 < len(s) and s[i + 1] == "'":
                cur += "''"; i += 2; continue
            in_q = False; cur += ch
        elif ch == "," and not in_q:
            vals.append(cur.strip()); cur = ""
        else:
            cur += ch
        i += 1
    if cur.strip():
        vals.append(cur.strip())
    return vals


def _raw_val(raw):
    if raw is None:
        return None
    raw = raw.strip()
    if raw.upper() == "NULL":
        return None
    if raw.startswith("'") and raw.endswith("'"):
        return raw[1:-1].replace("''", "'")
    return raw


# =============================================================================
# ÉTAPE 2 — FONCTIONS DE NETTOYAGE
# =============================================================================

# [FIX 1] Ajout des formats manquants : %m/%d/%Y, variantes avec point,
#          et formats datetime MySQL (avec heure) souvent présents dans les dumps.
DATE_FORMATS = [
    "%Y-%m-%d",
    "%d/%m/%Y",
    "%Y/%m/%d",
    "%d-%m-%Y",
    "%m/%d/%Y",           # format US
    "%d.%m.%Y",           # format européen avec point
    "%Y.%m.%d",
    "%Y-%m-%d %H:%M:%S",  # datetime MySQL complet
    "%d/%m/%Y %H:%M:%S",
    "%Y/%m/%d %H:%M:%S",
    "%d-%m-%Y %H:%M:%S",
]


def normalize_date(raw) -> str | None:
    """Convertit toute représentation de date connue vers YYYY-MM-DD.

    [FIX 1] Gère les espaces en début/fin, les dates vides, et les formats
    datetime avec heure (ex : '2023-05-12 00:00:00' issu d'un dump MySQL).
    Retourne None si aucun format ne correspond (et affiche un avertissement).
    """
    if not raw:
        return None
    raw = str(raw).strip()
    if not raw:
        return None
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    # Avertissement pour aider au débogage de nouveaux formats
    print(f"[WARN] normalize_date : format non reconnu → '{raw}'")
    return None


def normalize_code(raw) -> str | None:
    if not raw:
        return None
    return str(raw).strip().upper()


def to_decimal(raw) -> float | None:
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    try:
        return float(s.replace(",", "."))
    except ValueError:
        return None


# ── Nettoyage de chaque table (mêmes colonnes, données propres) ───────────────

def clean_hr_vendeurs(rows: list[dict]) -> list[dict]:
    return [{
        "seller_code":  normalize_code(r.get("seller_code")),
        "first_name":   r.get("first_name"),
        "last_name":    r.get("last_name"),
        "email":        r.get("email"),
        "salary":       to_decimal(r.get("salary")),
        "hire_date":    normalize_date(r.get("hire_date")),
        "home_country": r.get("home_country"),
        "home_region":  r.get("home_region"),
        "home_city":    r.get("home_city"),
        "manager_code": normalize_code(r.get("manager_code")),
    } for r in rows]


def clean_ref_clients(rows: list[dict]) -> list[dict]:
    return [{
        "customer_code": normalize_code(r.get("customer_code")),
        "customer_name": r.get("customer_name"),
        "sector":        r.get("sector"),
        "country":       r.get("country"),
        "region_name":   r.get("region_name"),
        "city":          r.get("city"),
        "postal_code":   r.get("postal_code"),
        "created_at":    normalize_date(r.get("created_at")),
    } for r in rows]


def clean_ref_produits(rows: list[dict]) -> list[dict]:
    return [{
        "product_code":  normalize_code(r.get("product_code")),
        "product_name":  r.get("product_name"),
        "category_name": r.get("category_name"),
        "range_name":    r.get("range_name"),
        "list_price":    to_decimal(r.get("list_price")),
        "active_flag":   r.get("active_flag"),
        "launch_date":   normalize_date(r.get("launch_date")),
    } for r in rows]


def clean_sales_orders(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        qty   = to_decimal(r.get("quantity"))
        price = to_decimal(r.get("unit_price"))
        disc  = to_decimal(r.get("discount_pct")) or 0.0
        net   = round(qty * price * (1 - disc), 2) if qty and price else None
        out.append({
            "order_id":               r.get("order_id"),
            "order_date":             normalize_date(r.get("order_date")),
            "seller_code":            normalize_code(r.get("seller_code")),
            "customer_code":          normalize_code(r.get("customer_code")),
            "product_code":           normalize_code(r.get("product_code")),
            "quantity":               qty,
            "unit_price":             price,
            "discount_pct":           disc,
            "order_status":           r.get("order_status"),
            "promised_delivery_date": normalize_date(r.get("promised_delivery_date")),
            "net_sales_amount":       net,
        })
    return out


def clean_stg_route_logs(rows: list[dict]) -> list[dict]:
    return [{
        "route_id":       r.get("route_id"),
        "visit_date":     normalize_date(r.get("visit_date")),
        "seller_code":    normalize_code(r.get("seller_code")),
        "customer_code":  normalize_code(r.get("customer_code")),
        "visit_city":     r.get("visit_city", "").strip().title() if r.get("visit_city") else None,
        "planned_visits": to_decimal(r.get("planned_visits")),
        "actual_visits":  to_decimal(r.get("actual_visits")),
        "km_travelled":   to_decimal(r.get("km_travelled")),
        "travel_expense": to_decimal(r.get("travel_expense")),
        "road_toll":      to_decimal(r.get("road_toll")),
        "trip_status":    r.get("trip_status"),
        "notes":          r.get("notes"),
    } for r in rows]


def clean_stg_sales_promises(rows: list[dict]) -> list[dict]:
    """Nettoyage des promesses de vente.

    Colonnes réelles du staging (issues du fichier Excel) :
        promise_id, promise_date, seller_code, customer_code, product_code,
        promised_qty,          ← pas "quantity"
        expected_amount,
        expected_closing_date,
        probability_pct,
        status,                ← pas "promise_status"
        sales_stage

    Le staging ne contient pas unit_price ni discount_pct pour ce fichier.
    expected_amount est parfois vide (chaîne vide '') → traité comme None.
    """
    out = []
    for r in rows:
        # Quantité : clé "promised_qty" dans le staging Excel
        qty = to_decimal(r.get("promised_qty") or r.get("quantity"))

        # Pas de unit_price ni discount_pct dans cette source
        exp = to_decimal(r.get("expected_amount"))

        # Statut : clé "status" dans le staging
        status = normalize_code(r.get("status") or r.get("promise_status"))

        # Probabilité et étape de vente (colonnes bonus du fichier Excel)
        proba = to_decimal(r.get("probability_pct"))

        out.append({
            "promise_id":            r.get("promise_id"),
            "promise_date":          normalize_date(r.get("promise_date")),
            "seller_code":           normalize_code(r.get("seller_code")),
            "customer_code":         normalize_code(r.get("customer_code")),
            "product_code":          normalize_code(r.get("product_code")),
            "quantity":              qty,             # renommé → quantity pour cohérence DW
            "expected_amount":       exp,
            "expected_closing_date": normalize_date(r.get("expected_closing_date")),
            "probability_pct":       proba,
            "promise_status":        status,          # renommé depuis "status"
            "sales_stage":           r.get("sales_stage"),
        })
    return out


def clean_stg_fuel_expenses(rows: list[dict]) -> list[dict]:
    return [{
        "expense_id":     r.get("expense_id"),
        "seller_code":    normalize_code(r.get("seller_code")),
        "expense_date":   normalize_date(r.get("expense_date")),
        "fuel_liters":    to_decimal(r.get("fuel_liters")),
        "fuel_cost":      to_decimal(r.get("fuel_cost")),
        "toll_cost":      to_decimal(r.get("toll_cost")),
        "hotel_cost":     to_decimal(r.get("hotel_cost")),
        "meal_cost":      to_decimal(r.get("meal_cost")),
        "misc_cost":      to_decimal(r.get("misc_cost")),
        "payment_mode":   r.get("payment_mode"),
        "receipt_status": r.get("receipt_status"),
    } for r in rows]


# =============================================================================
# ÉTAPE 3 — GÉNÉRATION DE clean_load.sql
# =============================================================================

def build_clean_sql(clean: dict[str, list[dict]]) -> str:
    lines = []

    lines.append("-- ============================================================")
    lines.append("-- clean_load.sql")
    lines.append(f"-- Généré le {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("-- Mêmes tables que le staging, données nettoyées & normalisées")
    lines.append("-- Schéma : clean")
    lines.append("-- ============================================================")
    lines.append("")
    lines.append(f"CREATE SCHEMA IF NOT EXISTS {CLEAN_SCHEMA};")
    lines.append("")
    lines.append("BEGIN;")

    for table_name, rows in clean.items():
        section_header(lines, f"{table_name.upper()} ({len(rows)} lignes)")
        if not rows:
            lines.append("-- (aucune donnée)")
            continue
        cols = list(rows[0].keys())
        col_ddl = ",\n".join(f'    "{c}" TEXT' for c in cols)
        lines.append(f'DROP TABLE IF EXISTS {CLEAN_SCHEMA}."{table_name}";')
        lines.append(f'CREATE TABLE IF NOT EXISTS {CLEAN_SCHEMA}."{table_name}" (')
        lines.append(col_ddl)
        lines.append(");")
        lines.append("")
        for row in rows:
            lines.append(dict_to_insert(CLEAN_SCHEMA, table_name, row))
        lines.append(f"-- {len(rows)} ligne(s)")

    lines.append("")
    lines.append("COMMIT;")
    lines.append("")
    lines.append("-- ============================================================")
    lines.append("-- FIN clean_load.sql")
    lines.append("-- ============================================================")
    return "\n".join(lines)


# =============================================================================
# ÉTAPE 4 — CONSTRUCTION DES DIMENSIONS
# =============================================================================

def build_dim_seller(rows: list[dict]) -> list[dict]:
    return [{"seller_id": i, **r} for i, r in enumerate(rows, 1)]


def build_dim_customer(rows: list[dict]) -> list[dict]:
    return [{"customer_id": i, **r} for i, r in enumerate(rows, 1)]


def build_dim_product(rows: list[dict]) -> list[dict]:
    return [{"product_id": i, **r} for i, r in enumerate(rows, 1)]


def build_dim_date(all_dates: list) -> list[dict]:
    unique = sorted({d for d in all_dates if d})
    dim = []
    for d_str in unique:
        try:
            d = datetime.strptime(d_str, "%Y-%m-%d")
        except ValueError:
            continue
        dim.append({
            "date_id":      int(d.strftime("%Y%m%d")),
            "full_date":    d_str,
            "year":         d.year,
            "quarter":      (d.month - 1) // 3 + 1,
            "month":        d.month,
            "month_name":   d.strftime("%B"),
            "week":         int(d.strftime("%W")),
            "day_of_month": d.day,
            "day_of_week":  d.isoweekday(),
            "is_weekend":   d.isoweekday() >= 6,
        })
    return dim


# =============================================================================
# ÉTAPE 5 — CONSTRUCTION DE LA TABLE DE FAITS
# =============================================================================

def build_fact(orders, promises, routes, fuel,
               dim_seller, dim_customer, dim_product, dim_date) -> list[dict]:

    seller_idx   = {r["seller_code"]:   r["seller_id"]   for r in dim_seller}
    customer_idx = {r["customer_code"]: r["customer_id"] for r in dim_customer}
    product_idx  = {r["product_code"]:  r["product_id"]  for r in dim_product}
    date_idx     = {r["full_date"]:     r["date_id"]     for r in dim_date}

    # [FIX 4] Ignorer les lignes dont la clé de jointure contient None.
    # Sans ce filtre, toutes les lignes avec visit_date=None ou seller_code=None
    # s'accumulent sous la clé (None, None) et ne matchent jamais dans la fact.
    route_agg = defaultdict(lambda: {"km_travelled": 0.0, "travel_expense": 0.0, "road_toll": 0.0})
    skipped_routes = 0
    for r in routes:
        if not r["visit_date"] or not r["seller_code"]:
            skipped_routes += 1
            continue
        k = (r["visit_date"], r["seller_code"])
        route_agg[k]["km_travelled"]   += r["km_travelled"]  or 0.0
        route_agg[k]["travel_expense"] += r["travel_expense"] or 0.0
        route_agg[k]["road_toll"]      += r["road_toll"]      or 0.0
    if skipped_routes:
        print(f"[WARN] route_agg : {skipped_routes} ligne(s) ignorée(s) (clé None)")

    fuel_agg = defaultdict(lambda: {"fuel_liters": 0.0, "fuel_cost": 0.0,
                                     "hotel_cost": 0.0, "meal_cost": 0.0, "misc_cost": 0.0})
    skipped_fuel = 0
    for f in fuel:
        if not f["expense_date"] or not f["seller_code"]:
            skipped_fuel += 1
            continue
        k = (f["expense_date"], f["seller_code"])
        fuel_agg[k]["fuel_liters"] += f["fuel_liters"] or 0.0
        fuel_agg[k]["fuel_cost"]   += f["fuel_cost"]   or 0.0
        fuel_agg[k]["hotel_cost"]  += f["hotel_cost"]  or 0.0
        fuel_agg[k]["meal_cost"]   += f["meal_cost"]   or 0.0
        fuel_agg[k]["misc_cost"]   += f["misc_cost"]   or 0.0
    if skipped_fuel:
        print(f"[WARN] fuel_agg  : {skipped_fuel} ligne(s) ignorée(s) (clé None)")

    # [FIX 3] La clé de jointure promise n'inclut plus la date.
    # Dans les données réelles, la promise_date précède souvent l'order_date
    # de plusieurs jours : une jointure sur la date exacte ne matchait jamais.
    # On joint sur (seller_code, customer_code, product_code) uniquement.
    # En cas de doublons (plusieurs promesses pour le même triplet), on garde
    # la plus récente (tri décroissant par promise_date avant indexation).
    promises_sorted = sorted(
        [p for p in promises if p["seller_code"] and p["customer_code"] and p["product_code"]],
        key=lambda p: p["promise_date"] or "",
        reverse=True,
    )
    promise_idx = {}
    for p in promises_sorted:
        k = (p["seller_code"], p["customer_code"], p["product_code"])
        # premier = plus récent → on ne l'écrase pas
        if k not in promise_idx:
            promise_idx[k] = p

    facts = []
    unresolved = {"date": 0, "seller": 0, "customer": 0, "product": 0}

    for fact_id, o in enumerate(orders, 1):
        d, s, c, p = o["order_date"], o["seller_code"], o["customer_code"], o["product_code"]

        date_id     = date_idx.get(d)
        seller_id   = seller_idx.get(s)
        customer_id = customer_idx.get(c)
        product_id  = product_idx.get(p)

        if date_id     is None: unresolved["date"]     += 1
        if seller_id   is None: unresolved["seller"]   += 1
        if customer_id is None: unresolved["customer"] += 1
        if product_id  is None: unresolved["product"]  += 1

        rt = route_agg.get((d, s), {})
        fu = fuel_agg.get((d, s), {})
        # [FIX 3] Jointure sans la date
        pr = promise_idx.get((s, c, p), {})

        total_field = round(
            (fu.get("fuel_cost")  or 0) + (fu.get("hotel_cost") or 0) +
            (fu.get("meal_cost")  or 0) + (fu.get("misc_cost")  or 0) +
            (rt.get("road_toll")  or 0), 2
        ) or None

        facts.append({
            "fact_id":          fact_id,
            "date_id":          date_id,
            "seller_id":        seller_id,
            "customer_id":      customer_id,
            "product_id":       product_id,
            "order_id":         o["order_id"],
            "order_status":     o["order_status"],
            "quantity":         o["quantity"],
            "unit_price":       o["unit_price"],
            "discount_pct":     o["discount_pct"],
            "net_sales_amount": o["net_sales_amount"],
            # promise : colonnes réelles du staging Excel
            "promise_qty":      pr.get("quantity"),          # promised_qty → quantity
            "expected_amount":  pr.get("expected_amount"),
            "promise_status":   pr.get("promise_status"),    # status → promise_status
            "probability_pct":  pr.get("probability_pct"),
            "km_travelled":     rt.get("km_travelled") or None,
            "travel_expense":   rt.get("travel_expense") or None,
            "road_toll":        rt.get("road_toll") or None,
            "fuel_liters":      fu.get("fuel_liters") or None,
            "fuel_cost":        fu.get("fuel_cost") or None,
            "total_field_cost": total_field,
        })

    # Rapport de résolution des FK
    if any(unresolved.values()):
        print(f"[WARN] FK non résolues dans fact_sales_activity :")
        for k, n in unresolved.items():
            if n:
                print(f"         {k}_id NULL : {n} ligne(s)")

    return facts


# =============================================================================
# ÉTAPE 6 — GÉNÉRATION DE transform_load.sql
# =============================================================================

def build_transform_sql(dim_seller, dim_customer, dim_product, dim_date, facts) -> str:
    lines = []

    lines.append("-- ============================================================")
    lines.append("-- transform_load.sql")
    lines.append(f"-- Généré le {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("-- Schéma cible : dw  (datawarehouse en étoile)")
    lines.append("-- ⚠️  À exécuter APRÈS clean_load.sql")
    lines.append("-- ============================================================")
    lines.append("")
    lines.append(f"CREATE SCHEMA IF NOT EXISTS {DW_SCHEMA};")
    lines.append("")
    lines.append("BEGIN;")

    ddls = {
        "dim_date": f"""CREATE TABLE IF NOT EXISTS {DW_SCHEMA}.dim_date (
    date_id      INT PRIMARY KEY,
    full_date    DATE NOT NULL,
    year         SMALLINT,
    quarter      SMALLINT,
    month        SMALLINT,
    month_name   VARCHAR(20),
    week         SMALLINT,
    day_of_month SMALLINT,
    day_of_week  SMALLINT,
    is_weekend   BOOLEAN
);""",
        "dim_seller": f"""CREATE TABLE IF NOT EXISTS {DW_SCHEMA}.dim_seller (
    seller_id    SERIAL PRIMARY KEY,
    seller_code  VARCHAR(10) NOT NULL UNIQUE,
    first_name   VARCHAR(60),
    last_name    VARCHAR(60),
    email        VARCHAR(120),
    salary       NUMERIC(12,2),
    hire_date    DATE,
    home_country VARCHAR(60),
    home_region  VARCHAR(80),
    home_city    VARCHAR(80),
    manager_code VARCHAR(10)
);""",
        "dim_customer": f"""CREATE TABLE IF NOT EXISTS {DW_SCHEMA}.dim_customer (
    customer_id   SERIAL PRIMARY KEY,
    customer_code VARCHAR(10) NOT NULL UNIQUE,
    customer_name VARCHAR(150),
    sector        VARCHAR(50),
    country       VARCHAR(60),
    region_name   VARCHAR(80),
    city          VARCHAR(80),
    postal_code   VARCHAR(20),
    created_at    DATE
);""",
        "dim_product": f"""CREATE TABLE IF NOT EXISTS {DW_SCHEMA}.dim_product (
    product_id    SERIAL PRIMARY KEY,
    product_code  VARCHAR(10) NOT NULL UNIQUE,
    product_name  VARCHAR(120),
    category_name VARCHAR(60),
    range_name    VARCHAR(50),
    list_price    NUMERIC(12,2),
    active_flag   SMALLINT,
    launch_date   DATE
);""",
        "fact_sales_activity": f"""CREATE TABLE IF NOT EXISTS {DW_SCHEMA}.fact_sales_activity (
    fact_id          SERIAL PRIMARY KEY,
    date_id          INT REFERENCES {DW_SCHEMA}.dim_date(date_id),
    seller_id        INT REFERENCES {DW_SCHEMA}.dim_seller(seller_id),
    customer_id      INT REFERENCES {DW_SCHEMA}.dim_customer(customer_id),
    product_id       INT REFERENCES {DW_SCHEMA}.dim_product(product_id),
    order_id         VARCHAR(20),
    order_status     VARCHAR(20),
    quantity         NUMERIC(10,2),
    unit_price       NUMERIC(12,2),
    discount_pct     NUMERIC(5,2),
    net_sales_amount NUMERIC(14,2),
    promise_qty      NUMERIC(10,2),
    expected_amount  NUMERIC(14,2),
    promise_status   VARCHAR(30),
    probability_pct  NUMERIC(5,2),
    km_travelled     NUMERIC(10,2),
    travel_expense   NUMERIC(10,2),
    road_toll        NUMERIC(10,2),
    fuel_liters      NUMERIC(10,2),
    fuel_cost        NUMERIC(10,2),
    total_field_cost NUMERIC(12,2)
);""",
    }

    section_header(lines, "DDL — Dimensions & Table de faits")
    for tname, ddl in ddls.items():
        lines.append(f"DROP TABLE IF EXISTS {DW_SCHEMA}.{tname} CASCADE;")
        lines.append(ddl)

    for label, rows, tname in [
        ("DIM_DATE",            dim_date,     "dim_date"),
        ("DIM_SELLER",          dim_seller,   "dim_seller"),
        ("DIM_CUSTOMER",        dim_customer, "dim_customer"),
        ("DIM_PRODUCT",         dim_product,  "dim_product"),
        ("FACT_SALES_ACTIVITY", facts,        "fact_sales_activity"),
    ]:
        section_header(lines, f"{label} ({len(rows)} lignes)")
        for row in rows:
            lines.append(dict_to_insert(DW_SCHEMA, tname, row))

    lines.append("")
    lines.append("COMMIT;")
    lines.append("")
    lines.append("-- ============================================================")
    lines.append("-- FIN transform_load.sql")
    lines.append("-- ============================================================")
    return "\n".join(lines)


# =============================================================================
# POINT D'ENTRÉE
# =============================================================================

def main():
    print("=" * 60)
    print("  transform_load.py  (v2 — bugs #1-4 corrigés)")
    print("=" * 60)

    # 1. Lire le staging
    print(f"\n[...] Lecture de {STAGING_SQL.name} …")
    staging = parse_staging_sql(STAGING_SQL)

    # 2. Nettoyer chaque table
    print("[...] Nettoyage …")
    clean = {
        "hr_vendeurs":        clean_hr_vendeurs(staging.get("hr_vendeurs", [])),
        "ref_clients":        clean_ref_clients(staging.get("ref_clients", [])),
        "ref_produits":       clean_ref_produits(staging.get("ref_produits", [])),
        "sales_orders":       clean_sales_orders(staging.get("sales_orders", [])),
        "stg_route_logs":     clean_stg_route_logs(staging.get("stg_route_logs", [])),
        "stg_sales_promises": clean_stg_sales_promises(staging.get("stg_sales_promises", [])),
        "stg_fuel_expenses":  clean_stg_fuel_expenses(staging.get("stg_fuel_expenses", [])),
    }
    for t, rows in clean.items():
        print(f"  {t:30s} : {len(rows)} lignes")

    # 3. Générer clean_load.sql
    print(f"\n[...] Génération de {CLEAN_SQL.name} …")
    CLEAN_SQL.write_text(build_clean_sql(clean), encoding="utf-8")
    print(f"[OK]  {CLEAN_SQL.name}  ({CLEAN_SQL.stat().st_size / 1024:.1f} Ko)")

    # 4. Construire les dimensions
    print("\n[...] Construction des dimensions …")
    dim_seller   = build_dim_seller(clean["hr_vendeurs"])
    dim_customer = build_dim_customer(clean["ref_clients"])
    dim_product  = build_dim_product(clean["ref_produits"])

    all_dates = (
        [o["order_date"]   for o in clean["sales_orders"]] +
        [p["promise_date"] for p in clean["stg_sales_promises"]] +
        [r["visit_date"]   for r in clean["stg_route_logs"]] +
        [f["expense_date"] for f in clean["stg_fuel_expenses"]]
    )
    dim_date = build_dim_date(all_dates)

    print(f"  dim_seller   : {len(dim_seller)} lignes")
    print(f"  dim_customer : {len(dim_customer)} lignes")
    print(f"  dim_product  : {len(dim_product)} lignes")
    print(f"  dim_date     : {len(dim_date)} dates distinctes")

    # 5. Construire la table de faits
    print("\n[...] Construction de fact_sales_activity …")
    facts = build_fact(
        clean["sales_orders"], clean["stg_sales_promises"],
        clean["stg_route_logs"], clean["stg_fuel_expenses"],
        dim_seller, dim_customer, dim_product, dim_date
    )
    print(f"  fact_sales_activity : {len(facts)} lignes")

    # 6. Générer transform_load.sql
    print(f"\n[...] Génération de {TRANSFORM_SQL.name} …")
    TRANSFORM_SQL.write_text(
        build_transform_sql(dim_seller, dim_customer, dim_product, dim_date, facts),
        encoding="utf-8"
    )
    print(f"[OK]  {TRANSFORM_SQL.name}  ({TRANSFORM_SQL.stat().st_size / 1024:.1f} Ko)")

    # Résumé
    print("\n" + "─" * 60)
    print("  Fichiers générés :")
    print(f"    1. {CLEAN_SQL.name}  — données nettoyées (schéma : clean)")
    for t, rows in clean.items():
        print(f"       • clean.{t:28s} ({len(rows)} lignes)")
    print(f"\n    2. {TRANSFORM_SQL.name}  — datawarehouse (schéma : dw)")
    print(f"       • dw.dim_date              ({len(dim_date)} lignes)")
    print(f"       • dw.dim_seller            ({len(dim_seller)} lignes)")
    print(f"       • dw.dim_customer          ({len(dim_customer)} lignes)")
    print(f"       • dw.dim_product           ({len(dim_product)} lignes)")
    print(f"       • dw.fact_sales_activity   ({len(facts)} lignes)")
    print("─" * 60)
    print("\n  Ordre d'exécution :")
    print("    psql -d <db> -f staging_load.sql")
    print("    psql -d <db> -f clean_load.sql")
    print("    psql -d <db> -f transform_load.sql\n")


if __name__ == "__main__":
    main()