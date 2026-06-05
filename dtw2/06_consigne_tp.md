# Consigne TP - Petit ETL vers PostgreSQL

## Objectif
Construire un petit datawarehouse en étoile à partir de 4 sources hétérogènes :
1. Base MySQL
2. Fichier texte
3. Fichier Excel
4. Fichier JSON

## Travail demandé
1. Charger les 4 sources dans des tables de staging PostgreSQL
2. Nettoyer les données
3. Normaliser les formats
4. Créer les dimensions `date`, `seller`, `customer`, `product`
5. Alimenter la table de faits `fact_sales_activity`

## Règles de transformation minimales
- standardiser les identifiants métier : `upper(trim(code))`
- convertir les nombres avec virgule en décimaux
- harmoniser les formats de date
- recalculer `expected_amount` si la valeur est absente
- calculer `net_sales_amount`
- agréger les dépenses carburant / route par vendeur et par date
- conserver uniquement des clés de substitution dans les dimensions

## Questions d'analyse possibles
- chiffre d'affaires net par vendeur et par mois
- kilomètres parcourus par région
- vendeurs les moins efficaces (beaucoup de km, peu de CA)
- clients les plus coûteux à servir
- taux de transformation promesses de vente -> ventes
