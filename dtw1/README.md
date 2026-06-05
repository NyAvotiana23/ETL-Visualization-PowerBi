# TP — Modélisation & ETL DataWarehouse
## Entreprise de vente d'imprimantes — Analyse des représentants commerciaux

---

## 🎯 Objectif du TP

Construire un **DataMart en schéma étoile** pour analyser l'activité des représentants commerciaux d'une entreprise de vente d'imprimantes, en réalisant un pipeline ETL complet depuis 4 sources hétérogènes vers PostgreSQL.

---

## 📁 Structure des fichiers

```
tp_dwh/
├── sources/
│   ├── 01_mysql_ventes_rh.sql      ← Source MySQL  (vendeurs + 600 ventes)
│   ├── 02_clients.txt              ← Source TXT    (150 clients, séparateur |)
│   ├── 03_produits.xlsx            ← Source Excel  (15 produits)
│   ├── 04_feuilles_route.json      ← Source JSON   (600 feuilles de route)
│   └── 05_load_postgres.sql        ← Script de chargement PostgreSQL (généré par ETL)
└── etl/
    └── etl_pipeline.py             ← Script ETL Python complet
```

---

## 🗄️ Sources de données

### 1. MySQL — `01_mysql_ventes_rh.sql`
Système RH + Gestion des ventes.


### 2. Fichier TXT — `02_clients.txt`
Export CRM

**150 clients** avec : nom, prénom, adresse, ville, province, pays, personne ressource, téléphone, email, segment (PME / Grand Compte / Administration...).

> ⚠️ **Transformation nécessaire** : parsing du séparateur, nettoyage des espaces, normalisation des majuscules....

---

### 3. Excel — `03_produits.xlsx`
Catalogue produits — 2 feuilles :
- **Produits** : 15 imprimantes avec catégorie, groupe, prix, stock, fournisseur
- **Stats** : formules de synthèse (COUNTA, AVERAGE, MAX, MIN,...)

> ⚠️ **Transformation nécessaire** : conversion des types (prix → float, stock → int), gestion des valeurs None

---

### 4. JSON — `04_feuilles_route.json`
Feuilles de route des représentants — structure imbriquée.

```json
{
  "id_feuille": 1,
  "id_vendeur": 5,
  "date_deplacement": "2023-04-12",
  "nb_clients_visites": 3,
  "clients_visites": [12, 45, 78],
  "villes_etapes": ["Lyon", "Grenoble"],
  "km_parcourus": 320,
  "litres_essence": 28.5,
  "frais": {
    "carburant": 54.15,
    "peage": 12.50,
    "repas": 22.00,
    "hotel": 0,
    "total": 88.65
  },
  "objectif_visite_atteint": true,
  "commentaire": "Devis remis"
}
```

> ⚠️ **Transformation nécessaire** : aplatissement du sous-objet , déduplication 


## 🔄 Pipeline ETL — `etl_pipeline.py`

### Étape E — Extraction
| Source | Méthode | Données extraites |
|--------|---------|-------------------|

### Étape T — Transformations appliquées
- **DIM_TEMPS** 
- **DIM_VENDEUR** 
- **DIM_CLIENT**
- **DIM_PRODUIT**
- **DIM_GEO**
- **FAIT** : résolution des clés surrogates, calcul `marge_estimee` (CA − 60% du prix × qté), calcul `rentabilite_nette` (marge − frais)

### Étape L — Chargement
- Génération d'un script `05_load_postgres.sql`
- Clés surrogates propres (SERIAL)
- 0 rejet (toutes les clés résolues)

---

## 🚀 Utilisation

### Prérequis
```bash
pip install openpyxl psycopg2-binary
```

### Lancer l'ETL
```bash
cd tp_dwh/etl
python3 etl_pipeline.py
```

### Charger dans PostgreSQL
```bash
psql -U postgres -d dwh_imprimantes -f ../sources/05_load_postgres.sql
```

### Ou en une commande
```bash
createdb dwh_imprimantes
psql -U postgres -d dwh_imprimantes -f sources/05_load_postgres.sql
```

---

## 📊 Requêtes analytiques incluses

Le script de chargement inclut 5 requêtes de validation métier :

1. **Performance vendeurs** — CA total, CA moyen, km parcourus
2. **Ventes par produit/catégorie** — quantités, CA, marge
3. **Évolution mensuelle** — CA, frais, rentabilité par mois/année
4. **Couverture géographique** — nb visites et km par vendeur et province
5. **Rentabilité par segment client** — PME vs Grand Compte vs Administration

---

## ✅ Points de contrôle DWH (cours rappel)

- ✅ Clés surrogates distinctes des clés opérationnelles MySQL
- ✅ DIM_TEMPS toujours présente (axe d'analyse universel)
- ✅ Granularité faits = granularité dimensions (jour)
- ✅ Chaque ligne de fait reliée à TOUTES les dimensions
- ✅ Pas de relations entre dimensions (schéma étoile pur)
- ✅ Mesures numériques dans la table de faits uniquement
- ✅ Approche Bottom-Up : un DataMart à la fois
