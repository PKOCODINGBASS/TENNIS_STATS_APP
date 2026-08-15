# Tennis Stats App

Application Streamlit tennis (ATP / WTA), sur le même modèle que MLB / NPB / KBO / NHL.

## Sources API

- **ESPN** (`site.web.api.espn.com`, repli `site.api.espn.com`) — scoreboard & classements ATP/WTA  
- **The-Odds-API** — sports tennis **par tournoi** (ex. `tennis_atp_cincinnati_open`, `tennis_wta_cincinnati_open`)  
  - Hot Pronostics : marchés h2h **Winamax** (`winamax_fr`)  
  - Résumé : repli scores/cotes si ESPN est vide  

## Onglets

1. **Résumé** — tous les matchs du jour (heure de Paris), tous championnats
2. **Hot Pronostics** — pour chaque match :
   - **Victoire** (favori + %)
   - **Les 2 gagnent un set** (Oui / Non + %)
   - boîte de questions en dessous

## Lancer en local

```bash
cd "Tennis_Stats_App"
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py
```

## Secrets optionnels

`.streamlit/secrets.toml` :

```toml
[odds_api]
api_key = "..."
```

Sans clé, le Résumé repose sur ESPN seul ; Hot Pronostics nécessite Winamax via Odds-API.
