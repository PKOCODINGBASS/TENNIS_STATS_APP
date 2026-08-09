"""
Application d'Analyse Statistiques Tennis (ATP / WTA via ESPN)
===================================================================
Même esprit que MLB / NPB / KBO / NHL, avec 2 onglets uniquement :
  - Résumé du jour (tous matchs, tous championnats)
  - Hot Pronostics (victoire + les 2 gagnent un set) + assistant questions

Sources :
  - Scoreboard ESPN (`site.web.api.espn.com`) — calendrier / scores live
  - Classements ESPN ATP & WTA
  - Optionnel : cotes h2h The-Odds-API si clé configurée
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
ESPN_API = "https://site.web.api.espn.com/apis/site/v2/sports/tennis"
TZ_PARIS = ZoneInfo("Europe/Paris")
ANNEE_COURANTE = datetime.now(TZ_PARIS).year

# Ligues ESPN à agréger (le scoreboard "all" couvre souvent ATP+WTA du jour ;
# on déduplique ensuite). Ajouter d'autres slugs s'ils répondent 200.
LIGUES_ESPN = ("all", "atp", "wta")

_SESSION = requests.Session()
_SESSION.headers.update({
    "User-Agent": "PARIS-SPORTIFS-Tennis-Stats-App/1.0",
    "Accept": "application/json",
})


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
@st.cache_data(show_spinner=False, ttl=180)
def _charger_scoreboard_espn(ligue: str) -> dict:
    return appeler_avec_retry(_get_json, f"{ESPN_API}/{ligue}/scoreboard")


@st.cache_data(show_spinner=False, ttl=1800)
def _charger_classement_espn(tour: str) -> dict:
    """tour = 'atp' ou 'wta' → {nom_normalise: {rang, points, nom}}."""
    try:
        data = appeler_avec_retry(_get_json, f"{ESPN_API}/{tour}/rankings")
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
def obtenir_matchs_tennis_du_jour(cache_bust: int = 0, _cache_version: int = 3) -> tuple[list[dict], str]:
    """
    Agrège les matchs de la journée tennis (heure de Paris), tous championnats ESPN.
    Inclut : date Paris = aujourd'hui, plus les matchs encore en cours / à venir
    démarrés la veille (sessions Nord-Amérique qui débordent après minuit Paris).
    """
    del cache_bust, _cache_version
    maintenant = datetime.now(TZ_PARIS)
    aujourdhui = maintenant.strftime("%Y-%m-%d")
    hier = (maintenant - timedelta(days=1)).strftime("%Y-%m-%d")
    vus = set()
    matchs = []
    for ligue in LIGUES_ESPN:
        try:
            data = _charger_scoreboard_espn(ligue)
        except Exception:
            continue
        for m in _extraire_matchs_depuis_scoreboard(data, ligue):
            date_m = m.get("date_paris") or ""
            state = m.get("state") or ""
            if date_m == aujourdhui:
                garder = True
            elif date_m == hier and state in ("in", "pre"):
                # Session US encore live / pas jouée après minuit Paris
                garder = True
            else:
                garder = False
            if not garder:
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


@st.cache_data(show_spinner=False, ttl=1800)
def obtenir_classements_tennis() -> dict:
    """Fusion ATP + WTA indexée par nom normalisé."""
    out = {}
    out.update(_charger_classement_espn("atp"))
    out.update(_charger_classement_espn("wta"))
    return out


# ============================================================
# Cotes (optionnel)
# ============================================================
@st.cache_data(show_spinner=False, ttl=900)
def obtenir_cotes_tennis_du_jour(api_key: str | None) -> dict:
    """
    Retourne {(nom1_norm, nom2_norm): {joueur: cote}} à partir des sports
    tennis actifs The-Odds-API.
    """
    if not api_key:
        return {}
    try:
        sports = appeler_avec_retry(
            _get_json,
            "https://api.the-odds-api.com/v4/sports",
            {"apiKey": api_key},
        )
    except Exception:
        return {}
    tennis_keys = [
        s["key"] for s in sports
        if s.get("active") and (
            s.get("group") == "Tennis" or str(s.get("key", "")).startswith("tennis_")
        )
    ]
    index = {}
    for key in tennis_keys:
        try:
            events = appeler_avec_retry(
                _get_json,
                f"https://api.the-odds-api.com/v4/sports/{key}/odds",
                {
                    "apiKey": api_key,
                    "regions": "eu",
                    "markets": "h2h",
                    "oddsFormat": "decimal",
                },
            )
        except Exception:
            continue
        for ev in events or []:
            home = ev.get("home_team") or ""
            away = ev.get("away_team") or ""
            cotes = {}
            for book in ev.get("bookmakers") or []:
                for market in book.get("markets") or []:
                    if market.get("key") != "h2h":
                        continue
                    for outcome in market.get("outcomes") or []:
                        nom = outcome.get("name")
                        prix = _parser_float(outcome.get("price"))
                        if nom and prix and prix > 1:
                            # moyenne simple si plusieurs bookmakers
                            cotes.setdefault(nom, []).append(prix)
            if not cotes:
                continue
            moyennes = {n: sum(v) / len(v) for n, v in cotes.items()}
            cle_ab = tuple(sorted([_normaliser_nom(home), _normaliser_nom(away)]))
            index[cle_ab] = {_normaliser_nom(n): c for n, c in moyennes.items()}
            # garder aussi les noms bruts pour affichage
            index[cle_ab]["_raw"] = moyennes
    return index


def _cotes_pour_match(match: dict, index_cotes: dict) -> dict | None:
    cle = tuple(sorted([
        _normaliser_nom(match["joueur1"]),
        _normaliser_nom(match["joueur2"]),
    ]))
    return index_cotes.get(cle)


# ============================================================
# Modèles de prédiction (heuristiques tennis)
# ============================================================
def predire_probabilite_victoire_tennis(
    rang1: int | None,
    rang2: int | None,
    points1: float = 0.0,
    points2: float = 0.0,
    cote1: float | None = None,
    cote2: float | None = None,
) -> tuple[float, float]:
    """
    Probabilités de victoire joueur1 / joueur2 (somme = 100).

    1) Signal classement : logistic sur l'écart de rang (et points si dispo)
    2) Signal marché : probabilités implicites des cotes (si présentes)
    3) Blend 40% classement / 60% marché quand les cotes existent, sinon 100% classement
    """
    # --- Classement ---
    r1 = rang1 if rang1 and rang1 > 0 else 80
    r2 = rang2 if rang2 and rang2 > 0 else 80
    # Un meilleur rang (plus petit) → force plus élevée
    force1 = (1.0 / math.sqrt(r1)) + 0.00008 * max(points1, 0.0)
    force2 = (1.0 / math.sqrt(r2)) + 0.00008 * max(points2, 0.0)
    # Softmax
    exp1 = math.exp(3.2 * force1)
    exp2 = math.exp(3.2 * force2)
    p_rank1 = 100.0 * exp1 / (exp1 + exp2)
    p_rank2 = 100.0 - p_rank1

    # --- Marché ---
    if cote1 and cote2 and cote1 > 1 and cote2 > 1:
        inv1, inv2 = 1.0 / cote1, 1.0 / cote2
        total = inv1 + inv2
        p_mkt1 = 100.0 * inv1 / total
        p_mkt2 = 100.0 - p_mkt1
        p1 = 0.40 * p_rank1 + 0.60 * p_mkt1
        p2 = 100.0 - p1
    else:
        p1, p2 = p_rank1, p_rank2

    p1 = max(5.0, min(95.0, p1))
    p2 = 100.0 - p1
    return round(p1, 1), round(p2, 1)


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
def construire_resume_tennis(cache_bust: int = 0) -> tuple[pd.DataFrame, str | None, str]:
    try:
        matchs, date_ref = obtenir_matchs_tennis_du_jour(cache_bust)
    except Exception as exc:
        return pd.DataFrame(), f"Impossible de charger les matchs ({exc}).", datetime.now(TZ_PARIS).strftime("%Y-%m-%d")

    lignes = []
    for m in matchs:
        if m.get("joueur1_winner"):
            vainqueur = m["joueur1"]
        elif m.get("joueur2_winner"):
            vainqueur = m["joueur2"]
        else:
            vainqueur = "—"
        sets = f"{m['joueur1_sets']}-{m['joueur2_sets']}" if m.get("state") != "pre" else "—"
        lignes.append({
            "Heure": m.get("heure_paris") or "—",
            "Tournoi": m.get("tournoi") or "—",
            "Tableau": m.get("tableau") or "—",
            "Match": f"{m['joueur1']} vs {m['joueur2']}",
            "Statut": m.get("statut") or "—",
            "Score": m.get("score") or "—",
            "Sets": sets,
            "Vainqueur": vainqueur,
            "Court": m.get("court") or "—",
        })
    return pd.DataFrame(lignes), None, date_ref


def construire_donnees_hot_pronostics_tennis(cache_bust: int = 0):
    """
    Tous les matchs du jour (simples de préférence, puis doubles),
    avec 2 values : victoire + les 2 gagnent un set.
    """
    matchs, date_ref = obtenir_matchs_tennis_du_jour(cache_bust)
    classements = obtenir_classements_tennis()
    api_key = _obtenir_cle_odds_api()
    index_cotes = obtenir_cotes_tennis_du_jour(api_key)

    # Hot Pronostics : on priorise les simples ; on garde les doubles s'il n'y a
    # aucun simple (journée atypique).
    simples = [m for m in matchs if m.get("est_simple")]
    cibles = simples if simples else matchs

    lignes = []
    for m in cibles:
        i1 = _infos_joueur(m["joueur1"], classements)
        i2 = _infos_joueur(m["joueur2"], classements)
        cotes = _cotes_pour_match(m, index_cotes) or {}
        cote1 = cotes.get(_normaliser_nom(m["joueur1"]))
        cote2 = cotes.get(_normaliser_nom(m["joueur2"]))

        p1, p2 = predire_probabilite_victoire_tennis(
            i1.get("rang"), i2.get("rang"),
            i1.get("points") or 0.0, i2.get("points") or 0.0,
            cote1, cote2,
        )
        if p1 >= p2:
            favori, favori_pct, outsider = m["joueur1"], p1, m["joueur2"]
            fav_rang, out_rang = i1.get("rang"), i2.get("rang")
        else:
            favori, favori_pct, outsider = m["joueur2"], p2, m["joueur1"]
            fav_rang, out_rang = i2.get("rang"), i1.get("rang")

        sets_kind, sets_pct = predire_les_deux_gagnent_un_set(favori_pct, m.get("best_of") or 3)
        sets_label = f"{sets_kind} ({sets_pct:.0f}%)"

        detail_victoire = []
        if fav_rang:
            detail_victoire.append(f"Rang favori #{fav_rang}")
        if out_rang:
            detail_victoire.append(f"adv. #{out_rang}")
        if cote1 and cote2:
            detail_victoire.append("cotes marché intégrées")
        else:
            detail_victoire.append("classement uniquement")

        detail_sets = (
            f"Best-of-{m.get('best_of') or 3} · "
            f"{'match équilibré attendu' if sets_kind == 'OUI' else 'écart important → straight sets probable'}"
        )

        # Hot Pronostics = toujours la PRÉDICTION. Si le match est terminé, on
        # ajoute le résultat réel en détail (sans écraser favori / %).
        statut = m.get("statut") or "—"
        detail_victoire_txt = " · ".join(detail_victoire)
        detail_sets_txt = detail_sets
        sets_kind_aff, sets_label_aff, sets_pct_aff = sets_kind, sets_label, sets_pct

        if m.get("termine"):
            vainqueur_reel = m["joueur1"] if m.get("joueur1_winner") else (
                m["joueur2"] if m.get("joueur2_winner") else "—"
            )
            ok_victoire = (vainqueur_reel == favori)
            detail_victoire_txt = (
                f"Résultat : {vainqueur_reel} "
                f"({'✅' if ok_victoire else '❌'} vs prédit {favori}) · "
                + detail_victoire_txt
            )
            both_sets_reel = m.get("joueur1_sets", 0) > 0 and m.get("joueur2_sets", 0) > 0
            if m.get("joueur1_sets", 0) + m.get("joueur2_sets", 0) > 0:
                ok_sets = (sets_kind == "OUI") == both_sets_reel
                detail_sets_txt = (
                    f"Résultat : {'OUI' if both_sets_reel else 'NON'} "
                    f"({'✅' if ok_sets else '❌'}) · {detail_sets}"
                )

        lignes.append({
            "confrontation": f"{m['joueur1']} vs {m['joueur2']}",
            "heure": m.get("heure_paris") or "—",
            "tournoi": m.get("tournoi"),
            "tableau": m.get("tableau"),
            "statut": statut,
            "favori": favori,
            "favori_pct": favori_pct,
            "victoire_detail": detail_victoire_txt,
            "sets_kind": sets_kind_aff,
            "sets_label": sets_label_aff,
            "sets_pct": sets_pct_aff,
            "sets_detail": detail_sets_txt,
            "proba_j1": p1,
            "proba_j2": p2,
            "joueur1": m["joueur1"],
            "joueur2": m["joueur2"],
        })

    return cibles, lignes, date_ref


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
        - **Victoire** : joueur favori + probabilité
        - **Les 2 gagnent un set** : Oui / Non selon l'équilibre du match
        """
    )
    st.caption(f"Date de référence : {datetime.now(TZ_PARIS).strftime('%Y-%m-%d')} (heure de Paris)")
    if _obtenir_cle_odds_api():
        st.caption("Cotes marché : The-Odds-API activée")
    else:
        st.caption("Cotes marché : non configurées (classements seuls)")

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
                _charger_scoreboard_espn.clear()
            except Exception:
                pass

        with st.spinner("Récupération des scores tennis (ESPN)..."):
            df_resume, err, date_ref = construire_resume_tennis(st.session_state.tennis_resume_bust)

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
                    "Tournoi": st.column_config.TextColumn("Tournoi", width="medium"),
                    "Tableau": st.column_config.TextColumn("Tableau", width="small"),
                    "Match": st.column_config.TextColumn("Match", width="large"),
                    "Statut": st.column_config.TextColumn("Statut", width="small"),
                    "Score": st.column_config.TextColumn("Score", width="medium"),
                    "Sets": st.column_config.TextColumn("Sets", width="small"),
                    "Vainqueur": st.column_config.TextColumn("Vainqueur", width="medium"),
                    "Court": st.column_config.TextColumn("Court", width="small"),
                },
            )

# ---- Hot Pronostics ----
with onglets[1]:
    if onglets[1].open:
        render_section_title(
            "Hot Pronostics du jour",
            "Victoire & les deux gagnent un set — tous matchs, tous championnats",
        )
        if "tennis_hot_bust" not in st.session_state:
            st.session_state.tennis_hot_bust = 0

        with st.spinner("Analyse des matchs du jour (classements ATP/WTA, cotes si dispo)..."):
            matchs_jour, lignes_recap, date_ref = construire_donnees_hot_pronostics_tennis(
                st.session_state.tennis_hot_bust
            )

        if not matchs_jour:
            st.info("Aucun match tennis à pronostiquer pour aujourd'hui (heure de Paris).")
        else:
            st.subheader("📋 Tableau de bord du jour")
            afficher_tableau_recap_hot_pronostics_tennis(lignes_recap)
            st.caption(
                "Victoire : blend classement ATP/WTA (+ cotes marché si disponibles). "
                "Les 2 gagnent un set : plus le match est équilibré, plus « Oui » est probable "
                "(ajusté best-of-3 / best-of-5)."
            )
            st.caption(
                "⚠️ Heuristiques automatiques à titre informatif — pas de garantie de résultat."
            )
            st.caption(
                f"📅 {len(matchs_jour)} match(s) · date {date_ref} (Paris) · "
                f"{len({m.get('tournoi') for m in matchs_jour})} tournoi(x)"
            )

            st.markdown("---")
            afficher_assistant_hot_pronostics_tennis(lignes_recap, key_prefix="tennis_hot")

            with st.expander("Méthodologie", expanded=False):
                st.markdown(
                    """
                    - **Victoire** : force relative via le rang / points ATP-WTA
                      (et les cotes h2h The-Odds-API si la clé est dans les secrets).
                    - **Les 2 gagnent un set** : fonction de l'équilibre du match
                      (`≈ 74%` à 50/50, diminue quand l'écart de favori grandit).
                    - Les matchs **terminés** affichent le résultat réel, avec la
                      prédiction rappelée en détail.
                    """
                )

render_footer("Tennis", datetime.now(TZ_PARIS).strftime("%d/%m/%Y %H:%M") + " Paris")
