# Accessibilité des plages du Grand Montréal

Cette petite application compare, depuis un point de départ choisi :

- le temps et la distance à vélo;
- le temps en transport collectif;
- le nombre de correspondances et la marche associée;
- les cinq plages sur une même carte;
- les itinéraires à vélo, en transport collectif et en combinaison vélo + TC;
- les étapes détaillées au clic sur une plage ou un trajet;
- une vérification indicative des règles permettant un vélo standard dans le TC.

Les cinq destinations sont celles du [palmarès de la
CMM](https://cmm.qc.ca/nouvelles/top-5-des-plus-belles-plages-du-grand-montreal/).

## Installation

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Pour l'utilisation via l'API ouverte MOTIS 2 de Transitous
L’application utilise l’API ouverte MOTIS 2 fournie par
[Transitous](https://transitous.org/api/) et une carte Leaflet/OpenStreetMap.
Aucune clé Google n’est nécessaire.

Transitous exige que chaque requête contienne le nom et la version de
l’application ainsi qu’un moyen de contact. Définissez donc une adresse
courriel ou, de préférence, l’URL publique de l’application :

```bash
export TRANSITOUS_CONTACT="https://exemple.org/contact"
streamlit run accessibilite_plages_montreal.py
```

## Conditions d’utilisation de Transitous

Avant une mise en ligne publique :

- publiez ce code sous une licence libre; une licence MIT est incluse;
- conservez le lien visible vers les
  [sources Transitous](https://transitous.org/sources/) et l’attribution
  OpenStreetMap déjà affichés dans l’application;
- fournissez une vraie valeur `TRANSITOUS_CONTACT`;
- contactez l’équipe Transitous dans son
  [salon Matrix](https://matrix.to/#/#transitous:matrix.org) avant d’ouvrir
  l’application au public, car le calcul d’itinéraires est une opération
  coûteuse et le service est communautaire, sans garantie de disponibilité;
- réservez le serveur public de tuiles OpenStreetMap à un prototype peu
  fréquenté et respectez sa [politique d’utilisation](https://operations.osmfoundation.org/policies/tiles/);
  pour davantage de trafic, utilisez un fournisseur compatible ou vos propres
  tuiles;
- si l’application devient commerciale ou très fréquentée, demandez leur
  accord ou hébergez votre propre instance MOTIS.

Le code limite une comparaison à dix requêtes de routage, fixe un délai maximal
de calcul, ne demande aucune alternative de trajet et met les résultats en
cache pendant 30 minutes. Le formulaire évite aussi de recalculer les trajets
quand un filtre d’affichage est modifié.

## Recalibrage MOTIS appliqué

Pour éviter des résultats artificiellement optimistes, l’application :

- utilise 4,5 km/h pour la marche et environ 15 km/h pour le vélo;
- réserve deux minutes supplémentaires à chaque correspondance;
- augmente de 15 % le temps des cheminements de correspondance;
- demande à MOTIS de calculer les transferts sur le réseau OpenStreetMap;
- inclut l’attente entre l’heure demandée et le premier départ;
- choisit le trajet arrivant réellement le plus tôt, et non celui ayant
  seulement la plus courte durée après son départ.

### Paramètres de calibration

| Paramètre MOTIS | Valeur | Interprétation |
|---|---:|---|
| `pedestrianSpeed` | `1.25` | Marche à 1,25 m/s, soit 4,5 km/h. |
| `cyclingSpeed` | `4.17` | Vélo à environ 4,17 m/s, soit 15 km/h. |
| `minTransferTime` | `2` | Une correspondance doit disposer d’au moins deux minutes. |
| `additionalTransferTime` | `2` | Deux minutes de prudence sont ajoutées pour effectuer chaque correspondance. |
| `transferTimeFactor` | `1.15` | Un cheminement théorique de 10 minutes est évalué à environ 11,5 minutes. |
| `useRoutedTransfers` | `true` | MOTIS cherche un véritable chemin dans OpenStreetMap entre deux arrêts, au lieu de se fier uniquement au temps de correspondance inscrit dans le GTFS. |
| `detailedTransfers` | `true` | MOTIS retourne la géométrie et les étapes détaillées des correspondances. |

L’heure demandée par l’utilisateur est également comparée à l’heure d’arrivée
de chaque solution. L’attente avant le premier départ est ainsi comprise dans
la durée affichée. L’application retient ensuite le trajet qui arrive
réellement le plus tôt, et non celui dont la durée après l’embarquement est
simplement la plus courte.

## Limites appliquées aux itinéraires

Pour écarter les solutions excessivement longues ou complexes et limiter la
charge imposée à l’API Transitous, l’application utilise les bornes suivantes :

| Limite | Paramètre | Valeur |
|---|---|---:|
| Nombre maximal de correspondances | `maxTransfers` | `3` |
| Durée maximale totale | `maxTravelTime` | `360` minutes, soit 6 heures |
| Accès avant le premier transport collectif | `maxPreTransitTime` | `3 600` secondes, soit 1 heure |
| Accès après le dernier transport collectif | `maxPostTransitTime` | `3 600` secondes, soit 1 heure |
| Temps maximal accordé au calcul MOTIS | `timeout` | `20` secondes |
| Alternatives détaillées par segment | `numLegAlternatives` | `0` |

La valeur `numLegAlternatives = 0` évite de demander des variantes inutiles
pour chaque segment. Elle ne supprime pas la recherche des itinéraires
principaux permettant de comparer le vélo, le transport collectif et le mode
vélo + TC.

### Pour l'utilisation via les API Google
Il faut ensuite créer deux clés Google Maps Platform :

- une clé serveur avec la **Routes API** activée;
- une clé navigateur avec la **Maps JavaScript API** activée et une restriction
  par référent HTTP adaptée à l'adresse où l'application sera ouverte.

Les deux clés sont distinctes afin de pouvoir appliquer les restrictions de
sécurité recommandées pour chaque usage.

```bash
export GOOGLE_ROUTES_API_KEY="votre-cle-serveur"
export GOOGLE_MAPS_BROWSER_KEY="votre-cle-navigateur"
streamlit run accessibilite_plages_montreal.py
```

Google Maps Platform peut facturer les requêtes au-delà de son crédit ou de ses
quotas. Un calcul complet représente dix demandes d’itinéraire : cinq à vélo et
cinq en transport collectif.
