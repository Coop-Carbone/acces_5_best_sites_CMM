# Accessibilité des plages et des parcs du Grand Montréal — version Google

Cette application compare, depuis un point de départ choisi :

- les plages, les parcs ou l’ensemble des destinations;
- le temps et la distance à vélo;
- le temps en transport collectif;
- le nombre de correspondances et la marche associée;
- les destinations sélectionnées sur une même carte;
- les plages avec des points jaunes et les parcs avec des points verts;
- les itinéraires à vélo, en transport collectif et en combinaison vélo + TC;
- les étapes détaillées au clic sur un lieu ou un trajet;
- une vérification indicative des règles permettant un vélo standard dans le TC.

Les dix destinations proviennent des palmarès de la CMM :

- [Top 5 des plus belles plages du Grand Montréal](https://cmm.qc.ca/nouvelles/top-5-des-plus-belles-plages-du-grand-montreal/);
- [Top 5 des plus beaux parcs du Grand Montréal](https://cmm.qc.ca/nouvelles/top-5-des-plus-beaux-parcs-du-grand-montreal/).

## Installation

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Configuration des API Google

Il faut créer deux clés Google Maps Platform :

- une clé serveur avec la **Routes API** activée;
- une clé navigateur avec la **Maps JavaScript API** activée.

Les deux clés sont distinctes afin d’appliquer des restrictions adaptées à
chaque utilisation :

- limitez la clé serveur à la Routes API et, lorsque l’hébergement le permet,
  aux adresses IP du serveur;
- limitez la clé navigateur à la Maps JavaScript API et aux référents HTTP de
  l’application, par exemple
  `https://votre-application.streamlit.app/*`;
- ne placez jamais la clé serveur directement dans le code ou dans le dépôt.

Pour une utilisation locale :

```bash
export GOOGLE_ROUTES_API_KEY="votre-cle-serveur"
export GOOGLE_MAPS_BROWSER_KEY="votre-cle-navigateur"
streamlit run accessibilite_plages_montreal_google.py
```

Sur Streamlit Community Cloud, ajoutez plutôt les deux valeurs dans
**Manage app → Settings → Secrets** :

```toml
GOOGLE_ROUTES_API_KEY = "votre-cle-serveur"
GOOGLE_MAPS_BROWSER_KEY = "votre-cle-navigateur"
```

## Quotas et maîtrise des coûts

Google Maps Platform peut facturer les requêtes selon la tarification et les
crédits applicables à votre compte. Configurez dans Google Cloud :

- un quota quotidien pour la Routes API;
- des alertes de budget;
- des restrictions d’API sur chaque clé;
- des restrictions de référent HTTP sur la clé navigateur.

L’application met les résultats en cache pendant 30 minutes et ne recalcule pas
les trajets lorsqu’un simple filtre d’affichage de la carte est modifié.

Le calcul de base effectue deux requêtes par destination : une à vélo et une en
transport collectif. L’option vélo + TC peut ajouter jusqu’à deux requêtes pour
les accès à vélo. La sélection d’une seule catégorie traite cinq destinations;
la sélection « Tous » en traite dix. Le total réel dépend donc des trajets en
transport collectif trouvés et de l’autorisation d’emporter un vélo.

## Limites

- Les durées et horaires dépendent des résultats fournis par Google au moment
  du calcul.
- Le mode vélo + TC est une estimation construite à partir du trajet en
  transport collectif et de segments à vélo.
- Les règles de transport d’un vélo peuvent changer et la place à bord n’est
  jamais garantie.
- Les coordonnées et les résultats doivent être vérifiés avant un déplacement.
