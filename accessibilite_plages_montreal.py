"""Comparateur vélo / transport collectif des plages du Grand Montréal.

Les itinéraires sont calculés par MOTIS 2 via l'instance communautaire
Transitous, à partir de données ouvertes OpenStreetMap et GTFS.
"""

from __future__ import annotations

import json
import math
import os
from html import escape
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import plotly.express as px
import polyline
import requests
import streamlit as st
import streamlit.components.v1 as components


MONTREAL_TZ = ZoneInfo("America/Toronto")

# Coordonnées OpenStreetMap vérifiées des cinq lieux du palmarès de la CMM.
PLAGES = {
    "Plage urbaine de Verdun": (45.462697, -73.5601118),
    "RécréoParc": (45.4071881, -73.6005709),
    "Plage de l’Est": (45.6985922, -73.4814062),
    "Berge aux Quatre-Vents": (45.5422728, -73.8799235),
    "Pointe-Valaine": (45.5426493, -73.2198175),
}

COULEURS = {
    "Vélo": "#2F7657",
    "Transport collectif": "#3267B1",
    "Vélo + TC": "#B14678",
}
COULEUR_DEPART = "#D47A2C"

NUMEROS_PLAGES = {plage: numero for numero, plage in enumerate(PLAGES, start=1)}
LIGNES_STM_AVEC_SUPPORT = {"34", "50", "94", "140", "146", "180", "185", "769"}
LIGNES_RTL_VELO = {"61", "461", "462"}

TRANSITOUS_URL = "https://api.transitous.org/api"
APP_NAME = "PlagesGrandMontreal"
APP_VERSION = "0.2.0"


def obtenir_parametre(nom: str) -> str | None:
    """Lit un paramètre sans exiger la présence d'un fichier secrets.toml."""
    valeur = os.getenv(nom)
    if valeur:
        return valeur
    try:
        return st.secrets.get(nom)
    except Exception:
        return None


def entetes_transitous(contact: str) -> dict[str, str]:
    return {
        "Accept": "application/json",
        "User-Agent": f"{APP_NAME}/{APP_VERSION} (contact: {contact})",
    }


def appel_transitous(
    chemin: str,
    contact: str,
    parametres: list[tuple[str, str | int | float | bool]],
) -> dict | list:
    """Interroge uniquement l'endpoint public et stable de Transitous."""
    reponse = requests.get(
        f"{TRANSITOUS_URL}{chemin}",
        headers=entetes_transitous(contact),
        params=parametres,
        timeout=25,
    )
    if not reponse.ok:
        try:
            contenu = reponse.json()
            message = (
                contenu.get("error") or contenu.get("message") or reponse.text
                if isinstance(contenu, dict)
                else reponse.text
            )
        except requests.exceptions.JSONDecodeError:
            message = reponse.text
        raise RuntimeError(f"Transitous/MOTIS ({reponse.status_code}) : {message}")
    return reponse.json()


def geocoder_origine(origine: str, contact: str) -> tuple[float, float, str]:
    """Résout le départ avec le géocodeur MOTIS, biaisé vers Montréal."""
    resultats = appel_transitous(
        "/v1/geocode",
        contact,
        [
            ("text", origine),
            ("place", "45.52,-73.58"),
            ("placeBias", 5),
            ("language", "fr"),
            ("numResults", 5),
        ],
    )
    candidats = [
        resultat
        for resultat in resultats
        if resultat.get("country") == "CA"
        and 44.8 <= resultat.get("lat", 0) <= 46.2
        and -74.5 <= resultat.get("lon", 0) <= -72.5
    ]
    if not candidats:
        raise RuntimeError(
            "Le point de départ n’a pas été reconnu dans la région de Montréal."
        )
    lieu = candidats[0]
    return lieu["lat"], lieu["lon"], lieu.get("name", origine)


def decoder_geometrie(geometrie: dict | None) -> list[tuple[float, float]]:
    if not geometrie or not geometrie.get("points"):
        return []
    return polyline.decode(
        geometrie["points"],
        precision=int(geometrie.get("precision", 6)),
    )


def distance_trace_metres(trace: list[tuple[float, float]]) -> float:
    """Approxime la distance d'une trace par la formule de Haversine."""
    total = 0.0
    for (lat1, lon1), (lat2, lon2) in zip(trace, trace[1:]):
        phi1, phi2 = math.radians(lat1), math.radians(lat2)
        dphi = math.radians(lat2 - lat1)
        dlambda = math.radians(lon2 - lon1)
        a = (
            math.sin(dphi / 2) ** 2
            + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
        )
        total += 2 * 6_371_000 * math.asin(math.sqrt(a))
    return total


def minutes_et_distance_motis(duree: float, distance: float | None) -> str:
    minutes = round(duree / 60)
    kilometres = round((distance or 0) / 1000, 1)
    details = []
    if minutes:
        details.append(f"{minutes} min")
    if kilometres:
        details.append(f"{kilometres:.1f} km")
    return f" ({' · '.join(details)})" if details else ""


NOMS_MODES = {
    "WALK": "Marche",
    "BIKE": "Vélo",
    "BUS": "Autobus",
    "SUBWAY": "Métro",
    "REGIONAL_RAIL": "Train",
    "SUBURBAN": "Train",
    "TRAM": "Tramway",
    "FERRY": "Navette fluviale",
}

DIRECTIONS = {
    "DEPART": "Partir",
    "HARD_LEFT": "Tourner franchement à gauche",
    "LEFT": "Tourner à gauche",
    "SLIGHTLY_LEFT": "Obliquer à gauche",
    "CONTINUE": "Continuer",
    "SLIGHTLY_RIGHT": "Obliquer à droite",
    "RIGHT": "Tourner à droite",
    "HARD_RIGHT": "Tourner franchement à droite",
    "UTURN_LEFT": "Faire demi-tour à gauche",
    "UTURN_RIGHT": "Faire demi-tour à droite",
    "STAIRS": "Prendre l’escalier",
    "ELEVATOR": "Prendre l’ascenseur",
}


def decrire_leg(leg: dict) -> list[str]:
    """Produit des instructions françaises courtes à partir d'un leg MOTIS."""
    mode = leg.get("mode", "")
    if mode not in {"WALK", "BIKE"}:
        numero = leg.get("routeShortName") or leg.get("displayName") or ""
        depart = leg.get("from", {}).get("name", "arrêt de départ")
        arrivee = leg.get("to", {}).get("name", "arrêt d’arrivée")
        libelle = f"{NOMS_MODES.get(mode, 'Transport collectif')} {numero}".strip()
        return [
            escape(
                f"{libelle} : {depart} → {arrivee}"
                + minutes_et_distance_motis(leg.get("duration", 0), None)
            )
        ]

    groupes: list[dict] = []
    for etape in leg.get("steps", []) or []:
        direction = etape.get("relativeDirection")
        rue = etape.get("streetName") or ""
        cle = (direction, rue)
        if groupes and groupes[-1]["cle"] == cle:
            groupes[-1]["distance"] += etape.get("distance") or 0
        else:
            groupes.append(
                {
                    "cle": cle,
                    "direction": direction,
                    "rue": rue,
                    "distance": etape.get("distance") or 0,
                }
            )
    instructions = []
    for groupe in groupes:
        action = DIRECTIONS.get(groupe["direction"], "Continuer")
        rue = groupe["rue"]
        texte = f"{action}{f' sur {rue}' if rue else ''}"
        instructions.append(
            escape(texte + minutes_et_distance_motis(0, groupe["distance"]))
        )
    if instructions:
        return instructions
    lieu = leg.get("to", {}).get("name", "la prochaine étape")
    return [
        escape(
            f"{NOMS_MODES.get(mode, 'Continuer')} jusqu’à {lieu}"
            + minutes_et_distance_motis(
                leg.get("duration", 0), leg.get("distance")
            )
        )
    ]


def heure_locale(valeur: str | None) -> str | None:
    if not valeur:
        return None
    return (
        datetime.fromisoformat(valeur.replace("Z", "+00:00"))
        .astimezone(MONTREAL_TZ)
        .strftime("%H:%M")
    )


def resumer_segment(leg: dict) -> dict:
    """Normalise un segment MOTIS pour une chronologie lisible sur la carte."""
    mode = leg.get("mode", "")
    numero = leg.get("routeShortName") or leg.get("displayName") or ""
    titre = NOMS_MODES.get(mode, "Déplacement")
    if numero and mode not in {"WALK", "BIKE"}:
        titre = f"{titre} {numero}"
    direction = leg.get("headsign")
    if direction and mode not in {"WALK", "BIKE"}:
        titre += f" vers {direction}"

    distance = leg.get("distance")
    trace_leg = decoder_geometrie(leg.get("legGeometry"))
    if distance is None and trace_leg:
        distance = distance_trace_metres(trace_leg)

    intermediaires = leg.get("intermediateStops") or []
    return {
        "mode": mode,
        "titre": escape(titre),
        "depart": escape(leg.get("from", {}).get("name", "Départ")),
        "arrivee": escape(leg.get("to", {}).get("name", "Arrivée")),
        "heure_depart": heure_locale(leg.get("startTime")),
        "heure_arrivee": heure_locale(leg.get("endTime")),
        "duree_min": max(1, round(leg.get("duration", 0) / 60)),
        "distance_km": round(distance / 1000, 1) if distance else None,
        "agence": escape(str(leg.get("agencyName") or "")),
        "nombre_arrets": len(intermediaires) + 2 if intermediaires else None,
        "etapes": decrire_leg(leg) if mode in {"WALK", "BIKE"} else [],
    }


def metro_stm_autorise(moment: datetime) -> bool:
    if date(2026, 5, 18) <= moment.date() <= date(2026, 8, 16):
        return True
    if moment.weekday() >= 5:
        return True
    heure = moment.hour + moment.minute / 60
    return heure < 7 or 9.5 <= heure <= 15.5 or heure >= 18


def autorisation_velo_tc(
    legs_tc: list[dict],
    depart: datetime,
) -> tuple[str, str]:
    """Évalue l'embarquement d'un vélo standard selon l'opérateur et l'heure."""
    niveaux: list[str] = []
    raisons: list[str] = []

    for leg in legs_tc:
        numero = str(leg.get("routeShortName") or leg.get("displayName") or "")
        vehicule = leg.get("mode", "")
        agences = str(leg.get("agencyName") or "").lower()
        heure_iso = leg.get("startTime")
        moment = (
            datetime.fromisoformat(heure_iso.replace("Z", "+00:00")).astimezone(
                MONTREAL_TZ
            )
            if heure_iso
            else depart
        )
        est_rem = (
            "réseau express métropolitain" in agences
            or "reseau express metropolitain" in agences
            or agences.strip() == "rem"
        )

        if vehicule == "SUBWAY" and not est_rem:
            permis = metro_stm_autorise(moment)
            niveaux.append("autorisé" if permis else "interdit")
            raisons.append(
                "métro STM permis à cette heure"
                if permis
                else "vélo interdit dans le métro STM à cette heure"
            )
        elif est_rem or vehicule == "TRAM":
            ete_2026 = date(2026, 5, 18) <= moment.date() <= date(2026, 8, 16)
            heure = moment.hour + moment.minute / 60
            pointe = moment.weekday() < 5 and (
                7 <= heure <= 9.5 or 15.5 <= heure <= 18
            )
            permis = ete_2026 or not pointe
            niveaux.append("autorisé" if permis else "interdit")
            raisons.append(
                "REM permis à cette heure"
                if permis
                else "vélo interdit dans le REM à cette heure de pointe"
            )
        elif vehicule in {"REGIONAL_RAIL", "SUBURBAN"}:
            niveaux.append("autorisé")
            raisons.append("vélo permis dans les trains exo, selon l’espace")
        elif vehicule == "FERRY":
            niveaux.append("autorisé")
            raisons.append("vélo permis sur la navette fluviale")
        elif vehicule == "BUS":
            if "montréal" in agences or "stm" in agences:
                permis = numero in LIGNES_STM_AVEC_SUPPORT
                niveaux.append("conditionnel" if permis else "interdit")
                raisons.append(
                    f"support disponible sur la ligne STM {numero}, selon la place"
                    if permis
                    else f"vélo non accepté sur la ligne STM {numero}"
                )
            elif "longueuil" in agences or "rtl" in agences:
                permis = numero in LIGNES_RTL_VELO
                niveaux.append("conditionnel" if permis else "interdit")
                raisons.append(
                    f"vélo accepté sous conditions sur la ligne RTL {numero}"
                    if permis
                    else f"vélo non accepté sur la ligne RTL {numero}"
                )
            elif "laval" in agences or "stl" in agences:
                en_saison = date(moment.year, 4, 15) <= moment.date() <= date(
                    moment.year, 11, 14
                )
                niveaux.append("conditionnel" if en_saison else "interdit")
                raisons.append(
                    "support Cyclobus STL, selon la lumière et la place"
                    if en_saison
                    else "supports Cyclobus STL hors saison"
                )
            elif "exo" in agences:
                en_saison = date(moment.year, 4, 15) <= moment.date() <= date(
                    moment.year, 10, 31
                )
                heure_decimale = moment.hour + moment.minute / 60
                if moment.month <= 5:
                    heure_limite = 21
                elif moment.month <= 8:
                    heure_limite = 21.5
                else:
                    heure_limite = 19.5
                disponible = en_saison and heure_decimale <= heure_limite
                niveaux.append("conditionnel" if disponible else "interdit")
                raisons.append(
                    "support vélo exo, selon le secteur, l’heure et la place"
                    if disponible
                    else "support vélo exo indisponible à cette date ou cette heure"
                )
            else:
                niveaux.append("conditionnel")
                raisons.append("règle du transporteur d’autobus à confirmer")
        else:
            niveaux.append("conditionnel")
            raisons.append("règle du transporteur à confirmer")

    raisons_uniques = list(dict.fromkeys(raisons))
    if "interdit" in niveaux:
        return "Non autorisé", " · ".join(raisons_uniques)
    if "conditionnel" in niveaux:
        return "Conditionnel", " · ".join(raisons_uniques)
    return "Autorisé", " · ".join(raisons_uniques)


def parametres_plan(
    origine: tuple[float, float],
    destination: tuple[float, float],
    depart: datetime,
    acces: str,
) -> list[tuple[str, str | int | float | bool]]:
    """Construit une requête bornée afin de limiter la charge Transitous."""
    return [
        ("fromPlace", f"{origine[0]},{origine[1]}"),
        ("toPlace", f"{destination[0]},{destination[1]}"),
        ("time", depart.isoformat(timespec="seconds")),
        ("language", "fr"),
        ("directModes", "BIKE" if acces == "WALK" else ""),
        ("transitModes", "TRANSIT"),
        ("preTransitModes", acces),
        ("postTransitModes", acces),
        ("maxPreTransitTime", 3600),
        ("maxPostTransitTime", 3600),
        ("maxDirectTime", 14400),
        ("maxTravelTime", 360),
        ("maxTransfers", 3),
        ("minTransferTime", 2),
        ("additionalTransferTime", 2),
        ("transferTimeFactor", 1.15),
        ("pedestrianSpeed", 1.25),
        ("cyclingSpeed", 4.17),
        ("useRoutedTransfers", True),
        ("detailedTransfers", True),
        ("fastestDirectFactor", 10),
        ("detailedLegs", True),
        ("numLegAlternatives", 0),
        ("timeout", 20),
    ]


def duree_depuis_depart(itineraire: dict, depart: datetime) -> float:
    """Inclut l'attente avant le premier départ dans la durée comparée."""
    fin_iso = itineraire.get("endTime")
    if not fin_iso:
        return float(itineraire.get("duration", math.inf))
    fin = datetime.fromisoformat(fin_iso.replace("Z", "+00:00")).astimezone(
        depart.tzinfo
    )
    return max(float(itineraire.get("duration", 0)), (fin - depart).total_seconds())


def choisir_plus_rapide(
    itineraires: list[dict], depart: datetime
) -> dict | None:
    return min(
        itineraires,
        key=lambda item: duree_depuis_depart(item, depart),
        default=None,
    )


def resumer_itineraire(
    plage: str,
    libelle_mode: str,
    itineraire: dict,
    origine: tuple[float, float],
    destination: tuple[float, float],
    depart: datetime,
) -> dict:
    """Transforme un itinéraire MOTIS en données visualisables."""
    legs = itineraire.get("legs", [])
    legs_tc = [leg for leg in legs if leg.get("mode") not in {"WALK", "BIKE"}]
    marche_secondes = sum(
        leg.get("duration", 0) for leg in legs if leg.get("mode") == "WALK"
    )
    trace: list[tuple[float, float]] = []
    distance_metres = 0.0
    etapes: list[str] = []
    for leg in legs:
        trace_leg = decoder_geometrie(leg.get("legGeometry"))
        trace.extend(trace_leg)
        distance_metres += leg.get("distance") or distance_trace_metres(trace_leg)
        etapes.extend(decrire_leg(leg))

    segments = [resumer_segment(leg) for leg in legs]
    if legs and legs[0].get("startTime"):
        premier_depart = datetime.fromisoformat(
            legs[0]["startTime"].replace("Z", "+00:00")
        ).astimezone(depart.tzinfo)
        attente = round((premier_depart - depart).total_seconds() / 60)
        if attente > 0:
            segments.insert(
                0,
                {
                    "mode": "WAIT",
                    "titre": "Attente avant le départ",
                    "depart": "",
                    "arrivee": "",
                    "heure_depart": depart.strftime("%H:%M"),
                    "heure_arrivee": premier_depart.strftime("%H:%M"),
                    "duree_min": attente,
                    "distance_km": None,
                    "agence": "",
                    "nombre_arrets": None,
                    "etapes": [],
                },
            )

    return {
        "plage": plage,
        "mode": libelle_mode,
        "duree_min": round(duree_depuis_depart(itineraire, depart) / 60),
        "distance_km": round(distance_metres / 1000, 1),
        "correspondances": (
            itineraire.get("transfers", max(0, len(legs_tc) - 1))
            if legs_tc
            else None
        ),
        "marche_min": round(marche_secondes / 60) if legs_tc else None,
        "autorisation": "—",
        "etapes": etapes,
        "segments": segments,
        "origine_coord": origine,
        "destination_coord": destination,
        "trace": trace or [origine, destination],
        "_legs_tc": legs_tc,
    }


@st.cache_data(ttl=1800, show_spinner=False)
def calculer_accessibilite(
    contact: str,
    origine: str,
    depart_iso: str,
) -> tuple[list[dict], list[str]]:
    """Calcule les accès avec au plus dix requêtes de routage par comparaison."""
    depart = datetime.fromisoformat(depart_iso)
    resultats: list[dict] = []
    avertissements: list[str] = []

    try:
        latitude, longitude, nom_origine = geocoder_origine(origine, contact)
        origine_coord = (latitude, longitude)
    except (requests.RequestException, RuntimeError) as erreur:
        return [], [f"Point de départ : {erreur}"]

    for plage, destination in PLAGES.items():
        try:
            reponse = appel_transitous(
                "/v6/plan",
                contact,
                parametres_plan(origine_coord, destination, depart, "WALK"),
            )
            itineraire_velo = choisir_plus_rapide(
                reponse.get("direct", []), depart
            )
            itineraire_tc = choisir_plus_rapide(
                reponse.get("itineraries", []), depart
            )

            if itineraire_velo:
                resultats.append(
                    resumer_itineraire(
                        plage,
                        "Vélo",
                        itineraire_velo,
                        origine_coord,
                        destination,
                        depart,
                    )
                )
            else:
                avertissements.append(f"{plage} — Vélo : aucun trajet.")

            resume_tc = None
            if itineraire_tc:
                resume_tc = resumer_itineraire(
                    plage,
                    "Transport collectif",
                    itineraire_tc,
                    origine_coord,
                    destination,
                    depart,
                )
                statut, raison = autorisation_velo_tc(
                    resume_tc["_legs_tc"], depart
                )
                resume_tc["velo_tc_statut"] = statut
                resume_tc["velo_tc_raison"] = raison
                resultats.append(resume_tc)
            else:
                avertissements.append(
                    f"{plage} — Transport collectif : aucun trajet à cet horaire."
                )

            # MOTIS reçoit BIKE pour le premier et dernier kilomètre. On ne
            # force pas requireBikeTransport, car les GTFS montréalais marquent
            # plusieurs trajets comme interdits même lorsque les règles locales
            # les autorisent à certaines heures. La validation est faite ici.
            reponse_mixte = appel_transitous(
                "/v6/plan",
                contact,
                parametres_plan(origine_coord, destination, depart, "BIKE"),
            )
            itineraire_mixte = choisir_plus_rapide(
                reponse_mixte.get("itineraries", []), depart
            )
            if itineraire_mixte:
                resume_mixte = resumer_itineraire(
                    plage,
                    "Vélo + TC",
                    itineraire_mixte,
                    origine_coord,
                    destination,
                    depart,
                )
                statut, raison = autorisation_velo_tc(
                    resume_mixte["_legs_tc"], depart
                )
                if statut != "Non autorisé":
                    resume_mixte["autorisation"] = f"{statut} — {raison}"
                    resultats.append(resume_mixte)
                else:
                    avertissements.append(f"{plage} — Vélo + TC : {raison}.")
            elif resume_tc:
                avertissements.append(
                    f"{plage} — Vélo + TC : aucun trajet multimodal."
                )
        except (requests.RequestException, RuntimeError) as erreur:
            avertissements.append(f"{plage} : {erreur}.")

    for resultat in resultats:
        resultat.pop("_legs_tc", None)
        resultat["origine_nom"] = nom_origine

    return resultats, avertissements


def creer_carte_html(
    resultats: list[dict],
    modes_affiches: list[str],
) -> str:
    """Prépare une carte Leaflet/OpenStreetMap des plages et de leurs accès."""
    par_plage: list[dict] = []
    for numero, plage in enumerate(PLAGES, start=1):
        acces = [r for r in resultats if r["plage"] == plage]
        if not acces:
            continue
        transport = next(
            (r for r in acces if r["mode"] == "Transport collectif"), None
        )
        par_plage.append(
            {
                "numero": numero,
                "nom": plage,
                "position": {
                    "lat": acces[0]["destination_coord"][0],
                    "lng": acces[0]["destination_coord"][1],
                },
                "acces": [
                    {
                        "mode": resultat["mode"],
                        "couleur": COULEURS[resultat["mode"]],
                        "duree": resultat["duree_min"],
                        "distance": resultat["distance_km"],
                        "autorisation": resultat.get("autorisation", "—"),
                        "etapes": resultat.get("etapes", []),
                        "segments": resultat.get("segments", []),
                    }
                    for resultat in acces
                ],
                "velo_tc_statut": (
                    transport.get("velo_tc_statut") if transport else None
                ),
                "velo_tc_raison": (
                    transport.get("velo_tc_raison") if transport else None
                ),
            }
        )

    selection = [r for r in resultats if r["mode"] in modes_affiches]
    trajets = [
        {
            "plage": resultat["plage"],
            "mode": resultat["mode"],
            "duree": resultat["duree_min"],
            "couleur": COULEURS[resultat["mode"]],
            "autorisation": resultat.get("autorisation", "—"),
            "etapes": resultat.get("etapes", []),
            "segments": resultat.get("segments", []),
            "trace": [
                {"lat": latitude, "lng": longitude}
                for latitude, longitude in resultat["trace"]
            ],
            "origine": {
                "lat": resultat["origine_coord"][0],
                "lng": resultat["origine_coord"][1],
            },
            "destination": {
                "lat": resultat["destination_coord"][0],
                "lng": resultat["destination_coord"][1],
            },
        }
        for resultat in selection
    ]
    origine = {
        "lat": resultats[0]["origine_coord"][0],
        "lng": resultats[0]["origine_coord"][1],
    }
    donnees_json = json.dumps(trajets, ensure_ascii=False)
    plages_json = json.dumps(par_plage, ensure_ascii=False)
    origine_json = json.dumps(origine)

    return f"""
<link rel="stylesheet"
  href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<style>
  #carte-conteneur {{ height: 520px; position: relative; width: 100%; }}
  #carte {{ width: 100%; height: 520px; border-radius: 8px; }}
  #panneau-details {{
    background: #fff;
    border-radius: 8px;
    box-shadow: 0 4px 18px rgba(0,0,0,.28);
    box-sizing: border-box;
    color: #1f2937;
    font-family: system-ui, sans-serif;
    line-height: 1.35;
    max-height: calc(100% - 24px);
    overflow-y: auto;
    padding: 16px;
    position: absolute;
    right: 12px;
    top: 12px;
    width: min(400px, calc(100% - 24px));
    z-index: 1000;
  }}
  #panneau-details[hidden] {{ display: none; }}
  .panneau-fermer {{
    align-items: center;
    background: #fff;
    border: 1px solid #cbd5e1;
    border-radius: 50%;
    cursor: pointer;
    display: flex;
    font-size: 20px;
    height: 30px;
    justify-content: center;
    position: sticky;
    float: right;
    right: 0;
    top: 0;
    width: 30px;
    z-index: 2;
  }}
  .panneau-titre {{ margin: 2px 38px 10px 0; }}
  .plage-numero, .depart-marqueur {{
    align-items: center;
    border: 2px solid rgba(255,255,255,.95);
    border-radius: 50%;
    color: #fff;
    display: flex;
    font: 700 13px/1 system-ui, sans-serif;
    height: 28px;
    justify-content: center;
    width: 28px;
    box-shadow: 0 1px 4px rgba(0,0,0,.35);
  }}
  .plage-numero {{ background: #475569; }}
  .depart-marqueur {{ background: {COULEUR_DEPART}; }}
  .acces-detail {{
    border-left: 4px solid var(--mode-color);
    margin-top: 9px;
    padding-left: 9px;
  }}
  .acces-bouton {{
    align-items: center;
    background: transparent;
    border: 0;
    color: inherit;
    cursor: pointer;
    display: flex;
    font: inherit;
    gap: 6px;
    padding: 5px 28px 5px 0;
    text-align: left;
    width: 100%;
  }}
  .acces-bouton:hover {{ text-decoration: underline; }}
  .acces-bouton:focus-visible {{
    outline: 2px solid var(--mode-color);
    outline-offset: 2px;
  }}
  .acces-chevron {{
    display: inline-block;
    font-size: 16px;
    min-width: 12px;
  }}
  .acces-contenu[hidden] {{
    display: none;
  }}
  .itineraire-defilement {{
    margin-top: 8px;
    max-height: 260px;
    overflow-y: auto;
    padding-right: 6px;
  }}
  .segment {{
    display: grid;
    gap: 8px;
    grid-template-columns: 28px minmax(0, 1fr);
    padding: 5px 0 10px;
    position: relative;
  }}
  .segment:not(:last-child)::after {{
    background: #cbd5e1;
    content: "";
    height: calc(100% - 30px);
    left: 13px;
    position: absolute;
    top: 32px;
    width: 2px;
  }}
  .segment-icone {{
    align-items: center;
    background: #f1f5f9;
    border: 1px solid #cbd5e1;
    border-radius: 50%;
    display: flex;
    font-size: 15px;
    height: 28px;
    justify-content: center;
    position: relative;
    width: 28px;
    z-index: 1;
  }}
  .segment-entete {{
    align-items: baseline;
    display: flex;
    gap: 8px;
    justify-content: space-between;
  }}
  .segment-heures, .segment-meta, .segment-lieux {{
    color: #475569;
    font-size: 12px;
  }}
  .segment-lieux {{ margin-top: 2px; }}
  .segment-meta {{ margin-top: 3px; }}
  .segment-etapes {{
    margin: 6px 0 0;
    padding-left: 18px;
  }}
  .segment-etapes li {{ margin-bottom: 3px; }}
</style>
<div id="carte-conteneur">
  <div id="carte" aria-label="Itinéraires vers les plages"></div>
  <section id="panneau-details" aria-live="polite" hidden></section>
</div>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
  const trajets = {donnees_json};
  const plages = {plages_json};
  const origine = {origine_json};
  const panneau = document.getElementById("panneau-details");
  const carte = L.map("carte", {{
    scrollWheelZoom: false,
    zoomControl: true
  }});
  L.tileLayer("https://tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png", {{
    maxZoom: 19,
    attribution:
      '&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank">' +
      'contributeurs OpenStreetMap</a>'
  }}).addTo(carte);
  const limites = L.latLngBounds([]);
  const iconeDepart = L.divIcon({{
    className: "",
    html: '<div class="depart-marqueur">D</div>',
    iconSize: [28, 28],
    iconAnchor: [14, 14]
  }});
  const marqueurDepart = L.marker([origine.lat, origine.lng], {{
    icon: iconeDepart,
    title: "Point de départ"
  }}).addTo(carte);
  marqueurDepart.on("click", () => ouvrirPanneau(
    '<h3 class="panneau-titre">Point de départ</h3>'
  ));
  limites.extend([origine.lat, origine.lng]);

  function ouvrirPanneau(contenu) {{
    panneau.innerHTML = `
      <button type="button" class="panneau-fermer"
        data-action="fermer-panneau" aria-label="Fermer les détails">×</button>
      ${{contenu}}
    `;
    panneau.hidden = false;
  }}

  function fermerPanneau() {{
    panneau.hidden = true;
    panneau.innerHTML = "";
  }}

  function iconeSegment(mode) {{
    if (mode === "WAIT") return "⏱";
    if (mode === "WALK") return "🚶";
    if (mode === "BIKE") return "🚲";
    if (mode === "SUBWAY") return "Ⓜ";
    if (mode === "BUS") return "🚌";
    if (mode === "FERRY") return "⛴";
    if (mode === "REGIONAL_RAIL" || mode === "SUBURBAN") return "🚆";
    if (mode === "TRAM") return "🚊";
    return "●";
  }}

  function listeEtapes(etapes) {{
    if (!etapes || !etapes.length) return "";
    return `<ol class="segment-etapes">${{
      etapes.map((etape) => `<li>${{etape}}</li>`).join("")
    }}</ol>`;
  }}

  function chronologie(segments) {{
    if (!segments || !segments.length) return "";
    return `<div class="itineraire-defilement">${{segments.map((segment) => {{
      const heures = segment.heure_depart && segment.heure_arrivee
        ? `${{segment.heure_depart}}–${{segment.heure_arrivee}}`
        : "";
      const distance = segment.distance_km
        ? ` · ${{segment.distance_km}} km`
        : "";
      const arrets = segment.nombre_arrets
        ? ` · ${{segment.nombre_arrets}} arrêts`
        : "";
      const agence = segment.agence ? ` · ${{segment.agence}}` : "";
      return `<div class="segment">
        <div class="segment-icone" aria-hidden="true">${{
          iconeSegment(segment.mode)
        }}</div>
        <div>
          <div class="segment-entete">
            <strong>${{segment.titre}}</strong>
            <span class="segment-heures">${{heures}}</span>
          </div>
          <div class="segment-lieux">${{
            segment.depart
          }} → ${{segment.arrivee}}</div>
          <div class="segment-meta">${{
            segment.duree_min
          }} min${{distance}}${{arrets}}${{agence}}</div>
          ${{listeEtapes(segment.etapes)}}
        </div>
      </div>`;
    }}).join("")}}</div>`;
  }}

  function basculerAcces(bouton, evenement) {{
    evenement.preventDefault();
    evenement.stopPropagation();
    const contenu = bouton.nextElementSibling;
    const ouvrir = contenu.hidden;
    contenu.hidden = !ouvrir;
    bouton.setAttribute("aria-expanded", String(ouvrir));
    bouton.querySelector(".acces-chevron").textContent = ouvrir ? "▾" : "▸";
  }}

  function blocAcces(acces) {{
    const icone = acces.mode === "Vélo"
      ? "🚲"
      : acces.mode === "Transport collectif" ? "🚇" : "🚲＋🚇";
    const autorisation = acces.mode === "Vélo + TC"
      ? `<div style="margin:5px 0;font-size:12px">${{acces.autorisation}}</div>`
      : "";
    const detail = chronologie(acces.segments)
      || `<div class="itineraire-defilement">${{
        listeEtapes(acces.etapes)
      }}</div>`;
    return `<div class="acces-detail" style="--mode-color:${{
      acces.couleur
    }}">
      <button type="button" class="acces-bouton" aria-expanded="false"
        data-action="basculer-acces">
        <span class="acces-chevron" aria-hidden="true">▸</span>
        <span>${{icone}} <strong>${{acces.mode}}</strong> · ${{
          acces.duree
        }} min · ${{acces.distance}} km</span>
      </button>
      <div class="acces-contenu" hidden>${{autorisation}}${{detail}}</div>
    </div>`;
  }}

  plages.forEach((plage) => {{
    let contenu = plage.acces.map(blocAcces).join("");
    if (!plage.acces.some((acces) => acces.mode === "Vélo + TC")
        && plage.velo_tc_statut) {{
      contenu += `<div style="margin-top:6px"><small>🚲＋🚇 ${{
        plage.velo_tc_statut
      }} — ${{plage.velo_tc_raison}}</small></div>`;
    }}
    const icone = L.divIcon({{
      className: "",
      html: `<div class="plage-numero">${{plage.numero}}</div>`,
      iconSize: [28, 28],
      iconAnchor: [14, 14]
    }});
    const marqueur = L.marker([plage.position.lat, plage.position.lng], {{
      icon: icone,
      title: plage.nom
    }}).addTo(carte);
    marqueur.on("click", () => ouvrirPanneau(
      `<h3 class="panneau-titre">${{
        plage.numero
      }}. ${{plage.nom}}</h3>${{contenu}}`
    ));
    limites.extend([plage.position.lat, plage.position.lng]);
  }});

  trajets.forEach((trajet) => {{
    const dashArray = trajet.mode === "Transport collectif"
      ? "9 8"
      : trajet.mode === "Vélo + TC" ? "18 12" : null;
    const ligne = L.polyline(
      trajet.trace.map((point) => [point.lat, point.lng]),
      {{
        color: trajet.couleur,
        weight: 4,
        opacity: 0.88,
        dashArray
      }}
    ).addTo(carte);
    const autorisation = trajet.mode === "Vélo + TC"
      ? `<br><small>${{trajet.autorisation}}</small>`
      : "";
    ligne.on("click", () => ouvrirPanneau(
      `<h3 class="panneau-titre">${{
        trajet.plage
      }}</h3><strong>${{trajet.mode}}</strong> · ${{trajet.duree}} min${{
        autorisation
      }}${{chronologie(trajet.segments)
        || `<div class="itineraire-defilement">${{
          listeEtapes(trajet.etapes)
        }}</div>`}}`
    ));
  }});
  panneau.addEventListener("click", (evenement) => {{
    const cible = evenement.target;
    const fermer = cible.closest
      ? cible.closest('[data-action="fermer-panneau"]')
      : null;
    if (fermer) {{
      fermerPanneau();
      return;
    }}
    const bouton = cible.closest
      ? cible.closest('[data-action="basculer-acces"]')
      : null;
    if (bouton) basculerAcces(bouton, evenement);
  }});
  carte.fitBounds(limites, {{padding: [32, 32]}});
</script>
"""


def afficher_comparaison(resultats: list[dict]) -> None:
    """Affiche le graphique comparatif et un tableau de détails."""
    donnees = pd.DataFrame(resultats)
    donnees["numero"] = donnees["plage"].map(NUMEROS_PLAGES)
    donnees["plage_affichage"] = donnees.apply(
        lambda ligne: f'{ligne["numero"]}. {ligne["plage"]}', axis=1
    )
    ordre_plages = (
        donnees.groupby("plage")["duree_min"]
        .mean()
        .sort_values(ascending=True)
        .index.tolist()
    )
    ordre = [f"{NUMEROS_PLAGES[plage]}. {plage}" for plage in ordre_plages]

    figure = px.bar(
        donnees,
        x="duree_min",
        y="plage_affichage",
        color="mode",
        barmode="group",
        orientation="h",
        text="duree_min",
        category_orders={"plage_affichage": ordre},
        color_discrete_map=COULEURS,
        labels={
            "duree_min": "Durée estimée (minutes)",
            "plage_affichage": "",
            "mode": "Mode",
        },
    )
    figure.update_traces(texttemplate="%{text} min", textposition="outside")
    figure.update_layout(
        legend_title_text="",
        margin=dict(l=10, r=40, t=10, b=10),
        height=420,
    )
    st.plotly_chart(figure, use_container_width=True)

    tableau = donnees.copy()
    tableau["Distance"] = tableau["distance_km"].map(lambda x: f"{x:.1f} km")
    tableau["Durée"] = tableau["duree_min"].map(lambda x: f"{x} min")
    tableau["Correspondances"] = tableau["correspondances"].map(
        lambda x: "—" if pd.isna(x) else str(int(x))
    )
    tableau["Marche"] = tableau["marche_min"].map(
        lambda x: "—" if pd.isna(x) else f"{int(x)} min"
    )
    if "autorisation" not in tableau:
        tableau["autorisation"] = "—"
    tableau = tableau.rename(
        columns={
            "numero": "#",
            "plage": "Plage",
            "mode": "Mode",
            "autorisation": "Vélo dans le TC",
        }
    )
    st.dataframe(
        tableau[
            [
                "#",
                "Plage",
                "Mode",
                "Durée",
                "Distance",
                "Correspondances",
                "Marche",
                "Vélo dans le TC",
            ]
        ],
        hide_index=True,
        use_container_width=True,
    )


def afficher_legende() -> None:
    """Affiche les codes de la carte avec les mêmes couleurs que le graphique."""
    st.markdown(
        f"""
<div style="
  display:flex; flex-wrap:wrap; align-items:center; gap:10px 22px;
  padding:5px 0 10px; color:inherit;
">
  <span style="display:inline-flex;align-items:center;gap:8px">
    <svg width="42" height="12" aria-hidden="true">
      <line x1="1" y1="6" x2="41" y2="6"
        stroke="{COULEURS['Vélo']}" stroke-width="4" stroke-linecap="round"/>
    </svg>
    Vélo
  </span>
  <span style="display:inline-flex;align-items:center;gap:8px">
    <svg width="42" height="12" aria-hidden="true">
      <line x1="1" y1="6" x2="41" y2="6"
        stroke="{COULEURS['Transport collectif']}" stroke-width="4"
        stroke-dasharray="7 6" stroke-linecap="round"/>
    </svg>
    Transport collectif
  </span>
  <span style="display:inline-flex;align-items:center;gap:8px">
    <svg width="42" height="12" aria-hidden="true">
      <line x1="1" y1="6" x2="41" y2="6"
        stroke="{COULEURS['Vélo + TC']}" stroke-width="4"
        stroke-dasharray="15 9" stroke-linecap="round"/>
    </svg>
    Vélo + TC
  </span>
  <span style="display:inline-flex;align-items:center;gap:8px">
    <span aria-hidden="true" style="
      display:inline-flex;align-items:center;justify-content:center;
      width:24px;height:24px;border-radius:50%;background:{COULEUR_DEPART};
      color:white;font-weight:700;border:2px solid rgba(255,255,255,.9);
      box-shadow:0 1px 3px rgba(0,0,0,.25)
    ">D</span>
    Départ
  </span>
  <span style="opacity:.72">Sélectionnez une plage ou un trajet pour voir les étapes.</span>
</div>
""",
        unsafe_allow_html=True,
    )


def main() -> None:
    st.set_page_config(
        page_title="Accès aux plages du Grand Montréal",
        page_icon="🏖️",
        layout="wide",
    )
    st.title("Les plages du Grand Montréal, sans voiture")
    st.caption(
        "Comparaison des temps de trajet à vélo et en transport collectif "
        "vers les cinq plages sélectionnées par la CMM."
    )

    contact_transitous = obtenir_parametre("TRANSITOUS_CONTACT")
    if not contact_transitous:
        st.error(
            "Ajoutez TRANSITOUS_CONTACT avec une adresse courriel ou l’URL "
            "de votre dépôt GitHub/application. Ce n’est pas une clé secrète : "
            "cette identification est exigée par la politique d’utilisation "
            "de Transitous."
        )
        st.code(
            'export TRANSITOUS_CONTACT="votre-adresse@exemple.ca"',
            language="bash",
        )
        st.markdown(
            "[Consulter la politique d’utilisation de Transitous]"
            "(https://transitous.org/api/)"
        )
        st.stop()

    demain = date.today() + timedelta(days=1)
    with st.form("parametres"):
        col1, col2, col3 = st.columns([2, 1, 1])
        origine = col1.text_input(
            "Point de départ",
            value="Gare Centrale de Montréal, Montréal, Québec",
        )
        jour = col2.date_input("Jour du trajet", value=demain, min_value=date.today())
        heure = col3.time_input("Heure de départ", value=time(9, 0))
        lancer = st.form_submit_button("Comparer les accès", type="primary")

    if lancer:
        depart = datetime.combine(jour, heure, tzinfo=MONTREAL_TZ)
        with st.spinner("Calcul des itinéraires…"):
            resultats_calcules, avertissements_calcules = calculer_accessibilite(
                contact_transitous, origine.strip(), depart.isoformat()
            )
        st.session_state["resultats"] = resultats_calcules
        st.session_state["avertissements"] = avertissements_calcules
        st.session_state["contexte_calcul"] = (
            f"Départ : {origine.strip()} · "
            f"{depart.strftime('%d/%m/%Y à %H h %M')}"
        )

    if "resultats" not in st.session_state:
        st.info("Choisissez un départ, puis cliquez sur « Comparer les accès ».")
        st.stop()

    resultats = st.session_state["resultats"]
    avertissements = st.session_state.get("avertissements", [])
    st.caption(st.session_state.get("contexte_calcul", ""))

    if avertissements:
        with st.expander("Trajets non disponibles"):
            for avertissement in avertissements:
                st.write(f"• {avertissement}")

    if not resultats:
        st.error("Aucun itinéraire n’a pu être calculé.")
        st.stop()

    st.subheader("Carte des cinq plages et de leurs accès")
    modes_affiches = st.multiselect(
        "Itinéraires à afficher",
        options=["Vélo", "Transport collectif", "Vélo + TC"],
        default=["Vélo", "Transport collectif", "Vélo + TC"],
    )
    afficher_legende()
    carte_html = creer_carte_html(resultats, modes_affiches)
    components.html(carte_html, height=540)
    st.caption(
        "Calcul des trajets : [MOTIS via Transitous]"
        "(https://transitous.org/api/) · "
        "[Sources des données](https://transitous.org/sources/) · "
        "[Confidentialité Transitous](https://transitous.org/privacy/) · "
        "Fond de carte © contributeurs OpenStreetMap."
    )

    st.subheader("Comparaison des durées")
    afficher_comparaison(resultats)

    with st.expander("Règles utilisées pour l’option vélo + TC"):
        st.markdown(
            """
- [Métro et autobus STM](https://www.stm.info/fr/velo/bienvenue-aux-velos)
- [REM](https://rem.info/fr/se-deplacer/faq-sur-les-deplacements/est-ce-que-je-peux-transporter-mon-velo-dans-le-rem)
- [Trains et autobus exo](https://exo.quebec/fr/planifier-trajet/velo/velo-a-bord)
- [Cyclobus STL](https://stlaval.ca/modes-transport)
- [Autobus RTL](https://www.rtl-longueuil.qc.ca/infos-pratiques/securite-a-bord)
- [Navettes fluviales 2026](https://www.artm.quebec/retour-navettes-fluviales-saison-2026/)

Les supports d’autobus et les espaces à bord fonctionnent généralement selon
le principe du premier arrivé, premier servi. Une autorisation conditionnelle
ne garantit donc pas une place.
"""
        )

    st.caption(
        "Les estimations dépendent des horaires et des itinéraires fournis par "
        "MOTIS/Transitous au moment du calcul. Les règles concernent un vélo "
        "standard non électrique et la place n’est jamais garantie. Vérifiez "
        "le trajet avant de partir. "
        "[Données et sources Transitous](https://transitous.org/sources/) · "
        "[Politique d’utilisation](https://transitous.org/api/) · "
        "[OpenStreetMap](https://www.openstreetmap.org/copyright) · "
        "[Source des cinq plages : CMM]"
        "(https://cmm.qc.ca/nouvelles/top-5-des-plus-belles-plages-du-grand-montreal/)."
    )


if __name__ == "__main__":
    main()
