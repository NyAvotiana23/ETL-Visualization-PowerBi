# TP ETL - Datawarehouse (activité commerciale)

## Fichiers livrés
- `sources/01_source_mysql_printer_sales.sql` : dump MySQL avec 4 tables (`hr_vendeurs`, `ref_clients`, `ref_produits`, `sales_orders`)
- `sources/02_route_logs.txt` : fichier texte séparé par `|`
- `sources/03_sales_promises.xlsx` : fichier Excel (promesses de vente)
- `sources/04_fuel_expenses.json` : fichier JSON (carburant et frais terrain)
- `target_postgres/05_postgres_schema.sql` : schéma cible PostgreSQL en étoile
- `target_postgres/06_consigne_tp.md` : énoncé du TP
- `target_postgres/07_etl_solution.py` : exemple de correction ETL
- `target_postgres/requirements.txt`

## Volumétrie
- MySQL `sales_orders` : 1100 lignes
- Texte `route_logs.txt` : 1000 lignes
- Excel `promesses_vente` : 780 lignes
- JSON `fuel_expenses.json` : 900 objets

## Idée métier
Entreprise de vente d'imprimantes. On veut analyser :
- les ventes
- les promesses de vente
- les kilomètres parcourus
- les litres de carburant
- les frais de déplacement
- l'efficacité commerciale par vendeur / client / produit / date / zone géographique

## Qualité volontairement imparfaite des sources
Pour rendre le TP réaliste, certains problèmes ont été introduits :
- formats de dates hétérogènes
- codes vendeurs / clients / produits en minuscules ou avec espaces
- nombres avec virgule décimale dans le TXT/JSON
- montants attendus parfois absents dans le fichier Excel
- statuts non homogènes entre les sources

## Grain conseillé de la table de faits
Une ligne par `jour + vendeur + client + produit`.

## Exemple de transformations attendues
1. Normaliser les codes (`trim`, `upper`)
2. Convertir toutes les dates au format `DATE`
3. Convertir les décimaux texte en `NUMERIC`
4. Recalculer `expected_amount` si vide
5. Calculer `net_sales_amount = quantity * unit_price * (1 - discount_pct)`
6. Agréger les déplacements et le carburant par vendeur et par jour
7. Charger des dimensions avec clés de substitution
8. Alimenter une table de faits en étoile dans PostgreSQL
