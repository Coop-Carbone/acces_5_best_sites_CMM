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
