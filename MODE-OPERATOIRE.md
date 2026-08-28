# Correctif — push bloqué par la limite de 100 Mo de GitHub

## Ce qui s'est passé

`data/afrr_marginal_price.csv` est la série aFRR marginal price au **pas de 4 secondes** :
21 600 lignes / **1,7 Mo par jour**. Le script l'ajoutait dans un fichier unique qui
grossissait sans limite. Il a franchi les **100 Mo** (limite dure de GitHub, non
contournable sans Git LFS) début août.

Depuis, `git push` est rejeté à chaque exécution. Conséquence importante :
**plus aucune donnée n'a été enregistrée depuis le 1er août, sur les 8 séries**,
pas seulement l'aFRR — le commit contient tous les fichiers, donc tout est rejeté
en bloc. Il y a un trou d'environ 4 semaines à rattraper.

Rien n'est perdu côté GitHub : le push ayant été refusé, l'historique distant est
resté propre (aucun blob > 100 Mo n'y a été poussé). Pas besoin de réécrire l'historique.

## La correction

La série aFRR est désormais découpée en **partitions journalières compressées** :

```
data/afrr_marginal_price/2026-08-01.csv.gz     ← ~0,39 Mo par jour
```

Résultat sur les données réelles : **99,7 Mo → 62 partitions, 22,4 Mo au total**, la
plus grosse à 0,40 Mo, **1 317 600 lignes conservées, zéro perte, zéro doublon**
(vérifié horodatage par horodatage).

Trois bénéfices :
- chaque fichier est écrit une fois puis ne bouge plus → le dépôt grossit de
  ~0,4 Mo/jour (~140 Mo/an) au lieu de réécrire un fichier de plus en plus lourd ;
- la déduplication ne relit plus 1,3 million de lignes à chaque exécution,
  seulement la journée concernée ;
- le problème ne peut plus se reproduire : la taille d'un fichier est bornée par
  construction.

## Fichiers modifiés

| Fichier | Changement |
|---|---|
| `fetch_data.py` | Écriture aFRR en partitions journalières `.csv.gz` ; profondeur de collecte pilotable par `DAYS_BACK` ; découpage automatique des requêtes en fenêtres de 7 jours max (les APIs RTE refusent les plages larges) |
| `.github/workflows/daily_fetch.yml` | Entrée `days_back` pour un lancement manuel ; garde-fou qui échoue **avant** le push si un fichier dépasse 90 Mo |
| `.gitignore` | Ignore l'ancien fichier monolithique pour qu'il ne réapparaisse pas |
| `.gitattributes` | **Nouveau** — marque les `.csv.gz` comme binaires (`core.autocrlf=true` sur le poste local corromprait sinon les archives) |
| `split_afrr.py` | **Nouveau** — migration ponctuelle du fichier existant vers les partitions |
| `load_afrr.py` | **Nouveau** — helper pandas pour relire la série partitionnée |

## État actuel

Déjà fait dans ce clone :

1. ✅ `git pull` — le clone était resté au 15 juillet, il est maintenant aligné sur le distant
2. ✅ fichiers du correctif copiés
3. ✅ `python split_afrr.py` — 62 partitions écrites
4. ✅ ancien fichier supprimé (`git rm --cached` + `rm`) et tout est **committé localement**

## Ce qu'il reste à faire

### 1. Pousser

```bash
git push
```

Ce push doit passer : le plus gros fichier du dépôt fait désormais 7,2 Mo
(`data/imbalance_data.csv`).

### 2. Rattraper le trou du 1er au 28 août

Sur GitHub → onglet **Actions** → workflow *Collecte quotidienne données France (RTE)*
→ **Run workflow** → renseigner `days_back` = **30** → lancer.

Ne lance pas ça à 10 h heure de Paris (08:00 UTC), pour ne pas entrer en collision
avec l'exécution automatique.

Le run est plus long que d'habitude (l'API aFRR ne répond que jour par jour, donc
~30 appels pour cette série seule). Les autres séries sont découpées en tranches
de 7 jours.

**À vérifier après coup** : RTE ne garantit pas la même profondeur d'historique sur
toutes les APIs. Si `afrr_marginal_price` ne remonte pas jusqu'au 1er août, il
manquera les premiers jours — le log du run le dit, partition par partition.

### 3. Récupérer le résultat en local

```bash
git pull
```

## Relire la série ensuite

```python
from load_afrr import load_afrr

df = load_afrr("2026-08-01", "2026-08-27")   # une plage de dates
df = load_afrr()                              # tout l'historique
```

Le helper décompresse, concatène, convertit les horodatages en `datetime` Europe/Paris
et transforme les `"Invalid"` renvoyés par RTE en `NaN` (ils sont fréquents : sur la
semaine testée, seules ~46 000 des 108 000 lignes ont un prix à la hausse).

Il normalise aussi `prorata_mode` / `picasso_connection`, dont **RTE a changé la casse
en cours de route** (`"False"` jusqu'à mi-2026, puis `"false"`) — à surveiller si tu
as des filtres sur ces colonnes ailleurs dans tes analyses.

## Point de vigilance à moyen terme

L'historique Git contient déjà les versions successives du fichier monolithique
(jusqu'à ~99 Mo). Le dépôt reste fonctionnel, mais un `git clone` est lourd.
Si ça devient gênant, on peut purger ces blobs avec `git filter-repo` — opération
à faire à froid, elle réécrit l'historique et invalide les clones existants.
Rien d'urgent.
