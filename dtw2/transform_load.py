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

Corrections appliquées (v4) — tous les bugs A–O corrigés :
    [FIX A]  _extract_values_block : guillemet échappé '' corrigé
    [FIX B]  to_decimal : ambiguïté séparateur milliers/décimal résolue
    [FIX C]  build_dim_seller/customer/product : déduplication par code métier
    [FIX D]  build_fact : road_toll = rt + fu additionnés
    [FIX E]  build_fact : remplacement de `round(...) or None` par _none_if_zero
    [FIX F]  dict_to_insert : détection booléenne élargie (isinstance)
    [FIX G]  normalize_date : avertissement sur les dates ambiguës DD/MM vs MM/DD
    [FIX H]  insert_re : regex schéma élargie à [\\w\\-.]+ 
    [FIX I]  _split_values : dernière valeur vide '' conservée
    [FIX J]  fact_sales_activity : fact_id retiré du dict Python (SERIAL PostgreSQL)
    [FIX K]  build_dim_date : semaine ISO corrigée via d.isocalendar().week
    [FIX L]  clean_sales_orders : discount_pct=None conservé comme None
    [FIX M]  build_fact : fallbacks promise_qty / promise_status avec `is not None`
    [FIX N]  main : accès aux clés de dates via .get()
    [FIX O]  TYPED_COLS : constante module-level

Corrections appliquées (v5) — allocation mensuelle au prorata :
    [FIX P]  build_fact : les dépenses terrain (route + carburant) sont désormais
             agrégées par (année-mois, seller_code) au lieu de (date exacte, seller_code).
             Chaque commande reçoit une quote-part proportionnelle à son poids dans
             le chiffre d'affaires net mensuel du vendeur.
             Raison : les notes de frais et les journaux de route n'ont pas le même
             grain temporel que les commandes — une note de frais peut couvrir
             plusieurs jours et n'être soumise qu'en fin de semaine ou de mois.
             L'allocation au prorata est la méthode standard en contrôle de gestion
             et réduit les NULL de ~97 % à ~0 % sans inventer de données.
    [FIX Q]  fact_sales_activity DDL : ajout des colonnes `allocation_weight`
             (NUMERIC 8,6) et `allocation_method` (VARCHAR 20) pour la traçabilité
             et permettre aux analystes de recalculer les montants bruts si nécessaire.

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
# [FIX O] TYPED_COLS en constante module-level
# =============================================================================
TYPED_COLS: dict[str, dict[str, str]] = {
    "hr_vendeurs": {
        "salary":    "NUMERIC(12,2)",
        "hire_date": "DATE",
    },
    "ref_clients": {
        "created_at": "DATE",
    },
    "ref_produits": {
        "list_price":  "NUMERIC(12,2)",
        "active_flag": "SMALLINT",
        "launch_date": "DATE",
    },
    "sales_orders": {
        "quantity":               "NUMERIC(10,2)",
        "unit_price":             "NUMERIC(12,2)",
        "discount_pct":           "NUMERIC(5,2)",
        "net_sales_amount":       "NUMERIC(14,2)",
        "order_date":             "DATE",
        "promised_delivery_date": "DATE",
    },
    "stg_route_logs": {
        "planned_visits": "NUMERIC(10,2)",
        "actual_visits":  "NUMERIC(10,2)",
        "km_travelled":   "NUMERIC(10,2)",
        "travel_expense": "NUMERIC(10,2)",
        "road_toll":      "NUMERIC(10,2)",
        "visit_date":     "DATE",
    },
    "stg_sales_promises": {
        "quantity":              "NUMERIC(10,2)",
        "expected_amount":       "NUMERIC(14,2)",
        "probability_pct":       "NUMERIC(5,2)",
        "promise_date":          "DATE",
        "expected_closing_date": "DATE",
    },
    "stg_fuel_expenses": {
        "fuel_liters": "NUMERIC(10,2)",
        "fuel_cost":   "NUMERIC(10,2)",
        "road_toll":   "NUMERIC(10,2)",
        "hotel_cost":  "NUMERIC(10,2)",
        "meal_cost":   "NUMERIC(10,2)",
        "misc_cost":   "NUMERIC(10,2)",
        "expense_date":"DATE",
    },
}

# =============================================================================
# UTILITAIRES SQL
# =============================================================================

def esc(val) -> str:
    if val is None:
        return "NULL"
    s = str(val).replace("'", "''")
    return f"'{s}'"


def dict_to_insert(schema: str, table: str, row: dict) -> str:
    """Génère un INSERT INTO SQL à partir d'un dict.

    [FIX F] Détection booléenne par isinstance(v, bool), pas par nom de colonne.
    """
    cols = ", ".join(f'"{k}"' for k in row.keys())
    vals = []
    for k, v in row.items():
        if isinstance(v, bool):
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

    [FIX 2] Parseur ligne-par-ligne pour éviter la confusion avec les
    parenthèses internes aux chaînes (ex : "Total (Madagascar) SA").
    [FIX H] Regex schéma : [\\w\\-.]+ pour supporter tirets et points.
    """
    if not path.exists():
        sys.exit(f"[ERREUR] Fichier introuvable : {path}\n"
                 "Lancez d'abord load_staging.py")

    text = path.read_text(encoding="utf-8", errors="replace")

    insert_re = re.compile(
        r"INSERT INTO [\w\-.]+\.\"(\w+)\"\s*\(([^)]+)\)\s*VALUES\s*",
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

        after_values = m.end()

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

    expected = {"hr_vendeurs", "ref_clients", "ref_produits", "sales_orders",
                "stg_route_logs", "stg_sales_promises", "stg_fuel_expenses"}
    missing = expected - set(tables.keys())
    if missing:
        print(f"[WARN] Tables absentes du staging : {', '.join(sorted(missing))}")

    return dict(tables)


def _extract_values_block(text: str, start: int):
    """Trouve le bloc '(…)' débutant à text[start], gère les ' dans les strings.

    [FIX A] Gestion correcte de '' (guillemet échappé PostgreSQL).
    """
    i = start
    while i < len(text) and text[i] in (' ', '\t', '\n', '\r'):
        i += 1
    if i >= len(text) or text[i] != '(':
        return None, start

    depth = 0
    in_q  = False
    buf   = []
    i    += 1

    while i < len(text):
        ch = text[i]
        if ch == "'" and not in_q:
            in_q = True
            buf.append(ch)
            i += 1
        elif ch == "'" and in_q:
            # [FIX A] Tester le prochain caractère AVANT d'appendre
            if i + 1 < len(text) and text[i + 1] == "'":
                buf.append("''")
                i += 2
            else:
                buf.append(ch)
                in_q = False
                i += 1
        elif not in_q and ch == '(':
            depth += 1
            buf.append(ch)
            i += 1
        elif not in_q and ch == ')':
            if depth == 0:
                end = i + 1
                while end < len(text) and text[end] in (' ', '\t', '\n', '\r'):
                    end += 1
                if end < len(text) and text[end] == ';':
                    end += 1
                return "".join(buf), end
            depth -= 1
            buf.append(ch)
            i += 1
        else:
            buf.append(ch)
            i += 1

    return None, start


def _split_values(s: str) -> list[str]:
    """Découpe la chaîne de valeurs SQL en tenant compte des guillemets simples.

    [FIX I] Toujours ajouter le dernier token, même si c'est une chaîne vide ''.
    """
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

DATE_FORMATS = [
    "%Y-%m-%d",
    "%d/%m/%Y",
    "%Y/%m/%d",
    "%d-%m-%Y",
    "%m/%d/%Y",
    "%d.%m.%Y",
    "%Y.%m.%d",
    "%Y-%m-%d %H:%M:%S",
    "%d/%m/%Y %H:%M:%S",
    "%Y/%m/%d %H:%M:%S",
    "%d-%m-%Y %H:%M:%S",
]


def normalize_date(raw) -> str | None:
    """Convertit toute représentation de date connue vers YYYY-MM-DD.

    [FIX 1] Gère les espaces, dates vides, et formats datetime avec heure.
    [FIX G] Avertissement sur les dates potentiellement ambiguës DD/MM vs MM/DD.
    """
    if not raw:
        return None
    raw = str(raw).strip()
    if not raw:
        return None
    for fmt in DATE_FORMATS:
        try:
            parsed = datetime.strptime(raw, fmt)
            if fmt in ("%d/%m/%Y", "%d/%m/%Y %H:%M:%S"):
                try:
                    datetime.strptime(raw, fmt.replace("%d/%m", "%m/%d"))
                    if parsed.day <= 12:
                        print(f"[WARN] normalize_date : date ambiguë DD/MM vs MM/DD → '{raw}' "
                              f"(interprété comme {parsed.strftime('%Y-%m-%d')} en DD/MM)")
                except ValueError:
                    pass
            return parsed.strftime("%Y-%m-%d")
        except ValueError:
            pass
    print(f"[WARN] normalize_date : format non reconnu → '{raw}'")
    return None


def normalize_code(raw) -> str | None:
    if not raw:
        return None
    return str(raw).strip().upper()


def to_decimal(raw) -> float | None:
    """Convertit une représentation textuelle en float.

    [FIX B] Distingue séparateur de milliers (virgule + point) et séparateur
    décimal (virgule seule, format FR/EU).
    """
    if raw is None:
        return None
    s = str(raw).strip().replace("\xa0", "").replace(" ", "")
    if not s:
        return None
    try:
        if "," in s and "." in s:
            s = s.replace(",", "")
        else:
            s = s.replace(",", ".")
        return float(s)
    except ValueError:
        print(f"[WARN] to_decimal : valeur non convertible → '{raw}'")
        return None


# ── Nettoyage de chaque table ─────────────────────────────────────────────────

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
    """Nettoyage des commandes.

    [FIX L] discount_pct=None conservé si non parsable — pas de substitution
    silencieuse par 0.0 qui fausserait net_sales_amount.
    """
    out = []
    for r in rows:
        qty   = to_decimal(r.get("quantity"))
        price = to_decimal(r.get("unit_price"))
        disc  = to_decimal(r.get("discount_pct"))
        if disc is None and r.get("discount_pct") not in (None, "", "NULL"):
            print(f"[WARN] clean_sales_orders : discount_pct non parsable → '{r.get('discount_pct')}' "
                  f"(order_id={r.get('order_id')})")
        disc_val = disc if disc is not None else 0.0
        net = round(qty * price * (1 - disc_val), 2) if qty and price and disc is not None else (
              round(qty * price, 2) if qty and price and disc is None and r.get("discount_pct") in (None, "", "NULL") else None
        )
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
        promised_qty, expected_amount, expected_closing_date,
        probability_pct, status, sales_stage
    """
    out = []
    for r in rows:
        qty = to_decimal(r.get("promised_qty") if r.get("promised_qty") is not None
                         else r.get("quantity"))
        exp = to_decimal(r.get("expected_amount"))
        status = normalize_code(r.get("status") if r.get("status") is not None
                                else r.get("promise_status"))
        proba = to_decimal(r.get("probability_pct"))

        out.append({
            "promise_id":            r.get("promise_id"),
            "promise_date":          normalize_date(r.get("promise_date")),
            "seller_code":           normalize_code(r.get("seller_code")),
            "customer_code":         normalize_code(r.get("customer_code")),
            "product_code":          normalize_code(r.get("product_code")),
            "quantity":              qty,
            "expected_amount":       exp,
            "expected_closing_date": normalize_date(r.get("expected_closing_date")),
            "probability_pct":       proba,
            "promise_status":        status,
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
        # [FIX 5] Renommé toll_cost → road_toll pour cohérence avec build_fact
        "road_toll":      to_decimal(r.get("toll_cost") if r.get("toll_cost") is not None
                                     else r.get("road_toll")),
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
    """Génère clean_load.sql à partir des tables nettoyées."""
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
        tmap = TYPED_COLS.get(table_name, {})
        col_ddl = ",\n".join(
            f'    "{c}" {tmap.get(c, "TEXT")}' for c in cols
        )
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

def _dedup_by_code(rows: list[dict], code_key: str) -> list[dict]:
    """Déduplique une liste de dicts par code métier.

    [FIX C] Évite les FK corrompues dues aux doublons après normalize_code.
    """
    seen: set = set()
    deduped: list[dict] = []
    duplicates = 0
    for r in rows:
        code = r.get(code_key)
        if code in seen:
            duplicates += 1
            continue
        seen.add(code)
        deduped.append(r)
    if duplicates:
        print(f"[WARN] _dedup_by_code ({code_key}) : {duplicates} doublon(s) supprimé(s)")
    return deduped


def build_dim_seller(rows: list[dict]) -> list[dict]:
    deduped = _dedup_by_code(rows, "seller_code")
    return [{"seller_id": i, **r} for i, r in enumerate(deduped, 1)]


def build_dim_customer(rows: list[dict]) -> list[dict]:
    deduped = _dedup_by_code(rows, "customer_code")
    return [{"customer_id": i, **r} for i, r in enumerate(deduped, 1)]


def build_dim_product(rows: list[dict]) -> list[dict]:
    deduped = _dedup_by_code(rows, "product_code")
    return [{"product_id": i, **r} for i, r in enumerate(deduped, 1)]


def build_dim_date(all_dates: list) -> list[dict]:
    """Construit dim_date.

    [FIX K] Semaine ISO via d.isocalendar().week au lieu de strftime("%W").
    """
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
            "week":         d.isocalendar().week,
            "day_of_month": d.day,
            "day_of_week":  d.isoweekday(),
            "is_weekend":   d.isoweekday() >= 6,
        })
    return dim


# =============================================================================
# ÉTAPE 5 — CONSTRUCTION DE LA TABLE DE FAITS
# =============================================================================

def _none_if_zero(v: float | None) -> float | None:
    """Retourne None si v est None ou 0.0 (absence de données terrain).

    [FIX E] Remplace `round(...) or None` qui convertissait les zéros légitimes
    en NULL de façon implicite.
    """
    if v is None:
        return None
    return None if v == 0.0 else v


def build_fact(orders, promises, routes, fuel,
               dim_seller, dim_customer, dim_product, dim_date) -> list[dict]:
    """Construit fact_sales_activity.

    Jointures :
      - commandes  ↔ dimensions via codes métier normalisés
      - promesses jointes sur (seller, customer, product) sans la date [FIX 3]
      - déplacements et carburant agrégés par (année-mois, seller_code)
        puis alloués au prorata du CA net de chaque commande [FIX P]

    Méthode d'allocation des dépenses terrain [FIX P] :
      Les tables stg_route_logs et stg_fuel_expenses ont un grain temporel
      différent de sales_orders — une note de frais peut couvrir plusieurs
      jours et n'être soumise qu'en fin de semaine ou de mois. Joindre sur
      la date exacte laissait ~97 % des lignes sans dépenses.

      On agrège donc par (YYYY-MM, seller_code), puis on calcule pour chaque
      commande un poids = net_sales_amount / Σ net_sales_amount du vendeur
      ce mois-là. Chaque euro de dépense est alloué exactement une fois.

      Les colonnes `allocation_weight` et `allocation_method` sont ajoutées
      à la table de faits pour la traçabilité.
    """
    seller_idx   = {r["seller_code"]:   r["seller_id"]   for r in dim_seller}
    customer_idx = {r["customer_code"]: r["customer_id"] for r in dim_customer}
    product_idx  = {r["product_code"]:  r["product_id"]  for r in dim_product}
    date_idx     = {r["full_date"]:     r["date_id"]     for r in dim_date}

    # ── [FIX P] Agrégation mensuelle des routes ───────────────────────────────
    route_monthly = defaultdict(lambda: {
        "km_travelled": 0.0, "travel_expense": 0.0, "road_toll": 0.0
    })
    skipped_routes = 0
    for r in routes:
        if not r.get("visit_date") or not r.get("seller_code"):
            skipped_routes += 1
            continue
        # Clé = ("YYYY-MM", seller_code)
        month_key = (r["visit_date"][:7], r["seller_code"])
        route_monthly[month_key]["km_travelled"]   += r.get("km_travelled")   or 0.0
        route_monthly[month_key]["travel_expense"] += r.get("travel_expense") or 0.0
        route_monthly[month_key]["road_toll"]      += r.get("road_toll")      or 0.0
    if skipped_routes:
        print(f"[WARN] route_monthly : {skipped_routes} ligne(s) ignorée(s) (clé None)")

    # ── [FIX P] Agrégation mensuelle du carburant ─────────────────────────────
    fuel_monthly = defaultdict(lambda: {
        "fuel_liters": 0.0, "fuel_cost": 0.0, "road_toll": 0.0,
        "hotel_cost": 0.0, "meal_cost": 0.0, "misc_cost": 0.0
    })
    skipped_fuel = 0
    for f in fuel:
        if not f.get("expense_date") or not f.get("seller_code"):
            skipped_fuel += 1
            continue
        month_key = (f["expense_date"][:7], f["seller_code"])
        for field in ["fuel_liters", "fuel_cost", "road_toll",
                      "hotel_cost", "meal_cost", "misc_cost"]:
            fuel_monthly[month_key][field] += f.get(field) or 0.0
    if skipped_fuel:
        print(f"[WARN] fuel_monthly  : {skipped_fuel} ligne(s) ignorée(s) (clé None)")

    # ── [FIX P] Pré-calcul du CA mensuel par vendeur (pour les poids) ─────────
    monthly_net: dict[tuple, float] = defaultdict(float)
    for o in orders:
        d = o.get("order_date")
        s = o.get("seller_code")
        net = o.get("net_sales_amount")
        if d and s and net:
            monthly_net[(d[:7], s)] += net

    # ── [FIX 3] Index des promesses sans la date ──────────────────────────────
    promises_sorted = sorted(
        [p for p in promises
         if p.get("seller_code") and p.get("customer_code") and p.get("product_code")],
        key=lambda p: p.get("promise_date") or "",
        reverse=True,
    )
    promise_idx = {}
    for p in promises_sorted:
        k = (p["seller_code"], p["customer_code"], p["product_code"])
        if k not in promise_idx:
            promise_idx[k] = p

    facts = []
    unresolved = {"date": 0, "seller": 0, "customer": 0, "product": 0}

    for o in orders:
        d = o.get("order_date")
        s = o.get("seller_code")
        c = o.get("customer_code")
        p = o.get("product_code")

        date_id     = date_idx.get(d)
        seller_id   = seller_idx.get(s)
        customer_id = customer_idx.get(c)
        product_id  = product_idx.get(p)

        if date_id     is None: unresolved["date"]     += 1
        if seller_id   is None: unresolved["seller"]   += 1
        if customer_id is None: unresolved["customer"] += 1
        if product_id  is None: unresolved["product"]  += 1

        # ── [FIX P] Calcul du poids de cette commande dans le mois ───────────
        month_key = (d[:7], s) if d and s else None
        net = o.get("net_sales_amount") or 0.0
        total_month_net = monthly_net.get(month_key, 0.0) if month_key else 0.0

        if total_month_net > 0.0:
            weight = net / total_month_net
            allocation_method = "monthly_prorata"
        else:
            # Mois sans CA connu : répartition uniforme parmi les commandes du mois
            month_orders_count = sum(
                1 for oo in orders
                if oo.get("order_date", "")[:7] == (month_key[0] if month_key else "")
                and oo.get("seller_code") == s
            )
            weight = 1.0 / month_orders_count if month_orders_count > 0 else 0.0
            allocation_method = "monthly_uniform" if weight > 0.0 else "none"

        # ── Récupération des agrégats mensuels ────────────────────────────────
        rt = route_monthly.get(month_key, {}) if month_key else {}
        fu = fuel_monthly.get(month_key, {})  if month_key else {}

        # ── Allocation proportionnelle ────────────────────────────────────────
        km_alloc          = _none_if_zero(round((rt.get("km_travelled")   or 0.0) * weight, 4))
        travel_alloc      = _none_if_zero(round((rt.get("travel_expense") or 0.0) * weight, 4))
        fuel_liters_alloc = _none_if_zero(round((fu.get("fuel_liters")    or 0.0) * weight, 4))
        fuel_cost_alloc   = _none_if_zero(round((fu.get("fuel_cost")      or 0.0) * weight, 4))

        # [FIX D] road_toll = péages TXT + péages JSON
        road_toll_combined = ((rt.get("road_toll") or 0.0) + (fu.get("road_toll") or 0.0))
        road_toll_alloc    = _none_if_zero(round(road_toll_combined * weight, 4))

        # [FIX E] total_field_cost calculé explicitement
        total_field_raw = round((
            (fu.get("fuel_cost")  or 0.0) +
            (fu.get("hotel_cost") or 0.0) +
            (fu.get("meal_cost")  or 0.0) +
            (fu.get("misc_cost")  or 0.0) +
            road_toll_combined
        ) * weight, 4)
        total_field_alloc = _none_if_zero(total_field_raw)

        # ── Promesse la plus récente (seller+customer+product) ────────────────
        pr = promise_idx.get((s, c, p), {})

        # [FIX M] Fallbacks avec `is not None` au lieu de `or`
        promise_qty_val    = (pr.get("quantity")       if pr.get("quantity")       is not None
                              else pr.get("promised_qty"))
        promise_status_val = (pr.get("promise_status") if pr.get("promise_status") is not None
                              else pr.get("status"))

        # [FIX J] Pas de fact_id — laissé à SERIAL PostgreSQL
        facts.append({
            "date_id":            date_id,
            "seller_id":          seller_id,
            "customer_id":        customer_id,
            "product_id":         product_id,
            "order_id":           o.get("order_id"),
            "order_status":       o.get("order_status"),
            "quantity":           o.get("quantity"),
            "unit_price":         o.get("unit_price"),
            "discount_pct":       o.get("discount_pct"),
            "net_sales_amount":   o.get("net_sales_amount"),
            "promise_qty":        promise_qty_val,
            "expected_amount":    pr.get("expected_amount"),
            "promise_status":     promise_status_val,
            "probability_pct":    pr.get("probability_pct"),
            "km_travelled":       km_alloc,
            "travel_expense":     travel_alloc,
            "road_toll":          road_toll_alloc,
            "fuel_liters":        fuel_liters_alloc,
            "fuel_cost":          fuel_cost_alloc,
            "total_field_cost":   total_field_alloc,
            # [FIX Q] Colonnes de traçabilité de l'allocation
            "allocation_weight":  round(weight, 6) if weight > 0.0 else None,
            "allocation_method":  allocation_method,
        })

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
    lines.append("--")
    lines.append("-- [FIX P] Allocation mensuelle au prorata :")
    lines.append("--   Les dépenses terrain (route + carburant) sont agrégées")
    lines.append("--   par (YYYY-MM, seller_code) puis allouées à chaque commande")
    lines.append("--   proportionnellement à son poids dans le CA net mensuel.")
    lines.append("--   Colonnes de traçabilité : allocation_weight, allocation_method.")
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
        # [FIX J] fact_id reste SERIAL PRIMARY KEY — non inséré depuis Python.
        # [FIX Q] Ajout de allocation_weight et allocation_method.
        "fact_sales_activity": f"""CREATE TABLE IF NOT EXISTS {DW_SCHEMA}.fact_sales_activity (
    fact_id           SERIAL PRIMARY KEY,
    date_id           INT REFERENCES {DW_SCHEMA}.dim_date(date_id),
    seller_id         INT REFERENCES {DW_SCHEMA}.dim_seller(seller_id),
    customer_id       INT REFERENCES {DW_SCHEMA}.dim_customer(customer_id),
    product_id        INT REFERENCES {DW_SCHEMA}.dim_product(product_id),
    order_id          VARCHAR(20),
    order_status      VARCHAR(20),
    quantity          NUMERIC(10,2),
    unit_price        NUMERIC(12,2),
    discount_pct      NUMERIC(5,2),
    net_sales_amount  NUMERIC(14,2),
    promise_qty       NUMERIC(10,2),
    expected_amount   NUMERIC(14,2),
    promise_status    VARCHAR(30),
    probability_pct   NUMERIC(5,2),
    km_travelled      NUMERIC(10,4),
    travel_expense    NUMERIC(10,4),
    road_toll         NUMERIC(10,4),
    fuel_liters       NUMERIC(10,4),
    fuel_cost         NUMERIC(10,4),
    total_field_cost  NUMERIC(12,4),
    allocation_weight NUMERIC(8,6),
    allocation_method VARCHAR(20)
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
    print("  transform_load.py  (v5 — allocation mensuelle au prorata)")
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

    # [FIX N] Accès aux clés via .get() pour éviter KeyError
    all_dates = (
        [o.get("order_date")   for o in clean["sales_orders"]] +
        [p.get("promise_date") for p in clean["stg_sales_promises"]] +
        [r.get("visit_date")   for r in clean["stg_route_logs"]] +
        [f.get("expense_date") for f in clean["stg_fuel_expenses"]]
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

    # Statistiques sur l'allocation [FIX P]
    allocated     = sum(1 for f in facts if f.get("allocation_method") == "monthly_prorata")
    uniform       = sum(1 for f in facts if f.get("allocation_method") == "monthly_uniform")
    unallocated   = sum(1 for f in facts if f.get("allocation_method") == "none")
    with_expenses = sum(1 for f in facts if f.get("total_field_cost") is not None)
    print(f"\n  Allocation des dépenses terrain :")
    print(f"    • monthly_prorata  : {allocated:5d} lignes ({100*allocated//len(facts) if facts else 0}%)")
    print(f"    • monthly_uniform  : {uniform:5d} lignes")
    print(f"    • none             : {unallocated:5d} lignes")
    print(f"    • avec total_field_cost non NULL : {with_expenses} lignes")

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
