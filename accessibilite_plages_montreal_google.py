"""Comparateur vélo / transport collectif des plages du Grand Montréal.

L'application interroge Google Maps Directions à l'exécution afin de tenir
compte du point de départ et de l'heure choisis par la personne qui l'utilise.
"""

from __future__ import annotations

import json
import os
from html import escape
from datetime import date, datetime, time, timedelta
from typing import Any
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo

import pandas as pd
import plotly.express as px
import polyline
import requests
import streamlit as st
import streamlit.components.v1 as components


MONTREAL_TZ = ZoneInfo("America/Toronto")

# Les cinq lieux proviennent du palmarès de la CMM.
PLAGES = {
    "Plage urbaine de Verdun": "Plage urbaine de Verdun, Montréal, Québec",
    "RécréoParc": "RécréoParc, Sainte-Catherine, Québec",
    "Plage de l’Est": "Plage de l'Est, Pointe-aux-Trembles, Montréal, Québec",
    "Berge aux Quatre-Vents": "Berge aux Quatre-Vents, Laval, Québec",
    "Pointe-Valaine": "Plage de la Pointe-Valaine, Otterburn Park, Québec",
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

ROUTES_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"
FIELD_MASK = ",".join(
    [
        "routes.duration",
        "routes.distanceMeters",
        "routes.polyline.encodedPolyline",
        "routes.legs.startLocation",
        "routes.legs.endLocation",
        "routes.legs.steps.travelMode",
        "routes.legs.steps.staticDuration",
        "routes.legs.steps.distanceMeters",
        "routes.legs.steps.startLocation",
        "routes.legs.steps.endLocation",
        "routes.legs.steps.polyline.encodedPolyline",
        "routes.legs.steps.navigationInstruction.instructions",
        "routes.legs.steps.transitDetails",
    ]
)


def obtenir_cle(nom: str) -> str | None:
    """Lit une clé sans exiger la présence d'un fichier secrets.toml."""
    cle = os.getenv(nom)
    if cle:
        return cle
    try:
        return st.secrets.get(nom)
    except FileNotFoundError:
        return None


def premier_itineraire(
    cle_api: str,
    origine: str | tuple[float, float],
    destination: str | tuple[float, float],
    mode: str,
    depart: datetime,
) -> dict | None:
    """Interroge Routes API et retourne le premier trajet disponible."""
    def waypoint(valeur: str | tuple[float, float]) -> dict:
        if isinstance(valeur, str):
            return {"address": valeur}
        return {
            "location": {
                "latLng": {
                    "latitude": valeur[0],
                    "longitude": valeur[1],
                }
            }
        }

    requete: dict[str, Any] = {
        "origin": waypoint(origine),
        "destination": waypoint(destination),
        "travelMode": mode,
        "languageCode": "fr-CA",
        "regionCode": "ca",
        "computeAlternativeRoutes": False,
        "polylineQuality": "OVERVIEW",
        "polylineEncoding": "ENCODED_POLYLINE",
    }
    if mode == "TRANSIT":
        requete["departureTime"] = (
            depart.astimezone(ZoneInfo("UTC"))
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        )

    reponse = requests.post(
        ROUTES_URL,
        headers={
            "Content-Type": "application/json",
            "X-Goog-Api-Key": cle_api,
            "X-Goog-FieldMask": FIELD_MASK,
        },
        json=requete,
        timeout=30,
    )
    if not reponse.ok:
        try:
            message = reponse.json().get("error", {}).get("message", reponse.text)
        except requests.exceptions.JSONDecodeError:
            message = reponse.text
        raise RuntimeError(f"Routes API ({reponse.status_code}) : {message}")

    routes = reponse.json().get("routes", [])
    return routes[0] if routes else None


def duree_secondes(valeur: str | None) -> float:
    """Convertit une durée protobuf telle que '123.5s' en secondes."""
    if not valeur:
        return 0
    return float(valeur.removesuffix("s"))


def coordonnees(location: dict) -> tuple[float, float]:
    point = location["latLng"]
    return point["latitude"], point["longitude"]


def minutes_et_distance(etape: dict) -> str:
    minutes = round(duree_secondes(etape.get("staticDuration")) / 60)
    distance = round(etape.get("distanceMeters", 0) / 1000, 1)
    details = []
    if minutes:
        details.append(f"{minutes} min")
    if distance:
        details.append(f"{distance:.1f} km")
    return f" ({' · '.join(details)})" if details else ""


def decrire_etape(etape: dict) -> str:
    """Produit une instruction courte pour la carte."""
    transit = etape.get("transitDetails")
    if transit:
        ligne = transit.get("transitLine", {})
        vehicule = ligne.get("vehicle", {})
        type_vehicule = vehicule.get("name", {}).get("text") or vehicule.get(
            "type", "Transport collectif"
        )
        numero = ligne.get("nameShort") or ligne.get("name") or ""
        arrets = transit.get("stopDetails", {})
        depart = arrets.get("departureStop", {}).get("name", "arrêt de départ")
        arrivee = arrets.get("arrivalStop", {}).get("name", "arrêt d’arrivée")
        return escape(
            f"{type_vehicule} {numero}".strip()
            + f" : {depart} → {arrivee}"
            + minutes_et_distance(etape)
        )

    instruction = etape.get("navigationInstruction", {}).get("instructions")
    if not instruction:
        instruction = {
            "WALK": "Marcher",
            "BICYCLE": "Continuer à vélo",
        }.get(etape.get("travelMode"), "Continuer")
    return escape(instruction + minutes_et_distance(etape))


def heure_locale_google(valeur: str | None) -> str | None:
    """Convertit une heure ISO de Google dans le fuseau de Montréal."""
    if not valeur:
        return None
    try:
        moment = datetime.fromisoformat(valeur.replace("Z", "+00:00"))
        return moment.astimezone(MONTREAL_TZ).strftime("%H:%M")
    except ValueError:
        return None


def mode_segment_google(etape: dict) -> str:
    """Normalise les modes Google pour les icônes de la chronologie."""
    mode = etape.get("travelMode", "")
    if mode == "WALK":
        return "WALK"
    if mode == "BICYCLE":
        return "BIKE"
    transit = etape.get("transitDetails", {})
    vehicule = transit.get("transitLine", {}).get("vehicle", {}).get("type", "")
    correspondances = {
        "METRO": "SUBWAY",
        "SUBWAY": "SUBWAY",
        "BUS": "BUS",
        "FERRY": "FERRY",
        "TRAM": "TRAM",
        "LIGHT_RAIL": "TRAM",
        "HEAVY_RAIL": "REGIONAL_RAIL",
        "COMMUTER_TRAIN": "REGIONAL_RAIL",
        "HIGH_SPEED_TRAIN": "REGIONAL_RAIL",
        "LONG_DISTANCE_TRAIN": "REGIONAL_RAIL",
        "RAIL": "REGIONAL_RAIL",
    }
    return correspondances.get(vehicule, "TRANSIT")


def segment_google(
    etape: dict,
    depart_defaut: str,
    arrivee_defaut: str,
) -> dict:
    """Transforme une étape Google en segment lisible de type navigation."""
    transit = etape.get("transitDetails")
    mode = mode_segment_google(etape)
    duree = round(duree_secondes(etape.get("staticDuration")) / 60)
    distance = round(etape.get("distanceMeters", 0) / 1000, 1)
    instruction = etape.get("navigationInstruction", {}).get("instructions")

    if transit:
        ligne = transit.get("transitLine", {})
        vehicule = ligne.get("vehicle", {})
        type_vehicule = vehicule.get("name", {}).get("text") or {
            "SUBWAY": "Métro",
            "BUS": "Autobus",
            "FERRY": "Navette fluviale",
            "TRAM": "Tramway",
            "REGIONAL_RAIL": "Train",
        }.get(mode, "Transport collectif")
        numero = ligne.get("nameShort") or ligne.get("name") or ""
        arrets = transit.get("stopDetails", {})
        depart = arrets.get("departureStop", {}).get("name", depart_defaut)
        arrivee = arrets.get("arrivalStop", {}).get("name", arrivee_defaut)
        agences = ", ".join(
            agence.get("name", "")
            for agence in ligne.get("agencies", [])
            if agence.get("name")
        )
        return {
            "mode": mode,
            "titre": escape(f"{type_vehicule} {numero}".strip()),
            "depart": escape(depart),
            "arrivee": escape(arrivee),
            "duree_min": duree,
            "distance_km": distance,
            "nombre_arrets": transit.get("stopCount"),
            "agence": escape(agences),
            "heure_depart": heure_locale_google(arrets.get("departureTime")),
            "heure_arrivee": heure_locale_google(arrets.get("arrivalTime")),
            "etapes": [escape(instruction)] if instruction else [],
        }

    titre = {
        "WALK": "Marche",
        "BIKE": "Vélo",
    }.get(mode, "Déplacement")
    return {
        "mode": mode,
        "titre": titre,
        "depart": escape(depart_defaut),
        "arrivee": escape(arrivee_defaut),
        "duree_min": duree,
        "distance_km": distance,
        "nombre_arrets": None,
        "agence": "",
        "heure_depart": None,
        "heure_arrivee": None,
        "etapes": [escape(instruction)] if instruction else [],
    }


def segments_itineraire_google(
    itineraire: dict,
    depart: str,
    arrivee: str,
    etapes: list[dict] | None = None,
) -> list[dict]:
    """Construit la chronologie détaillée d'un itinéraire Google."""
    etapes_source = (
        etapes
        if etapes is not None
        else itineraire.get("legs", [{}])[0].get("steps", [])
    )
    segments: list[dict] = []
    for index, etape in enumerate(etapes_source):
        debut = depart if index == 0 else "Étape précédente"
        fin = arrivee if index == len(etapes_source) - 1 else "Étape suivante"
        segments.append(segment_google(etape, debut, fin))
    return segments


def metro_stm_autorise(moment: datetime) -> bool:
    if date(2026, 5, 18) <= moment.date() <= date(2026, 8, 16):
        return True
    if moment.weekday() >= 5:
        return True
    heure = moment.hour + moment.minute / 60
    return heure < 7 or 9.5 <= heure <= 15.5 or heure >= 18


def autorisation_velo_tc(
    etapes_tc: list[dict],
    depart: datetime,
) -> tuple[str, str]:
    """Évalue l'embarquement d'un vélo standard selon l'opérateur et l'heure."""
    niveaux: list[str] = []
    raisons: list[str] = []

    for etape in etapes_tc:
        transit = etape["transitDetails"]
        ligne = transit.get("transitLine", {})
        numero = str(ligne.get("nameShort") or ligne.get("name") or "")
        vehicule = ligne.get("vehicle", {}).get("type", "")
        agences = " ".join(
            agence.get("name", "") for agence in ligne.get("agencies", [])
        ).lower()
        heure_iso = transit.get("stopDetails", {}).get("departureTime")
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

        if vehicule in {"SUBWAY", "METRO"} and not est_rem:
            permis = metro_stm_autorise(moment)
            niveaux.append("autorisé" if permis else "interdit")
            raisons.append(
                "métro STM permis à cette heure"
                if permis
                else "vélo interdit dans le métro STM à cette heure"
            )
        elif est_rem or vehicule in {"TRAM", "LIGHT_RAIL"}:
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
        elif vehicule in {"HEAVY_RAIL", "COMMUTER_TRAIN", "RAIL"}:
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


def resumer_itineraire(
    plage: str,
    libelle_mode: str,
    itineraire: dict,
) -> dict:
    """Transforme la réponse détaillée de l'API en données visualisables."""
    leg = itineraire["legs"][0]
    etapes_tc = [
        etape for etape in leg.get("steps", []) if "transitDetails" in etape
    ]
    marche_secondes = sum(
        duree_secondes(etape.get("staticDuration"))
        for etape in leg.get("steps", [])
        if etape.get("travelMode") == "WALK"
    )
    debut = leg["startLocation"]["latLng"]
    fin = leg["endLocation"]["latLng"]

    return {
        "plage": plage,
        "mode": libelle_mode,
        "duree_min": round(duree_secondes(itineraire["duration"]) / 60),
        "distance_km": round(itineraire["distanceMeters"] / 1000, 1),
        "correspondances": max(0, len(etapes_tc) - 1) if etapes_tc else None,
        "marche_min": round(marche_secondes / 60) if etapes_tc else None,
        "autorisation": "—",
        "etapes": [decrire_etape(etape) for etape in leg.get("steps", [])],
        "segments": segments_itineraire_google(
            itineraire,
            "Point de départ",
            plage,
        ),
        "origine_coord": (
            debut["latitude"],
            debut["longitude"],
        ),
        "destination_coord": (
            fin["latitude"],
            fin["longitude"],
        ),
        "trace": polyline.decode(itineraire["polyline"]["encodedPolyline"]),
    }


def creer_itineraire_mixte(
    cle_api: str,
    plage: str,
    origine: str,
    destination: str,
    depart: datetime,
    itineraire_tc: dict,
    statut: str,
    raison: str,
) -> dict | None:
    """Remplace les accès à pied du trajet TC par des accès à vélo."""
    leg = itineraire_tc["legs"][0]
    etapes = leg.get("steps", [])
    indices_tc = [i for i, etape in enumerate(etapes) if "transitDetails" in etape]
    if not indices_tc or statut == "Non autorisé":
        return None

    premier_index, dernier_index = indices_tc[0], indices_tc[-1]
    premier_tc = etapes[premier_index]["transitDetails"]
    dernier_tc = etapes[dernier_index]["transitDetails"]
    embarquement = coordonnees(
        premier_tc["stopDetails"]["departureStop"]["location"]
    )
    debarquement = coordonnees(
        dernier_tc["stopDetails"]["arrivalStop"]["location"]
    )

    acces_velo = premier_itineraire(
        cle_api, origine, embarquement, "BICYCLE", depart
    )
    sortie_velo = premier_itineraire(
        cle_api, debarquement, destination, "BICYCLE", depart
    )
    if not acces_velo or not sortie_velo:
        return None

    avant_tc = sum(
        duree_secondes(etape.get("staticDuration"))
        for etape in etapes[:premier_index]
    )
    apres_tc = sum(
        duree_secondes(etape.get("staticDuration"))
        for etape in etapes[dernier_index + 1 :]
    )
    duree_acces_velo = duree_secondes(acces_velo["duration"])
    # Le premier véhicule conserve son horaire : arriver plus vite à vélo peut
    # ajouter de l'attente, mais ne permet pas de prendre ce départ plus tôt.
    duree_estimee = (
        duree_secondes(itineraire_tc["duration"])
        - apres_tc
        + duree_secondes(sortie_velo["duration"])
        + max(0, duree_acces_velo - avant_tc)
    )
    etapes_ferroviaires = etapes[premier_index : dernier_index + 1]
    distance_tc = sum(etape.get("distanceMeters", 0) for etape in etapes_ferroviaires)
    trace_tc: list[tuple[float, float]] = []
    for etape in etapes_ferroviaires:
        code = etape.get("polyline", {}).get("encodedPolyline")
        if code:
            trace_tc.extend(polyline.decode(code))

    debut = leg["startLocation"]["latLng"]
    fin = leg["endLocation"]["latLng"]
    nom_embarquement = premier_tc["stopDetails"]["departureStop"].get(
        "name", "l’arrêt"
    )
    nom_debarquement = dernier_tc["stopDetails"]["arrivalStop"].get(
        "name", "l’arrêt"
    )
    etapes_resumees = [
        escape(
            f"À vélo jusqu’à {nom_embarquement} "
            f"({round(duree_acces_velo / 60)} min)"
        ),
        *[
            decrire_etape(etape)
            for etape in etapes_ferroviaires
            if "transitDetails" in etape
        ],
        escape(
            f"À vélo de {nom_debarquement} à la plage "
            f"({round(duree_secondes(sortie_velo['duration']) / 60)} min)"
        ),
    ]

    return {
        "plage": plage,
        "mode": "Vélo + TC",
        "duree_min": round(duree_estimee / 60),
        "distance_km": round(
            (
                acces_velo["distanceMeters"]
                + distance_tc
                + sortie_velo["distanceMeters"]
            )
            / 1000,
            1,
        ),
        "correspondances": max(0, len(indices_tc) - 1),
        "marche_min": 0,
        "autorisation": f"{statut} — {raison}",
        "etapes": etapes_resumees,
        "segments": (
            segments_itineraire_google(
                acces_velo,
                "Point de départ",
                nom_embarquement,
            )
            + segments_itineraire_google(
                itineraire_tc,
                nom_embarquement,
                nom_debarquement,
                etapes=etapes_ferroviaires,
            )
            + segments_itineraire_google(
                sortie_velo,
                nom_debarquement,
                plage,
            )
        ),
        "origine_coord": (debut["latitude"], debut["longitude"]),
        "destination_coord": (fin["latitude"], fin["longitude"]),
        "trace": (
            polyline.decode(acces_velo["polyline"]["encodedPolyline"])
            + trace_tc
            + polyline.decode(sortie_velo["polyline"]["encodedPolyline"])
        ),
    }


@st.cache_data(ttl=1800, show_spinner=False)
def calculer_accessibilite(
    cle_api: str,
    origine: str,
    depart_iso: str,
) -> tuple[list[dict], list[str]]:
    """Calcule les itinéraires et conserve le résultat pendant 30 minutes."""
    depart = datetime.fromisoformat(depart_iso)
    resultats: list[dict] = []
    avertissements: list[str] = []

    for plage, destination in PLAGES.items():
        itineraire_tc = None
        resume_tc = None
        for mode_api, libelle in (
            ("BICYCLE", "Vélo"),
            ("TRANSIT", "Transport collectif"),
        ):
            try:
                itineraire = premier_itineraire(
                    cle_api, origine, destination, mode_api, depart
                )
                if not itineraire:
                    avertissements.append(f"{plage} — {libelle} : aucun trajet.")
                    continue
                resume = resumer_itineraire(plage, libelle, itineraire)
                resultats.append(resume)
                if mode_api == "TRANSIT":
                    itineraire_tc = itineraire
                    resume_tc = resume
            except (requests.RequestException, RuntimeError) as erreur:
                avertissements.append(f"{plage} — {libelle} : {erreur}.")

        if itineraire_tc and resume_tc:
            etapes_tc = [
                etape
                for etape in itineraire_tc["legs"][0].get("steps", [])
                if "transitDetails" in etape
            ]
            statut, raison = autorisation_velo_tc(etapes_tc, depart)
            resume_tc["velo_tc_statut"] = statut
            resume_tc["velo_tc_raison"] = raison
            try:
                mixte = creer_itineraire_mixte(
                    cle_api,
                    plage,
                    origine,
                    destination,
                    depart,
                    itineraire_tc,
                    statut,
                    raison,
                )
                if mixte:
                    resultats.append(mixte)
                elif statut == "Non autorisé":
                    avertissements.append(
                        f"{plage} — Vélo + TC : {raison}."
                    )
            except (requests.RequestException, RuntimeError) as erreur:
                avertissements.append(f"{plage} — Vélo + TC : {erreur}.")

    return resultats, avertissements


def creer_carte_html(
    resultats: list[dict],
    modes_affiches: list[str],
    cle_navigateur: str,
) -> str:
    """Prépare la carte Google avec le panneau de détails unifié."""
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
    cle_url = quote_plus(cle_navigateur)

    return f"""
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
    z-index: 5;
  }}
  #panneau-details[hidden] {{ display: none; }}
  .panneau-fermer {{
    align-items: center;
    background: #fff;
    border: 1px solid #cbd5e1;
    border-radius: 50%;
    color: #1f2937;
    cursor: pointer;
    display: flex;
    float: right;
    font-size: 20px;
    height: 30px;
    justify-content: center;
    position: sticky;
    right: 0;
    top: 0;
    width: 30px;
    z-index: 2;
  }}
  .panneau-titre {{ margin: 2px 38px 10px 0; }}
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
  .acces-contenu[hidden] {{ display: none; }}
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
<script>
  const trajets = {donnees_json};
  const plages = {plages_json};
  const origine = {origine_json};
  function initMap() {{
    const panneau = document.getElementById("panneau-details");
    const carte = new google.maps.Map(document.getElementById("carte"), {{
      center: {{lat: 45.55, lng: -73.55}},
      zoom: 10,
      mapTypeControl: false,
      streetViewControl: false,
      fullscreenControl: true
    }});
    const limites = new google.maps.LatLngBounds();

    const marqueurDepart = new google.maps.Marker({{
      map: carte,
      position: origine,
      title: "Point de départ",
      label: {{text: "D", color: "#ffffff", fontWeight: "bold"}},
      icon: {{
        path: google.maps.SymbolPath.CIRCLE,
        fillColor: "{COULEUR_DEPART}",
        fillOpacity: 1,
        strokeColor: "#ffffff",
        strokeWeight: 2,
        scale: 12
      }}
    }});
    marqueurDepart.addListener("click", () => ouvrirPanneau(
      '<h3 class="panneau-titre">Point de départ</h3>'
    ));
    limites.extend(origine);

    function ouvrirPanneau(contenu) {{
      panneau.innerHTML = `
        <button type="button" class="panneau-fermer"
          data-action="fermer-panneau" aria-label="Fermer les détails">×</button>
        ${{contenu}}
      `;
      panneau.hidden = false;
      panneau.scrollTop = 0;
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
      if (mode === "REGIONAL_RAIL") return "🚆";
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
      const marqueur = new google.maps.Marker({{
        map: carte,
        position: plage.position,
        label: {{
          text: String(plage.numero),
          color: "#ffffff",
          fontWeight: "bold"
        }},
        title: plage.nom,
        icon: {{
          path: google.maps.SymbolPath.CIRCLE,
          fillColor: "#475569",
          fillOpacity: 1,
          strokeColor: "#ffffff",
          strokeWeight: 2,
          scale: 14
        }}
      }});
      marqueur.addListener("click", () => ouvrirPanneau(
        `<h3 class="panneau-titre">${{
          plage.numero
        }}. ${{plage.nom}}</h3>${{contenu}}`
      ));
      limites.extend(plage.position);
    }});

    trajets.forEach((trajet) => {{
      const options = {{
        path: trajet.trace,
        geodesic: true,
        strokeColor: trajet.couleur,
        strokeOpacity: trajet.mode === "Vélo" ? 0.8 : 0,
        strokeWeight: trajet.mode === "Vélo" ? 4 : 3,
        map: carte
      }};
      if (trajet.mode !== "Vélo") {{
        options.icons = [{{
          icon: {{
            path: "M 0,-1 0,1",
            strokeColor: trajet.couleur,
            strokeOpacity: 0.9,
            scale: 3
          }},
          offset: "0",
          repeat: trajet.mode === "Vélo + TC" ? "24px" : "16px"
        }}];
      }}
      const ligne = new google.maps.Polyline(options);
      const autorisation = trajet.mode === "Vélo + TC"
        ? `<br><small>${{trajet.autorisation}}</small>`
        : "";
      ligne.addListener("click", () => ouvrirPanneau(
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
    carte.fitBounds(limites, 35);
  }}
</script>
<script async
  src="https://maps.googleapis.com/maps/api/js?key={cle_url}&callback=initMap">
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
    """Affiche les codes de la carte avec les couleurs du graphique."""
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
  <span style="opacity:.72">
    Sélectionnez une plage ou un trajet pour voir les étapes.
  </span>
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

    cle_routes = obtenir_cle("GOOGLE_ROUTES_API_KEY")
    cle_navigateur = obtenir_cle("GOOGLE_MAPS_BROWSER_KEY")
    if not cle_routes or not cle_navigateur:
        st.error(
            "Ajoutez GOOGLE_ROUTES_API_KEY et GOOGLE_MAPS_BROWSER_KEY, "
            "puis relancez l’application."
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
                cle_routes, origine.strip(), depart.isoformat()
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
    carte_html = creer_carte_html(resultats, modes_affiches, cle_navigateur)
    components.html(carte_html, height=540)
    st.caption(
        "Calcul des trajets et fond de carte : Google Maps Platform."
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
        "Google Maps au moment du calcul. Le mode vélo + TC remplace les accès "
        "à pied avant et après le TC par des segments à vélo; sa durée est une "
        "estimation. Les règles concernent un vélo standard non électrique et "
        "la place n’est jamais garantie. Vérifiez le trajet avant de partir. "
        "[Source des cinq plages : CMM]"
        "(https://cmm.qc.ca/nouvelles/top-5-des-plus-belles-plages-du-grand-montreal/)."
    )


if __name__ == "__main__":
    main()
