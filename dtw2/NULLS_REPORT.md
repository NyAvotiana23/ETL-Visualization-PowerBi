# Rapport simple sur les NULL dans fact_sales_activity

## Constat rapide (sur vos requetes)
- 1100 lignes dans `dw.fact_sales_activity`
- 1098 lignes ont `promise_qty` NULL
- 1065 lignes ont `km_travelled` NULL
- 1077 lignes ont `fuel_cost` NULL
- Aucune FK NULL (date_id, seller_id, customer_id, product_id)

## Pourquoi il y a beaucoup de NULL
1) Promesses presque jamais alignees avec les commandes
- La jointure se fait sur (seller_code, customer_code, product_code).
- L intersection des triplets est de 2 seulement.
- Donc presque toutes les lignes n ont pas de promesse associee.

2) Route / fuel rarement sur la meme date que la commande
- La jointure se fait sur (order_date, seller_code).
- Peu de dates communess entre ventes et route/fuel.
- Meme avec +/- 1 jour, le recouvrement reste faible.

3) Les zeros sont convertis en NULL
- La logique `_none_if_zero` transforme 0.0 en NULL.
- Donc meme si un vendeur n a pas bouge, on stocke NULL.

4) Donnees presentes mais non matchables
- Les tables `clean.stg_sales_promises`, `clean.stg_route_logs`, `clean.stg_fuel_expenses` sont remplies.
- Le probleme vient surtout du manque de correspondance des cles, pas d une table vide.

## Suggestions simples pour reduire les NULL
Option A (promesses) : jointure moins stricte
- Joindre seulement sur (seller_code, customer_code)
- Ou seulement sur (seller_code, product_code)
- Cela augmente le taux de correspondance, mais melange les promesses.

Option B (route/fuel) : jointure plus souple sur la date
- Joindre sur (seller_code, date) avec une tolerance de +/- 1 jour
- Ou joindre seulement sur seller_code

Option C (garder la precision)
- Conserver la jointure actuelle
- Les NULL restent mais la precision est maximale

## Conseils pratiques
- Verifier si les codes vendeur/client/produit sont homogenes entre sources.
- Verifier si les dates de promesse/route/fuel doivent vraiment etre les dates de commande.
- Choisir un compromis : precision vs reduction des NULL.

