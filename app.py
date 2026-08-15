"""
Application d'Analyse Statistiques Tennis (ATP / WTA via ESPN)
===================================================================
Même esprit que MLB / NPB / KBO / NHL, avec 2 onglets uniquement :
  - Résumé du jour (tous matchs, tous championnats)
  - Hot Pronostics (matchs à venir Winamax ; retirés dès qu'ils quittent le board)

Sources :
  - Scoreboard ESPN (`site.web.api` / repli `site.api`) — calendrier / scores live
  - Classements ESPN ATP & WTA
  - The-Odds-API (tournois tennis actifs, ex. tennis_atp_cincinnati_open) :
      Winamax h2h pour Hot Pronostics + repli Résumé si ESPN vide
"""

from __future__ import annotations

import importlib.util as _importlib_util
import json
import math
import os
import re
import sys as _sys
import time
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path as _Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import streamlit as st

# ---------------------------------------------------------------------------
# Design system partagé
# ---------------------------------------------------------------------------
_THEME_PATH = next(
    (
        p
        for p in (
            _Path(__file__).resolve().parent / "shared" / "theme.py",
            _Path(__file__).resolve().parent.parent / "shared" / "theme.py",
        )
        if p.is_file()
    ),
    None,
)
if _THEME_PATH is None:
    raise ImportError("shared/theme.py introuvable à côté de l'app Tennis.")
_spec = _importlib_util.spec_from_file_location("ps_shared_theme", _THEME_PATH)
_ps_theme = _importlib_util.module_from_spec(_spec)
_sys.modules["ps_shared_theme"] = _ps_theme
_spec.loader.exec_module(_ps_theme)
apply_theme = _ps_theme.apply_theme
render_page_header = _ps_theme.render_page_header
render_section_title = _ps_theme.render_section_title
afficher_tableau_recap_hot_pronostics_tennis = _ps_theme.afficher_tableau_recap_hot_pronostics_tennis
afficher_assistant_hot_pronostics_tennis = _ps_theme.afficher_assistant_hot_pronostics_tennis
render_footer = _ps_theme.render_footer

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------
# ESPN tennis : hosts essayés dans l'ordre (Cloud bloque parfois l'un ou l'autre).
ESPN_API_BASES = (
    "https://site.web.api.espn.com/apis/site/v2/sports/tennis",
    "https://site.api.espn.com/apis/site/v2/sports/tennis",
)
ESPN_API = ESPN_API_BASES[0]
TZ_PARIS = ZoneInfo("Europe/Paris")
ANNEE_COURANTE = datetime.now(TZ_PARIS).year

# Ligues ESPN à agréger (dédupe ensuite). "all" + tours.
LIGUES_ESPN = ("all", "atp", "wta")

# Instantanés Hot Pronostics figés dès le début du match (fichier local).
NOM_FICHIER_HISTORIQUE = "historique_predictions_tennis.json"
CHEMIN_HISTORIQUE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), NOM_FICHIER_HISTORIQUE
)

_SESSION = requests.Session()
_SESSION.headers.update({
    # UA navigateur : certains CDN ESPN renvoient un scoreboard vide / 403 sinon.
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
    ),
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "en-US,en;q=0.9,fr;q=0.8",
    "Referer": "https://www.espn.com/tennis/scoreboard",
    "Origin": "https://www.espn.com",
})


def _match_a_commence_tennis(match_ou_statut) -> bool:
    """True dès que le match n'est plus en pré-match (Scheduled / Delayed Start…)."""
    if isinstance(match_ou_statut, dict):
        state = (match_ou_statut.get("state") or "").strip().lower()
        statut = (match_ou_statut.get("statut") or "").strip().lower()
    else:
        state, statut = "", (match_ou_statut or "").strip().lower()
    if state in ("in", "post"):
        return True
    if state == "pre":
        return False
    if any(x in statut for x in (
        "scheduled", "pre-game", "preview", "warmup", "delayed start",
        "postponed", "cancelled", "canceled", "à venir",
    )):
        return False
    if any(x in statut for x in (
        "in progress", "live", "final", "game over", "retired", "walkover",
        "completed", "en cours", "suspend",
    )):
        return True
    return False


def _match_annule_ou_reporte_tennis(m: dict) -> bool:
    statut = (m.get("statut") or "").strip().lower()
    return any(x in statut for x in (
        "cancel", "postpon", "abandoned", "walkover", "retired before",
    ))


def _match_est_a_venir_tennis(m: dict) -> bool:
    """Hot Pronostics : uniquement les matchs pas encore commencés."""
    if not m or m.get("termine") or _match_a_commence_tennis(m):
        return False
    if _match_annule_ou_reporte_tennis(m):
        return False
    return True


def _cle_snapshot_tennis(m: dict) -> str:
    """Clé stable joueurs + date (indépendante ESPN / Winamax)."""
    j1 = _normaliser_nom(m.get("joueur1") or "")
    j2 = _normaliser_nom(m.get("joueur2") or "")
    paire = "|".join(sorted([j1, j2]))
    return f"p:{paire}|{m.get('date_paris') or ''}"


def _charger_historique_predictions_tennis() -> dict:
    try:
        with open(CHEMIN_HISTORIQUE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _index_snapshots_tennis(matches: list) -> dict:
    out = {}
    for m in matches or []:
        if isinstance(m, dict):
            out[_cle_snapshot_tennis(m)] = m
    return out


def _fusionner_snapshots_figes_tennis(existants: list, nouveaux: list, maintenant_iso: str) -> list:
    """
    Conserve la dernière prédiction pré-match dès que le match a commencé.
    Les matchs encore à venir restent rafraîchis.
    """
    index_old = _index_snapshots_tennis(existants)
    merges, vus = [], set()
    for m in nouveaux or []:
        if not isinstance(m, dict):
            continue
        cle = _cle_snapshot_tennis(m)
        vus.add(cle)
        old = index_old.get(cle)
        a_commence = bool(m.get("a_commence")) or _match_a_commence_tennis(m)
        if old and old.get("fige"):
            frozen = dict(old)
            frozen["statut"] = m.get("statut") or frozen.get("statut")
            frozen["state"] = m.get("state") or frozen.get("state")
            merges.append(frozen)
        elif old and a_commence:
            frozen = dict(old)
            frozen["fige"] = True
            frozen["fige_le"] = old.get("fige_le") or maintenant_iso
            frozen["statut"] = m.get("statut") or old.get("statut")
            frozen["state"] = m.get("state") or old.get("state")
            merges.append(frozen)
        else:
            new_m = dict(m)
            if a_commence:
                new_m["fige"] = True
                new_m["fige_le"] = maintenant_iso
            else:
                new_m["fige"] = False
            merges.append(new_m)
    for cle, old in index_old.items():
        if cle not in vus and old.get("fige"):
            merges.append(old)
    return merges


def _sauvegarder_predictions_tennis(date_str: str, matches_snapshot: list) -> None:
    """Archive locale des Hot Pronostics (figés au coup d'envoi). Ne lève jamais."""
    try:
        historique = _charger_historique_predictions_tennis()
        maintenant_iso = datetime.now(TZ_PARIS).isoformat()
        existants = (historique.get(date_str) or {}).get("matches") or []
        merges = _fusionner_snapshots_figes_tennis(existants, matches_snapshot, maintenant_iso)
        historique[date_str] = {
            "sauvegarde_le": maintenant_iso,
            "matches": merges,
        }
        date_limite = (datetime.now(TZ_PARIS) - timedelta(days=30)).strftime("%Y-%m-%d")
        historique = {d: v for d, v in historique.items() if d >= date_limite}
        with open(CHEMIN_HISTORIQUE, "w", encoding="utf-8") as f:
            json.dump(historique, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _archives_tennis_journee(date_ref: str) -> dict:
    """Index match_id → snapshot pour aujourd'hui + hier (session US)."""
    historique = _charger_historique_predictions_tennis()
    index = {}
    try:
        hier = (datetime.strptime(date_ref, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    except ValueError:
        hier = (datetime.now(TZ_PARIS) - timedelta(days=1)).strftime("%Y-%m-%d")
    for d in (hier, date_ref):
        index.update(_index_snapshots_tennis((historique.get(d) or {}).get("matches") or []))
    return index


def appeler_avec_retry(fonction, *args, tentatives: int = 3, delai_base: float = 0.5, **kwargs):
    derniere = None
    for tentative in range(1, tentatives + 1):
        try:
            return fonction(*args, **kwargs)
        except Exception as exc:
            derniere = exc
            if tentative < tentatives:
                time.sleep(delai_base * (2 ** (tentative - 1)))
    raise derniere


def _get_json(url: str, params: dict | None = None, timeout: int = 25):
    reponse = _SESSION.get(url, params=params or {}, timeout=timeout)
    reponse.raise_for_status()
    return reponse.json()


def _normaliser_nom(texte: str) -> str:
    brut = unicodedata.normalize("NFKD", texte or "")
    brut = "".join(c for c in brut if not unicodedata.combining(c))
    brut = re.sub(r"[^a-z0-9\s]", " ", brut.lower())
    return " ".join(brut.split())


def _parser_float(valeur, defaut=None):
    try:
        if valeur is None or (isinstance(valeur, float) and pd.isna(valeur)):
            return defaut
        return float(valeur)
    except (TypeError, ValueError):
        return defaut


def _obtenir_cle_odds_api() -> str | None:
    try:
        conf = st.secrets.get("odds_api", {})
        return conf.get("api_key")
    except Exception:
        return None


# ============================================================
# Données ESPN — scoreboard & classements
# ============================================================
def _espn_get_json(path: str, params: dict | None = None) -> dict:
    """GET JSON sur le premier host ESPN qui répond correctement."""
    derniere = None
    for base in ESPN_API_BASES:
        url = f"{base}/{path.lstrip('/')}"
        try:
            return appeler_avec_retry(_get_json, url, params=params)
        except Exception as exc:
            derniere = exc
            continue
    if derniere:
        raise derniere
    return {}


@st.cache_data(show_spinner=False, ttl=180)
def _charger_scoreboard_espn(ligue: str, dates: str | None = None) -> dict:
    params = {"dates": dates, "lang": "en", "region": "us"} if dates else {
        "lang": "en", "region": "us",
    }
    try:
        data = _espn_get_json(f"{ligue}/scoreboard", params=params)
    except Exception:
        return {"events": []}
    return data if isinstance(data, dict) else {"events": []}


@st.cache_data(show_spinner=False, ttl=1800)
def _charger_classement_espn(tour: str) -> dict:
    """tour = 'atp' ou 'wta' → {nom_normalise: {rang, points, nom}}."""
    try:
        data = _espn_get_json(f"{tour}/rankings", params={"lang": "en", "region": "us"})
    except Exception:
        return {}
    ranking = (data.get("rankings") or [{}])[0]
    out = {}
    for item in ranking.get("ranks") or []:
        ath = item.get("athlete") or {}
        nom = ath.get("displayName") or ""
        if not nom:
            continue
        out[_normaliser_nom(nom)] = {
            "rang": int(item.get("current") or 0) or None,
            "points": _parser_float(item.get("points"), 0.0) or 0.0,
            "nom": nom,
            "id": ath.get("id"),
            "tour": tour.upper(),
        }
    return out


def _sets_gagnes(competitor: dict) -> int:
    return sum(1 for ls in (competitor.get("linescores") or []) if ls.get("winner"))


def _score_sets_texte(p1: dict, p2: dict) -> str:
    ls1 = p1.get("linescores") or []
    ls2 = p2.get("linescores") or []
    n = max(len(ls1), len(ls2))
    if n == 0:
        return "—"
    parties = []
    for i in range(n):
        a = ls1[i] if i < len(ls1) else {}
        b = ls2[i] if i < len(ls2) else {}
        va = int(_parser_float(a.get("value"), 0) or 0)
        vb = int(_parser_float(b.get("value"), 0) or 0)
        ta = a.get("tiebreak")
        tb = b.get("tiebreak")
        if ta is not None or tb is not None:
            parties.append(f"{va}-{vb} ({int(ta or 0)}-{int(tb or 0)})")
        else:
            parties.append(f"{va}-{vb}")
    return " ".join(parties)


def _extraire_matchs_depuis_scoreboard(data: dict, ligue_slug: str) -> list[dict]:
    matchs = []
    for event in data.get("events") or []:
        tournoi = event.get("name") or event.get("shortName") or "Tournoi"
        for grouping in event.get("groupings") or []:
            ginfo = grouping.get("grouping") or {}
            tableau = ginfo.get("displayName") or ginfo.get("slug") or ""
            slug_tab = (ginfo.get("slug") or "").lower()
            est_simple = "singles" in slug_tab
            est_double = "doubles" in slug_tab
            for comp in grouping.get("competitions") or []:
                competitors = sorted(
                    comp.get("competitors") or [],
                    key=lambda x: x.get("order") or 0,
                )
                if len(competitors) < 2:
                    continue
                c1, c2 = competitors[0], competitors[1]

                def _nom_competiteur(comp_side: dict) -> str:
                    """Singles : athlete.displayName. Doubles ESPN : roster.displayName."""
                    ath = comp_side.get("athlete") or {}
                    roster = comp_side.get("roster") or {}
                    for candidat in (
                        ath.get("displayName"),
                        ath.get("fullName"),
                        roster.get("displayName"),
                        roster.get("shortDisplayName"),
                        comp_side.get("displayName"),
                        comp_side.get("name"),
                        comp_side.get("abbreviation"),
                    ):
                        if candidat and str(candidat).strip() and str(candidat).strip() != "?":
                            return str(candidat).strip()
                    # Repli : concatène les athlètes du roster (doubles)
                    athletes = roster.get("athletes") or []
                    noms = [
                        (a.get("displayName") or a.get("shortName") or "").strip()
                        for a in athletes
                        if (a.get("displayName") or a.get("shortName"))
                    ]
                    if noms:
                        return " / ".join(noms)
                    return "?"

                nom1 = _nom_competiteur(c1)
                nom2 = _nom_competiteur(c2)
                statut = ((comp.get("status") or {}).get("type") or {})
                date_iso = comp.get("date") or comp.get("startDate") or event.get("date")
                heure_paris = ""
                date_paris = ""
                if date_iso:
                    try:
                        dt = datetime.fromisoformat(date_iso.replace("Z", "+00:00")).astimezone(TZ_PARIS)
                        heure_paris = dt.strftime("%H:%M")
                        date_paris = dt.strftime("%Y-%m-%d")
                    except Exception:
                        pass
                # ESPN expose souvent periods=5 même en best-of-3 (Masters 1000).
                # On force Bo3 hors Grands Chelems / Coupe Davis-like.
                tournoi_l = (tournoi or "").lower()
                est_grand_chelem = any(
                    x in tournoi_l for x in (
                        "australian open", "roland garros", "french open",
                        "wimbledon", "us open",
                    )
                )
                periods = ((comp.get("format") or {}).get("regulation") or {}).get("periods")
                if est_grand_chelem and "mens-singles" in slug_tab:
                    best_of = 5
                elif periods in (3, 5) and est_grand_chelem:
                    best_of = int(periods)
                else:
                    best_of = 3
                note = ""
                if comp.get("notes"):
                    note = (comp["notes"][0] or {}).get("text") or ""
                matchs.append({
                    "match_id": str(comp.get("id") or f"{nom1}-{nom2}-{date_iso}"),
                    "ligue": ligue_slug.upper(),
                    "tournoi": tournoi,
                    "tableau": tableau,
                    "slug_tableau": slug_tab,
                    "est_simple": est_simple,
                    "est_double": est_double,
                    "date_iso": date_iso,
                    "date_paris": date_paris,
                    "heure_paris": heure_paris,
                    "statut": statut.get("description") or "—",
                    "state": (statut.get("state") or "").lower(),
                    "termine": bool(statut.get("completed")),
                    "joueur1": nom1,
                    "joueur2": nom2,
                    "joueur1_id": (c1.get("athlete") or {}).get("id") or c1.get("id"),
                    "joueur2_id": (c2.get("athlete") or {}).get("id") or c2.get("id"),
                    "joueur1_winner": bool(c1.get("winner")),
                    "joueur2_winner": bool(c2.get("winner")),
                    "joueur1_sets": _sets_gagnes(c1),
                    "joueur2_sets": _sets_gagnes(c2),
                    "score": _score_sets_texte(c1, c2),
                    "best_of": best_of,
                    "court": (comp.get("venue") or {}).get("court") or "",
                    "note": note,
                })
    return matchs


@st.cache_data(show_spinner=False, ttl=180)
def obtenir_matchs_tennis_du_jour(cache_bust: int = 0, _cache_version: int = 7) -> tuple[list[dict], str]:
    """
    Agrège les matchs du jour calendaire Paris uniquement : de 00:00 à 23:59
    (heure de Paris), tous championnats ESPN. Aucun match de la veille.
    """
    del cache_bust, _cache_version
    maintenant = datetime.now(TZ_PARIS)
    aujourdhui = maintenant.strftime("%Y-%m-%d")
    debut_jour = maintenant.replace(hour=0, minute=0, second=0, microsecond=0)
    fin_jour = maintenant.replace(hour=23, minute=59, second=59, microsecond=999999)
    vus = set()
    matchs = []
    d0 = maintenant.strftime("%Y%m%d")
    for ligue in LIGUES_ESPN:
        for dates_param in (None, d0):
            try:
                data = _charger_scoreboard_espn(ligue, dates_param)
            except Exception:
                continue
            for m in _extraire_matchs_depuis_scoreboard(data, ligue):
                # Fenêtre stricte : minuit → 23:59 heure de Paris le jour même
                date_iso = m.get("date_iso") or ""
                dt_paris = None
                if date_iso:
                    try:
                        dt_paris = datetime.fromisoformat(
                            date_iso.replace("Z", "+00:00")
                        ).astimezone(TZ_PARIS)
                    except Exception:
                        dt_paris = None
                if dt_paris is not None:
                    if not (debut_jour <= dt_paris <= fin_jour):
                        continue
                elif (m.get("date_paris") or "") != aujourdhui:
                    continue
                cle = (m["match_id"], m["joueur1"], m["joueur2"])
                if cle in vus:
                    continue
                vus.add(cle)
                matchs.append(m)

    # Ordre : live → à venir → terminés, puis heure
    ordre_state = {"in": 0, "pre": 1, "post": 2}
    matchs.sort(key=lambda m: (
        ordre_state.get(m.get("state"), 9),
        m.get("heure_paris") or "99:99",
        m.get("tournoi") or "",
    ))
    return matchs, aujourdhui


@st.cache_data(show_spinner=False, ttl=180)
def obtenir_matchs_tennis_fenetre(
    cache_bust: int = 0,
    jours_ahead: int = 7,
    _cache_version: int = 4,
) -> tuple[list[dict], str]:
    """
    Matchs ESPN sur la fenêtre aujourd'hui (Paris) → +jours_ahead
    (tous états : pre / live / post), tous championnats.
    """
    del cache_bust, _cache_version
    maintenant = datetime.now(TZ_PARIS)
    aujourdhui = maintenant.strftime("%Y-%m-%d")
    fin = (maintenant + timedelta(days=max(0, int(jours_ahead)))).strftime("%Y-%m-%d")
    d0 = maintenant.strftime("%Y%m%d")
    d1 = (maintenant + timedelta(days=max(0, int(jours_ahead)))).strftime("%Y%m%d")
    vus = set()
    matchs = []
    for ligue in LIGUES_ESPN:
        for dates_param in (None, f"{d0}-{d1}", d0):
            try:
                data = _charger_scoreboard_espn(ligue, dates_param)
            except Exception:
                continue
            for m in _extraire_matchs_depuis_scoreboard(data, ligue):
                date_paris = m.get("date_paris") or ""
                if date_paris and (date_paris < aujourdhui or date_paris > fin):
                    continue
                cle = (m["match_id"], m["joueur1"], m["joueur2"])
                if cle in vus:
                    continue
                vus.add(cle)
                matchs.append(m)

    matchs.sort(key=lambda m: (
        m.get("date_paris") or "9999-99-99",
        m.get("heure_paris") or "99:99",
        m.get("tournoi") or "",
        m.get("joueur1") or "",
    ))
    return matchs, aujourdhui


def obtenir_matchs_tennis_a_venir(cache_bust: int = 0) -> tuple[list[dict], str]:
    """Tous les matchs encore à venir dans la fenêtre Hot Pronostics."""
    matchs, date_ref = obtenir_matchs_tennis_fenetre(cache_bust)
    a_venir = [m for m in matchs if _match_est_a_venir_tennis(m)]
    return a_venir, date_ref


@st.cache_data(show_spinner=False, ttl=1800)
def obtenir_classements_tennis() -> dict:
    """Fusion ATP + WTA indexée par nom normalisé."""
    out = {}
    out.update(_charger_classement_espn("atp"))
    out.update(_charger_classement_espn("wta"))
    return out


# ============================================================
# Cotes / matchs Winamax (The-Odds-API)
# ============================================================
# Même clé bookmaker que MLB / NPB / KBO.
BOOKMAKERS_WINAMAX = ("winamax_fr", "winamax")
ODDS_API_BASE = "https://api.the-odds-api.com/v4"


def _choisir_bookmaker_winamax(bookmakers: list) -> dict | None:
    """Retourne le bookmaker Winamax s'il est présent, sinon None."""
    if not bookmakers:
        return None
    by_key = {(b.get("key") or "").lower(): b for b in bookmakers}
    for cle in BOOKMAKERS_WINAMAX:
        if cle in by_key:
            return by_key[cle]
    return None


def _extraire_cotes_h2h(bookmaker: dict) -> dict:
    """{nom_joueur: cote_decimale} depuis un bookmaker Odds-API."""
    cotes_raw = {}
    for market in bookmaker.get("markets") or []:
        if market.get("key") != "h2h":
            continue
        for outcome in market.get("outcomes") or []:
            nom = outcome.get("name")
            prix = _parser_float(outcome.get("price"))
            if nom and prix and prix > 1:
                cotes_raw[nom] = prix
    return cotes_raw


@st.cache_data(show_spinner=False, ttl=600)
def _lister_sports_tennis_odds_api(api_key: str) -> list[dict]:
    """
    Sports tennis actifs côté The-Odds-API.
    Les clés sont désormais par tournoi (ex. tennis_atp_cincinnati_open),
    plus un sport générique unique.
    """
    if not api_key:
        return []
    try:
        sports = appeler_avec_retry(
            _get_json,
            f"{ODDS_API_BASE}/sports",
            {"apiKey": api_key},
        )
    except Exception:
        return []
    out = []
    for s in sports or []:
        key = str(s.get("key") or "")
        group = str(s.get("group") or "").strip().lower()
        if not key.startswith("tennis_") and group != "tennis":
            continue
        # Prefer active; keep inactive only if title looks like current tour events
        if s.get("active"):
            out.append(s)
    # Si rien d'actif (entre deux tournois) : retenter sans filtre active
    if not out:
        out = [
            s for s in (sports or [])
            if str(s.get("key") or "").startswith("tennis_")
            or str(s.get("group") or "").strip().lower() == "tennis"
        ]
    out.sort(key=lambda s: (s.get("key") or ""))
    return out


def _fetch_odds_events_tennis(sport_key: str, api_key: str) -> list[dict]:
    """
    Événements h2h pour un sport tennis.
    1) filtre bookmakers Winamax côté API
    2) repli : tous bookmakers EU, puis filtre Winamax en local
    """
    params_base = {
        "apiKey": api_key,
        "regions": "eu",
        "markets": "h2h",
        "oddsFormat": "decimal",
    }
    try:
        events = appeler_avec_retry(
            _get_json,
            f"{ODDS_API_BASE}/sports/{sport_key}/odds",
            {**params_base, "bookmakers": ",".join(BOOKMAKERS_WINAMAX)},
        )
    except Exception:
        events = []
    if events:
        return events or []
    try:
        events = appeler_avec_retry(
            _get_json,
            f"{ODDS_API_BASE}/sports/{sport_key}/odds",
            params_base,
        )
    except Exception:
        return []
    return events or []


@st.cache_data(show_spinner=False, ttl=180)
def obtenir_matchs_tennis_odds_du_jour(
    api_key: str | None,
    cache_bust: int = 0,
    _cache_version: int = 1,
) -> list[dict]:
    """
    Repli Résumé : scores + cotes The-Odds-API pour la journée Paris.
    Couvre les tournois dont la clé sport est active (ATP/WTA Open, etc.).
    """
    del cache_bust, _cache_version
    if not api_key:
        return []
    aujourdhui = datetime.now(TZ_PARIS).strftime("%Y-%m-%d")
    sports = _lister_sports_tennis_odds_api(api_key)
    matchs = []
    vus = set()

    for sport in sports:
        sport_key = sport.get("key") or ""
        tournoi = sport.get("title") or sport.get("description") or sport_key
        ligue = "ATP" if "atp" in sport_key else ("WTA" if "wta" in sport_key else "TENNIS")

        # Scores (terminés / live récents)
        try:
            scores = appeler_avec_retry(
                _get_json,
                f"{ODDS_API_BASE}/sports/{sport_key}/scores",
                {"apiKey": api_key, "daysFrom": 1},
            )
        except Exception:
            scores = []
        for ev in scores or []:
            home = (ev.get("home_team") or "").strip()
            away = (ev.get("away_team") or "").strip()
            if not home or not away:
                continue
            commence = ev.get("commence_time") or ""
            date_paris, heure_paris = "", ""
            if commence:
                try:
                    dt = datetime.fromisoformat(commence.replace("Z", "+00:00")).astimezone(TZ_PARIS)
                    date_paris = dt.strftime("%Y-%m-%d")
                    heure_paris = dt.strftime("%H:%M")
                except Exception:
                    pass
            if date_paris != aujourdhui:
                continue
            completed = bool(ev.get("completed"))
            score_map = {
                (s.get("name") or ""): s.get("score")
                for s in (ev.get("scores") or [])
                if s.get("name")
            }
            try:
                s1 = int(float(score_map[home])) if score_map.get(home) is not None else 0
                s2 = int(float(score_map[away])) if score_map.get(away) is not None else 0
            except (TypeError, ValueError):
                s1 = s2 = 0
            mid = str(ev.get("id") or f"odds-score-{sport_key}-{home}-{away}-{date_paris}")
            cle = (_normaliser_nom(home), _normaliser_nom(away), date_paris)
            if cle in vus:
                continue
            vus.add(cle)
            matchs.append({
                "match_id": mid,
                "ligue": ligue,
                "tournoi": tournoi,
                "tableau": "Singles",
                "slug_tableau": "singles",
                "est_simple": True,
                "est_double": False,
                "date_iso": commence,
                "date_paris": date_paris,
                "heure_paris": heure_paris,
                "statut": "Final" if completed else "En cours / programmé",
                "state": "post" if completed else ("in" if score_map else "pre"),
                "termine": completed,
                "joueur1": home,
                "joueur2": away,
                "joueur1_id": None,
                "joueur2_id": None,
                "joueur1_winner": completed and s1 > s2,
                "joueur2_winner": completed and s2 > s1,
                "joueur1_sets": s1,
                "joueur2_sets": s2,
                "score": f"{s1}-{s2}" if completed or score_map else "—",
                "best_of": 3,
                "court": "",
                "note": "",
                "source": "odds_api",
            })

        # À venir via odds (complète les matchs pas encore dans scores)
        for ev in _fetch_odds_events_tennis(sport_key, api_key):
            home = (ev.get("home_team") or "").strip()
            away = (ev.get("away_team") or "").strip()
            if not home or not away:
                continue
            commence = ev.get("commence_time") or ""
            date_paris, heure_paris = "", ""
            if commence:
                try:
                    dt = datetime.fromisoformat(commence.replace("Z", "+00:00")).astimezone(TZ_PARIS)
                    date_paris = dt.strftime("%Y-%m-%d")
                    heure_paris = dt.strftime("%H:%M")
                except Exception:
                    pass
            if date_paris != aujourdhui:
                continue
            cle = (_normaliser_nom(home), _normaliser_nom(away), date_paris)
            if cle in vus:
                continue
            vus.add(cle)
            mid = str(ev.get("id") or f"odds-{sport_key}-{home}-{away}-{commence}")
            matchs.append({
                "match_id": mid,
                "ligue": ligue,
                "tournoi": tournoi,
                "tableau": "Singles",
                "slug_tableau": "singles",
                "est_simple": True,
                "est_double": False,
                "date_iso": commence,
                "date_paris": date_paris,
                "heure_paris": heure_paris,
                "statut": "À venir",
                "state": "pre",
                "termine": False,
                "joueur1": home,
                "joueur2": away,
                "joueur1_id": None,
                "joueur2_id": None,
                "joueur1_winner": False,
                "joueur2_winner": False,
                "joueur1_sets": 0,
                "joueur2_sets": 0,
                "score": "—",
                "best_of": 3,
                "court": "",
                "note": "",
                "source": "odds_api",
            })

    ordre_state = {"in": 0, "pre": 1, "post": 2}
    matchs.sort(key=lambda m: (
        ordre_state.get(m.get("state"), 9),
        m.get("heure_paris") or "99:99",
        m.get("tournoi") or "",
    ))
    return matchs


@st.cache_data(show_spinner=False, ttl=600)
def obtenir_matchs_tennis_winamax(
    api_key: str | None,
    cache_bust: int = 0,
    _cache_version: int = 3,
) -> tuple[list[dict], dict, str]:
    """
    Source Hot Pronostics : matchs à venir proposés par Winamax (The-Odds-API).

    Retourne (matchs, index_cotes, date_ref_paris).
    Un match n'apparaît que s'il a un marché h2h Winamax.
    """
    del cache_bust, _cache_version
    date_ref = datetime.now(TZ_PARIS).strftime("%Y-%m-%d")
    if not api_key:
        return [], {}, date_ref

    sports = _lister_sports_tennis_odds_api(api_key)
    matchs = []
    index_cotes = {}
    vus = set()

    for sport in sports:
        sport_key = sport.get("key") or ""
        tournoi = sport.get("title") or sport.get("description") or sport_key
        events = _fetch_odds_events_tennis(sport_key, api_key)

        for ev in events or []:
            home = (ev.get("home_team") or "").strip()
            away = (ev.get("away_team") or "").strip()
            if not home or not away:
                continue
            choisi = _choisir_bookmaker_winamax(ev.get("bookmakers") or [])
            if not choisi:
                continue
            cotes_raw = _extraire_cotes_h2h(choisi)
            if len(cotes_raw) < 2:
                continue

            commence = ev.get("commence_time") or ""
            heure_paris = ""
            date_paris = ""
            if commence:
                try:
                    dt_paris = datetime.fromisoformat(
                        commence.replace("Z", "+00:00")
                    ).astimezone(TZ_PARIS)
                    heure_paris = dt_paris.strftime("%H:%M")
                    date_paris = dt_paris.strftime("%Y-%m-%d")
                except Exception:
                    pass

            match_id = str(ev.get("id") or f"wina-{sport_key}-{home}-{away}-{commence}")
            cle_dup = (_normaliser_nom(home), _normaliser_nom(away), date_paris or commence)
            if cle_dup in vus:
                continue
            vus.add(cle_dup)

            book_title = choisi.get("title") or "Winamax"
            cle_ab = tuple(sorted([_normaliser_nom(home), _normaliser_nom(away)]))
            entry = {_normaliser_nom(n): c for n, c in cotes_raw.items()}
            entry["_bookmaker"] = book_title
            entry["_raw"] = cotes_raw
            index_cotes[cle_ab] = entry

            # Best-of : Grands Chelems hommes → 5, sinon 3
            tournoi_l = (tournoi or "").lower()
            est_gs = any(x in tournoi_l for x in (
                "australian open", "roland garros", "french open", "wimbledon", "us open",
            ))
            est_double = "/" in home or "/" in away or "doubles" in tournoi_l
            best_of = 5 if est_gs and not est_double and "wta" not in (sport_key or "") else 3

            matchs.append({
                "match_id": match_id,
                "ligue": "ATP" if "atp" in sport_key else ("WTA" if "wta" in sport_key else "TENNIS"),
                "tournoi": tournoi,
                "tableau": "Doubles" if est_double else "Singles",
                "slug_tableau": "doubles" if est_double else "singles",
                "est_simple": not est_double,
                "est_double": est_double,
                "date_iso": commence,
                "date_paris": date_paris,
                "heure_paris": heure_paris,
                "statut": "À venir (Winamax)",
                "state": "pre",
                "termine": False,
                "joueur1": home,
                "joueur2": away,
                "joueur1_id": None,
                "joueur2_id": None,
                "joueur1_winner": False,
                "joueur2_winner": False,
                "joueur1_sets": 0,
                "joueur2_sets": 0,
                "score": "—",
                "best_of": best_of,
                "court": "",
                "note": "",
                "source": "winamax",
                "sport_key": sport_key,
                "cote1": cotes_raw.get(home) or entry.get(_normaliser_nom(home)),
                "cote2": cotes_raw.get(away) or entry.get(_normaliser_nom(away)),
                "bookmaker": book_title,
            })

    matchs.sort(key=lambda m: (
        m.get("date_paris") or "9999-99-99",
        m.get("heure_paris") or "99:99",
        m.get("tournoi") or "",
        m.get("joueur1") or "",
    ))
    return matchs, index_cotes, date_ref


@st.cache_data(show_spinner=False, ttl=900)
def obtenir_cotes_tennis_du_jour(api_key: str | None) -> dict:
    """
    Index cotes Winamax {(nom1_norm, nom2_norm): {...}} — dérivé du même
    flux que les matchs Hot Pronostics.
    """
    _, index_cotes, _ = obtenir_matchs_tennis_winamax(api_key, 0)
    return index_cotes


def _cotes_pour_match(match: dict, index_cotes: dict) -> dict | None:
    cle = tuple(sorted([
        _normaliser_nom(match["joueur1"]),
        _normaliser_nom(match["joueur2"]),
    ]))
    hit = index_cotes.get(cle)
    if hit:
        return hit
    # Repli souple : match sur noms de famille
    j1, j2 = _normaliser_nom(match["joueur1"]), _normaliser_nom(match["joueur2"])
    for cle_idx, data in (index_cotes or {}).items():
        if not isinstance(cle_idx, tuple) or len(cle_idx) != 2:
            continue
        a, b = cle_idx
        if (j1 in a or a in j1 or j1.split()[-1] in a) and (j2 in b or b in j2 or j2.split()[-1] in b):
            return data
        if (j1 in b or b in j1 or j1.split()[-1] in b) and (j2 in a or a in j2 or j2.split()[-1] in a):
            return data
    return None


def evaluer_value_bet_tennis(proba_algo_pct, cote, nom_joueur: str, nom_bookmaker: str = "Bookmaker"):
    """
    Comme MLB : Value = Proba_Algo - Proba_Implicite(cote).
      >= +5 → value, <= -5 → evitez, sinon juste.
    """
    if not cote or cote <= 1.0 or proba_algo_pct is None:
        return None, None
    try:
        proba_algo = float(proba_algo_pct)
        cote_f = float(cote)
    except (TypeError, ValueError):
        return None, None
    proba_implicite = (1.0 / cote_f) * 100.0
    value = proba_algo - proba_implicite
    if value >= 5:
        return "value", (
            f"🟢 Value Bet : {nom_bookmaker} sous-évalue {nom_joueur} "
            f"(cote {cote_f:.2f}, value +{value:.1f}%)."
        )
    if value <= -5:
        return "evitez", (
            f"🔴 Pas de value sur {nom_joueur} : cote {nom_bookmaker} {cote_f:.2f} "
            f"trop basse (value {value:.1f}%)."
        )
    return "juste", (
        f"⚪ Cote juste sur {nom_joueur} ({nom_bookmaker} {cote_f:.2f})."
    )


def classer_value_tennis(proba_algo_pct, cote, nom_joueur: str, bookmaker: str = "Bookmaker"):
    """Retourne (value_kind, value_label) pour le tableau Hot Pronostics."""
    niveau, message = evaluer_value_bet_tennis(proba_algo_pct, cote, nom_joueur, bookmaker)
    if niveau == "value":
        return "value", "Value forte", message
    if niveau == "juste":
        return "medium", "Value moyenne", message
    if niveau == "evitez":
        return "avoid", "Pas de value", message
    return "none", "Pas de value", None


# ============================================================
# Modèles de prédiction (heuristiques tennis)
# ============================================================
def predire_probabilite_victoire_tennis(
    rang1: int | None,
    rang2: int | None,
    points1: float = 0.0,
    points2: float = 0.0,
) -> tuple[float, float]:
    """
    Probabilités de victoire joueur1 / joueur2 (somme = 100), basées sur le
    classement uniquement — indépendantes des cotes marché (comme MLB), pour
    pouvoir détecter une Value Bet proprement.
    """
    r1 = rang1 if rang1 and rang1 > 0 else 80
    r2 = rang2 if rang2 and rang2 > 0 else 80
    force1 = (1.0 / math.sqrt(r1)) + 0.00008 * max(points1, 0.0)
    force2 = (1.0 / math.sqrt(r2)) + 0.00008 * max(points2, 0.0)
    exp1 = math.exp(3.2 * force1)
    exp2 = math.exp(3.2 * force2)
    p1 = 100.0 * exp1 / (exp1 + exp2)
    p1 = max(5.0, min(95.0, p1))
    return round(p1, 1), round(100.0 - p1, 1)


def predire_les_deux_gagnent_un_set(proba_favori: float, best_of: int = 3) -> tuple[str, float]:
    """
    Estime si les deux joueurs devraient gagner au moins un set.

    Plus le match est équilibré, plus P(both win a set) est élevée.
    Best-of-5 : un peu plus élevé à écart égal (plus de sets joués).
    Retourne (OUI|NON, proba%).
    """
    ecart = abs((proba_favori or 50.0) - 50.0) / 50.0  # 0 = 50/50, 1 = 100/0
    base = 0.74 - 0.58 * ecart
    if best_of and best_of >= 5:
        base += 0.06
    proba = max(0.12, min(0.88, base)) * 100.0
    kind = "OUI" if proba >= 50.0 else "NON"
    return kind, round(proba, 1)


def _infos_joueur(nom: str, classements: dict) -> dict:
    cle = _normaliser_nom(nom)
    info = classements.get(cle)
    if info:
        return info
    # Repli : match partiel sur nom de famille
    parties = cle.split()
    if parties:
        nom_famille = parties[-1]
        candidats = [
            v for k, v in classements.items()
            if k.endswith(nom_famille) or nom_famille in k
        ]
        if len(candidats) == 1:
            return candidats[0]
    return {"rang": None, "points": 0.0, "nom": nom, "id": None, "tour": None}


# ============================================================
# Construction des vues
# ============================================================
def _calculer_prediction_match(m: dict, classements: dict, index_cotes: dict) -> dict:
    """Calcule favori / sets / value pour un match (Résumé + Hot Pronostics)."""
    i1 = _infos_joueur(m["joueur1"], classements)
    i2 = _infos_joueur(m["joueur2"], classements)
    cotes = _cotes_pour_match(m, index_cotes) or {}
    cote1 = cotes.get(_normaliser_nom(m["joueur1"]))
    cote2 = cotes.get(_normaliser_nom(m["joueur2"]))
    bookmaker = cotes.get("_bookmaker") or "Bookmaker"

    p1, p2 = predire_probabilite_victoire_tennis(
        i1.get("rang"), i2.get("rang"),
        i1.get("points") or 0.0, i2.get("points") or 0.0,
    )
    if p1 >= p2:
        favori, favori_pct = m["joueur1"], p1
        fav_rang, out_rang = i1.get("rang"), i2.get("rang")
        cote_fav = cote1
    else:
        favori, favori_pct = m["joueur2"], p2
        fav_rang, out_rang = i2.get("rang"), i1.get("rang")
        cote_fav = cote2

    sets_kind, sets_pct = predire_les_deux_gagnent_un_set(favori_pct, m.get("best_of") or 3)
    value_kind, value_label, value_msg = classer_value_tennis(
        favori_pct, cote_fav, favori, bookmaker
    )
    return {
        "proba_j1": p1,
        "proba_j2": p2,
        "favori": favori,
        "favori_pct": favori_pct,
        "sets_kind": sets_kind,
        "sets_pct": sets_pct,
        "fav_rang": fav_rang,
        "out_rang": out_rang,
        "cote1": cote1,
        "cote2": cote2,
        "cote_favori": cote_fav,
        "bookmaker": bookmaker,
        "value_kind": value_kind,
        "value_label": value_label,
        "value_msg": value_msg,
    }


def _valider_predictions_terminees(m: dict, pred: dict) -> dict:
    """
    Pour un match terminé : icônes ✅/❌ + notes sur vainqueur prédit et
    « les 2 gagnent un set ».
    """
    if m.get("joueur1_winner"):
        vainqueur = m["joueur1"]
    elif m.get("joueur2_winner"):
        vainqueur = m["joueur2"]
    else:
        vainqueur = None

    favori = pred.get("favori")
    if not vainqueur or not favori:
        return {
            "victoire_icone": "⏳",
            "victoire_note": "Résultat indisponible",
            "sets_icone": "⏳",
            "sets_note": "Résultat indisponible",
        }

    ok_victoire = _normaliser_nom(vainqueur) == _normaliser_nom(favori)
    victoire_icone = "✅" if ok_victoire else "❌"
    victoire_note = (
        f"{victoire_icone} Prédit {favori} ({pred.get('favori_pct'):.0f}%)"
        + ("" if ok_victoire else f" → réel : {vainqueur}")
    )

    both_sets_reel = (m.get("joueur1_sets") or 0) > 0 and (m.get("joueur2_sets") or 0) > 0
    sets_kind = (pred.get("sets_kind") or "").upper()
    if (m.get("joueur1_sets") or 0) + (m.get("joueur2_sets") or 0) <= 0:
        sets_icone, sets_note = "⏳", "Sets non disponibles"
    else:
        ok_sets = (sets_kind == "OUI") == both_sets_reel
        sets_icone = "✅" if ok_sets else "❌"
        reel_txt = "OUI" if both_sets_reel else "NON"
        sets_note = (
            f"{sets_icone} Prédit {sets_kind} ({pred.get('sets_pct'):.0f}%)"
            + ("" if ok_sets else f" → réel : {reel_txt}")
        )

    return {
        "victoire_icone": victoire_icone,
        "victoire_note": victoire_note,
        "sets_icone": sets_icone,
        "sets_note": sets_note,
    }


def construire_resume_tennis(cache_bust: int = 0):
    """
    Retourne (df_jour, df_termines, erreur, date_ref).
    - df_jour : tous les matchs (colonnes simplifiées)
    - df_termines : confrontations terminées + validation prédictions
    """
    try:
        matchs, date_ref = obtenir_matchs_tennis_du_jour(cache_bust)
    except Exception as exc:
        return (
            pd.DataFrame(), pd.DataFrame(),
            f"Impossible de charger les matchs ({exc}).",
            datetime.now(TZ_PARIS).strftime("%Y-%m-%d"),
        )

    # Repli The-Odds-API si ESPN ne renvoie rien (fréquent sur Streamlit Cloud)
    if not matchs:
        try:
            matchs = obtenir_matchs_tennis_odds_du_jour(_obtenir_cle_odds_api(), cache_bust)
        except Exception:
            matchs = []

    classements = obtenir_classements_tennis()
    index_cotes = obtenir_cotes_tennis_du_jour(_obtenir_cle_odds_api())
    archives = _archives_tennis_journee(date_ref)

    lignes_jour = []
    lignes_termines = []
    for m in matchs:
        if m.get("joueur1_winner"):
            vainqueur = m["joueur1"]
        elif m.get("joueur2_winner"):
            vainqueur = m["joueur2"]
        else:
            vainqueur = "—"
        sets = f"{m['joueur1_sets']}-{m['joueur2_sets']}" if m.get("state") != "pre" else "—"
        confrontation = f"{m['joueur1']} vs {m['joueur2']}"
        lignes_jour.append({
            "Heure": m.get("heure_paris") or "—",
            "Match": confrontation,
            "Statut": m.get("statut") or "—",
            "Score": m.get("score") or "—",
            "Sets": sets,
            "Vainqueur": vainqueur,
        })

        if not m.get("termine"):
            continue
        # Validation sur la prédiction FIGÉE si disponible (même source que Hot Pronostics)
        old = archives.get(_cle_snapshot_tennis(m))
        if old and old.get("favori"):
            pred = {
                "favori": old.get("favori"),
                "favori_pct": old.get("favori_pct"),
                "sets_kind": old.get("sets_kind"),
                "sets_pct": old.get("sets_pct"),
            }
        else:
            pred = _calculer_prediction_match(m, classements, index_cotes)
        valid = _valider_predictions_terminees(m, pred)
        lignes_termines.append({
            "Heure": m.get("heure_paris") or "—",
            "Match": confrontation,
            "Score": m.get("score") or "—",
            "Sets": sets,
            "Vainqueur": vainqueur,
            "Pred. victoire": valid["victoire_note"],
            "Pred. sets": valid["sets_note"],
            "_ok_victoire": valid["victoire_icone"] == "✅",
            "_ok_sets": valid["sets_icone"] == "✅",
        })

    return pd.DataFrame(lignes_jour), pd.DataFrame(lignes_termines), None, date_ref


def construire_donnees_hot_pronostics_tennis(cache_bust: int = 0):
    """
    Hot Pronostics tennis : matchs à venir Winamax uniquement.
    Dès qu'un match n'est plus proposé (démarré / retiré), il disparaît
    et la liste remonte. Les prédictions restent archivées pour le Résumé.
    """
    api_key = _obtenir_cle_odds_api()
    matchs_a_venir, index_cotes, date_ref = obtenir_matchs_tennis_winamax(api_key, cache_bust)
    classements = obtenir_classements_tennis()

    # ESPN : pour figer les archives quand un match a commencé (hors affichage Hot)
    try:
        matchs_espn, _ = obtenir_matchs_tennis_fenetre(cache_bust)
    except Exception:
        matchs_espn = []

    historique = _charger_historique_predictions_tennis()
    archives = {}
    dates_archives = {date_ref}
    for m in list(matchs_a_venir) + list(matchs_espn):
        if m.get("date_paris"):
            dates_archives.add(m["date_paris"])
    for d in dates_archives:
        archives.update(_index_snapshots_tennis((historique.get(d) or {}).get("matches") or []))

    lignes = []
    snapshots = []
    maintenant_iso = datetime.now(TZ_PARIS).isoformat()
    cles_winamax = {_cle_snapshot_tennis(m) for m in matchs_a_venir}

    # 1) Figer les matchs ESPN commencés + ceux qui ont quitté le board Winamax
    for m in matchs_espn:
        if not _match_a_commence_tennis(m):
            continue
        cle = _cle_snapshot_tennis(m)
        old = archives.get(cle)
        if not old or not old.get("favori"):
            continue
        snap = dict(old)
        snap["statut"] = m.get("statut") or snap.get("statut")
        snap["state"] = m.get("state") or snap.get("state")
        snap["a_commence"] = True
        snap["fige"] = True
        snap["fige_le"] = snap.get("fige_le") or maintenant_iso
        snap["date_paris"] = m.get("date_paris") or snap.get("date_paris") or date_ref
        snapshots.append(snap)

    for cle, old in archives.items():
        if cle in cles_winamax:
            continue
        if not old or not old.get("favori") or old.get("fige"):
            continue
        # Était sur Winamax, plus listé → considéré démarré / retiré
        snap = dict(old)
        snap["a_commence"] = True
        snap["fige"] = True
        snap["fige_le"] = snap.get("fige_le") or maintenant_iso
        snap["statut"] = snap.get("statut") or "Retiré Winamax"
        snapshots.append(snap)

    # 2) Afficher + rafraîchir uniquement les matchs Winamax à venir
    for m in matchs_a_venir:
        pred = _calculer_prediction_match(m, classements, index_cotes)
        favori = pred["favori"]
        favori_pct = pred["favori_pct"]
        sets_kind = pred["sets_kind"]
        sets_pct = pred["sets_pct"]
        p1, p2 = pred["proba_j1"], pred["proba_j2"]
        value_kind = pred["value_kind"]
        value_label = pred["value_label"]
        value_msg = pred.get("value_msg")
        cote_favori = pred.get("cote_favori")
        bookmaker = pred.get("bookmaker") or "Bookmaker"
        parts = []
        if pred.get("fav_rang"):
            parts.append(f"Rang favori #{pred['fav_rang']}")
        if pred.get("out_rang"):
            parts.append(f"adv. #{pred['out_rang']}")
        if cote_favori:
            parts.append(f"cote {bookmaker} {float(cote_favori):.2f}")
        else:
            parts.append("pas de cote marché")
        detail_base = " · ".join(parts)
        detail_sets_base = (
            f"Best-of-{m.get('best_of') or 3} · "
            f"{'match équilibré attendu' if sets_kind == 'OUI' else 'écart important → straight sets probable'}"
        )
        sets_label = f"{sets_kind} ({float(sets_pct):.0f}%)" if sets_pct is not None else str(sets_kind)
        statut = m.get("statut") or "À venir"
        date_paris = m.get("date_paris") or date_ref
        heure = m.get("heure_paris") or "—"
        if date_paris and date_paris != date_ref:
            try:
                heure_affichee = (
                    datetime.strptime(date_paris, "%Y-%m-%d").strftime("%d/%m") + f" {heure}"
                )
            except ValueError:
                heure_affichee = f"{date_paris} {heure}"
        else:
            heure_affichee = heure

        lignes.append({
            "confrontation": f"{m['joueur1']} vs {m['joueur2']}",
            "heure": heure_affichee,
            "tournoi": m.get("tournoi"),
            "tableau": m.get("tableau"),
            "statut": statut,
            "fige": False,
            "favori": favori,
            "favori_pct": favori_pct,
            "victoire_detail": detail_base,
            "value_kind": value_kind,
            "value_label": value_label,
            "value_msg": value_msg,
            "cote_favori": cote_favori,
            "bookmaker": bookmaker,
            "sets_kind": sets_kind,
            "sets_label": sets_label,
            "sets_pct": sets_pct,
            "sets_detail": detail_sets_base,
            "proba_j1": p1,
            "proba_j2": p2,
            "joueur1": m["joueur1"],
            "joueur2": m["joueur2"],
            "match_id": m.get("match_id"),
            "date_paris": date_paris,
        })
        snapshots.append({
            "match_id": m.get("match_id"),
            "joueur1": m["joueur1"],
            "joueur2": m["joueur2"],
            "date_paris": date_paris,
            "statut": statut,
            "state": m.get("state"),
            "a_commence": False,
            "favori": favori,
            "favori_pct": favori_pct,
            "sets_kind": sets_kind,
            "sets_pct": sets_pct,
            "proba_j1": p1,
            "proba_j2": p2,
            "value_kind": value_kind,
            "value_label": value_label,
            "value_msg": value_msg,
            "cote_favori": cote_favori,
            "bookmaker": bookmaker,
            "victoire_detail_base": detail_base,
            "sets_detail_base": detail_sets_base,
            "fige": False,
            "fige_le": None,
        })

    par_date: dict[str, list] = {}
    for snap in snapshots:
        d = snap.get("date_paris") or date_ref
        par_date.setdefault(d, []).append(snap)
    for d, snaps in par_date.items():
        _sauvegarder_predictions_tennis(d, snaps)

    return matchs_a_venir, lignes, date_ref


# ============================================================
# UI Streamlit
# ============================================================
st.set_page_config(
    page_title="Tennis Stats — Hot Pronostics",
    page_icon="🎾",
    layout="wide",
    initial_sidebar_state="expanded",
)
apply_theme("tennis")
render_page_header(
    "Tennis Stats",
    "Résumé du jour & Hot Pronostics — ATP / WTA, tous championnats",
    league="tennis",
)

with st.sidebar:
    st.header("⚙️ Paramètres")
    st.markdown(
        """
        **Légende Hot Pronostics**
        - **Source** : matchs à venir Winamax (The-Odds-API)
        - **À venir uniquement** : retiré dès qu'il quitte Winamax / démarre
        - **Victoire** : favori algo (classement) + %
        - **Value** : 🟢 forte / 🟠 moyenne / 🔴 pas de value (vs cote Winamax)
        - **Les 2 gagnent un set** : Oui / Non selon l'équilibre du match
        """
    )
    st.caption(f"Date de référence : {datetime.now(TZ_PARIS).strftime('%Y-%m-%d')} (heure de Paris)")
    if _obtenir_cle_odds_api():
        st.caption("Hot Pronostics : calendrier + cotes Winamax")
    else:
        st.caption("⚠️ Clé Odds-API manquante — Hot Pronostics vide sans Winamax")

onglets = st.tabs(["📊 Résumé", "🔥 Hot Pronostics"], on_change="rerun")

# ---- Résumé ----
with onglets[0]:
    if onglets[0].open:
        render_section_title(
            "Résumé du jour",
            "Tous les matchs tennis du jour, tous championnats confondus",
        )
        if "tennis_resume_bust" not in st.session_state:
            st.session_state.tennis_resume_bust = 0
        if st.button("🔄 Rafraîchir les scores", key="btn_refresh_resume"):
            st.session_state.tennis_resume_bust += 1
            try:
                obtenir_matchs_tennis_du_jour.clear()
                obtenir_matchs_tennis_odds_du_jour.clear()
                _charger_scoreboard_espn.clear()
                _lister_sports_tennis_odds_api.clear()
                obtenir_matchs_tennis_winamax.clear()
            except Exception:
                pass

        with st.spinner("Récupération des scores tennis (ESPN / Odds-API)..."):
            df_resume, df_termines, err, date_ref = construire_resume_tennis(
                st.session_state.tennis_resume_bust
            )

        st.caption(f"Date de référence (heure de Paris) : {date_ref}")
        if err:
            st.error(err)
        elif df_resume.empty:
            st.info("Aucun match tennis programmé aujourd'hui (heure de Paris).")
        else:
            n_live = int(
                df_resume["Statut"].astype(str).str.contains(
                    "In Progress|Live|En cours", case=False, na=False
                ).sum()
            ) if "Statut" in df_resume.columns else 0
            st.caption(f"{len(df_resume)} match(s) · dont ~{n_live} en cours")
            st.dataframe(
                df_resume,
                hide_index=True,
                width="stretch",
                column_config={
                    "Heure": st.column_config.TextColumn("Heure", width="small"),
                    "Match": st.column_config.TextColumn("Match", width="large"),
                    "Statut": st.column_config.TextColumn("Statut", width="small"),
                    "Score": st.column_config.TextColumn("Score", width="medium"),
                    "Sets": st.column_config.TextColumn("Sets", width="small"),
                    "Vainqueur": st.column_config.TextColumn("Vainqueur", width="medium"),
                },
            )

            st.markdown("---")
            st.subheader("✅ Confrontations terminées — validation des prédictions")
            st.caption(
                "Pour chaque match fini : ✅ si la prédiction est bonne, ❌ sinon — "
                "sur le vainqueur et sur « les 2 gagnent un set »."
            )
            if df_termines.empty:
                st.info("Aucune confrontation terminée à valider pour le moment.")
            else:
                n_ok_v = int(df_termines["_ok_victoire"].sum()) if "_ok_victoire" in df_termines else 0
                n_ok_s = int(df_termines["_ok_sets"].sum()) if "_ok_sets" in df_termines else 0
                n_tot = len(df_termines)
                st.caption(
                    f"{n_tot} match(s) terminé(s) · "
                    f"victoires validées {n_ok_v}/{n_tot} · "
                    f"sets validés {n_ok_s}/{n_tot}"
                )
                df_aff = df_termines.drop(columns=["_ok_victoire", "_ok_sets"], errors="ignore")
                st.dataframe(
                    df_aff,
                    hide_index=True,
                    width="stretch",
                    column_config={
                        "Heure": st.column_config.TextColumn("Heure", width="small"),
                        "Match": st.column_config.TextColumn("Match", width="large"),
                        "Score": st.column_config.TextColumn("Score", width="medium"),
                        "Sets": st.column_config.TextColumn("Sets", width="small"),
                        "Vainqueur": st.column_config.TextColumn("Vainqueur", width="medium"),
                        "Pred. victoire": st.column_config.TextColumn("Pred. victoire", width="large"),
                        "Pred. sets": st.column_config.TextColumn("Pred. sets", width="large"),
                    },
                )

# ---- Hot Pronostics ----
with onglets[1]:
    if onglets[1].open:
        render_section_title(
            "Hot Pronostics — Winamax",
            "Victoire, Value & les deux gagnent un set — matchs à venir listés chez Winamax",
        )
        if "tennis_hot_bust" not in st.session_state:
            st.session_state.tennis_hot_bust = 0

        if not _obtenir_cle_odds_api():
            st.warning(
                "Configure la clé The-Odds-API dans `.streamlit/secrets.toml` "
                "(`[odds_api]` → `api_key`) pour charger les matchs Winamax."
            )

        with st.spinner("Chargement des matchs Winamax + classements ATP/WTA..."):
            matchs_a_venir, lignes_recap, date_ref = construire_donnees_hot_pronostics_tennis(
                st.session_state.tennis_hot_bust
            )

        if not matchs_a_venir:
            st.info("Aucun match tennis à venir chez Winamax pour le moment.")
        else:
            st.subheader("📋 Tableau de bord — Winamax à venir")
            afficher_tableau_recap_hot_pronostics_tennis(lignes_recap)
            st.caption(
                "Victoire : classement ATP/WTA. "
                "Value : écart algo vs cote Winamax (≥ +5 pts = value forte). "
                "Les 2 gagnent un set : plus le match est équilibré, plus « Oui » est probable."
            )
            st.caption(
                "Liste = matchs encore cotés chez Winamax. Dès qu'un match démarre "
                "ou disparaît du board, il est retiré et la suite remonte."
            )
            st.caption(
                "⚠️ Heuristiques automatiques à titre informatif — pas de garantie de résultat."
            )
            st.caption(
                f"📅 {len(matchs_a_venir)} match(s) Winamax · réf. {date_ref} (Paris) · "
                f"{len({m.get('tournoi') for m in matchs_a_venir})} tournoi(x)"
            )

            st.markdown("---")
            afficher_assistant_hot_pronostics_tennis(lignes_recap, key_prefix="tennis_hot")

            with st.expander("Méthodologie", expanded=False):
                st.markdown(
                    """
                    - **Source calendrier** : The-Odds-API, bookmaker **Winamax** uniquement.
                    - **Victoire** : force relative via le rang / points ATP-WTA
                      (indépendant du marché, comme MLB).
                    - **Value Bet** : `Proba_Algo − Proba_Implicite(cote Winamax)` ;
                      ≥ +5 pts = value forte, ≤ −5 = pas de value.
                    - **Les 2 gagnent un set** : fonction de l'équilibre du match.
                    - Un match qui quitte Winamax (démarré) disparaît de Hot Pronostics.
                    """
                )

render_footer("Tennis", datetime.now(TZ_PARIS).strftime("%d/%m/%Y %H:%M") + " Paris")
