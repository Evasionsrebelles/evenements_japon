#!/usr/bin/env python3
"""
Scraper du calendrier des événements Japon : kanpai.fr + API JapanTravel

Sources combinées :
1. https://www.kanpai.fr/calendrier-japon (pages mensuelles, fériés/festivals)
2. https://api.japantravel.com/api/articles (événements pertinents seulement :
   mois passés exclus — le mois en cours est gardé —, jusqu'à 12 mois dans le
   futur par défaut, cf. --jt-horizon-months)

Par défaut (sans aucun argument), le script :
- scrape les mois trouvés sur kanpai.fr à partir du mois en cours (les mois passés
  sont ignorés), en une seule passe à chaque exécution, sans checkpoint ni reprise :
  chaque lancement re-scrape entièrement ces mois depuis le début. Pour le mois en
  cours (partiellement passé), seuls les jours à partir d'aujourd'hui sont conservés.
- récupère les événements à venir/en cours de l'API JapanTravel qui sont
  "pertinents" : ni dans un mois déjà passé (le mois en cours est toujours
  gardé, même pour ses jours déjà écoulés), ni au-delà de l'horizon (12 mois
  par défaut). Ce filtre s'applique dès l'accumulation en cours de scan : les
  sauvegardes intermédiaires ne contiennent donc que des événements pertinents,
  pas les résidus (événements très anciens ou très lointains) que l'API peut
  renvoyer en cours de route puisqu'elle trie par date de publication et non
  par date d'événement. La récupération reste INCRÉMENTALE : un fichier d'état
  (japantravel_state.json) garde en mémoire les événements déjà vus ; seuls les
  nouveaux événements ou ceux dont une donnée a changé (date ajoutée/modifiée,
  prix, etc.) sont retraités. Une fois la "frontière" d'une exécution complète
  atteinte sans changement, la pagination s'arrête plus tôt. Cette partie
  JapanTravel garde son propre système de reprise (checkpoint) si interrompue
  (Ctrl+C, crash) — seule la partie kanpai n'en a plus. Les mois déjà passés
  sont purgés du state à chaque exécution ; les événements au-delà de
  l'horizon restent en state (pour ressortir plus tard, une fois dans la
  fenêtre des 12 mois), mais ne sont jamais écrits dans le résultat en attendant.
- écrit un seul fichier JSON consolidé dans ./output/calendrier-japon.json
- si un token GitHub est disponible (--github-token ou $GITHUB_TOKEN), publie
  directement ce fichier sur le dépôt GitHub configuré (Contents API, sans
  clone local), puis purge le cache jsDelivr du CSS/JS du calendrier.

Usage :
    python3 kanpai_calendar_scraper.py                     # comportement complet par défaut
    python3 kanpai_calendar_scraper.py --split             # un fichier JSON par mois (kanpai)
    python3 kanpai_calendar_scraper.py --limit 3            # limite kanpai à 3 mois (tests)
    python3 kanpai_calendar_scraper.py --out mon_dossier    # dossier de sortie
    python3 kanpai_calendar_scraper.py --no-japantravel     # désactive la source JapanTravel
    python3 kanpai_calendar_scraper.py --jt-max-pages 20    # borne la source JapanTravel
    python3 kanpai_calendar_scraper.py --jt-horizon-months 3  # ne garder que les 3 prochains mois (JapanTravel)
    python3 kanpai_calendar_scraper.py --jt-horizon-months -1 # désactive l'horizon (tous les événements à venir)
    python3 kanpai_calendar_scraper.py --jt-merge           # fusionne JapanTravel dans les jours kanpai
    python3 kanpai_calendar_scraper.py --jt-full-rescan     # ignore l'arrêt anticipé, tout rebalayer
    GITHUB_TOKEN=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 python3 kanpai_calendar_scraper.py    # + publication auto sur GitHub
    python3 kanpai_calendar_scraper.py --no-push-github     # désactive la publication GitHub
    python3 kanpai_calendar_scraper.py --no-purge-jsdelivr  # désactive la purge jsDelivr
"""

import argparse
import base64
import calendar
import hashlib
import json
import os
import re
import sys
import time
import unicodedata
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://www.kanpai.fr"
CALENDAR_URL = f"{BASE_URL}/calendrier-japon"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; KanpaiCalendarAudit/1.0; +https://example.com)"
}

# --- Source additionnelle : API JapanTravel ---------------------------------
JAPANTRAVEL_API_URL = "https://api.japantravel.com/api/articles"
JAPANTRAVEL_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; KanpaiCalendarAudit/1.0; +https://example.com)",
    "Accept": "application/json",
}
MONTH_URL_RE = re.compile(
    r"^/calendrier-japon/"
    r"(janvier|fevrier|f[ée]vrier|mars|avril|mai|juin|juillet|aout|ao[ûu]t|"
    r"septembre|octobre|novembre|decembre|d[ée]cembre)-(\d{4})/?$",
    re.IGNORECASE,
)

# --- Purge du cache jsDelivr (front-end statique du calendrier) -------------
# Appelée en fin de script pour que les fichiers CSS/JS servis via jsDelivr
# soient rafraîchis dès qu'on republie le JSON, sans étape manuelle.
JSDELIVR_PURGE_URLS = [
    "https://purge.jsdelivr.net/gh/Evasionsrebelles/evenements_japon@main/calendrier-japon.js",
    "https://purge.jsdelivr.net/gh/Evasionsrebelles/evenements_japon@main/calendrier-japon.css",
]

# --- Publication automatique sur GitHub --------------------------------------
GITHUB_API_URL = "https://api.github.com"

# Token GitHub (scope "Contents: Read and write" sur le dépôt uniquement, de
# préférence un fine-grained token limité à evenements_japon).
# ATTENTION : ce token a un accès en écriture à ton dépôt. Ne partage jamais ce
# fichier une fois rempli (ne le pousse pas sur un dépôt public/partagé), sous
# peine que quiconque l'obtienne puisse écrire sur le dépôt à ta place.
# Reste vide -> le script utilisera --github-token ou $GITHUB_TOKEN à la place.
GITHUB_TOKEN_HARDCODED = "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"


def slugify(text: str) -> str:
    """Retire les accents pour comparer proprement les noms de mois."""
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii").lower()


# --- Classification des événements kanpai.fr en "Filtres" -------------------
# Catégories utilisées côté front (chips de filtre). Un événement peut avoir
# plusieurs valeurs à la fois (ex: un festival qui est aussi un jour férié).
FILTRES = ["Jours fériés", "Festivals", "Jour spécial", "Anniversaire"]

# Mots-clés recherchés dans le texte de l'événement (normalisé : sans accents,
# en minuscules) pour chaque valeur de Filtres, à l'exception de "Jours fériés"
# qui a sa propre logique d'exclusion (voir classify_kanpai_filtres).
FILTRE_KEYWORDS = {
    "Festivals": ["festival", "matsuri", "awa"],
    "Jour spécial": ["journee mondiale", "journee nationale", "jour de", "jour du"],
    "Anniversaire": ["anniversaire"],
}

# Mots-clés saisonniers pour "Jour spécial", testés avec des frontières de mot
# (\b) plutôt qu'en simple sous-chaîne : "ete" en sous-chaîne matcherait à tort
# des mots anglais comme "detective", "athletes" ou "eternity".
FILTRE_SAISON_KEYWORDS = ["automne", "ete", "hiver", "printemps"]


def classify_kanpai_filtres(text: str) -> list[str]:
    """
    Retourne la liste des valeurs de Filtres qui s'appliquent à un événement
    kanpai.fr, à partir de son texte. Un événement peut correspondre à
    plusieurs filtres simultanément.
    """
    norm = slugify(text)
    filtres = []

    # "Jours fériés" : contient "férié", sauf si c'est "non férié"
    if re.search(r"non\s*ferie", norm):
        pass
    elif re.search(r"ferie", norm):
        filtres.append("Jours fériés")

    for label, keywords in FILTRE_KEYWORDS.items():
        if any(kw in norm for kw in keywords):
            filtres.append(label)

    if any(re.search(rf"\b{kw}\b", norm) for kw in FILTRE_SAISON_KEYWORDS):
        if "Jour spécial" not in filtres:
            filtres.append("Jour spécial")

    return filtres


def is_kanpai_link(url: str) -> bool:
    """Retourne True si l'URL pointe vers le domaine kanpai.fr (à exclure des données extraites)."""
    host = urlparse(url).netloc.lower()
    return host == "kanpai.fr" or host.endswith(".kanpai.fr")


def strip_kanpai_links(links: list[dict]) -> list[dict]:
    """Filtre une liste de liens {text, url} en retirant ceux qui mènent vers kanpai.fr."""
    return [link for link in links if not is_kanpai_link(link["url"])]


def get_soup(url: str, session: requests.Session) -> BeautifulSoup:
    resp = session.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    return BeautifulSoup(resp.text, "html.parser")


def find_month_urls(session: requests.Session) -> list[str]:
    """Récupère, dédoublonnée et triée, la liste des URLs absolues des pages mois."""
    soup = get_soup(CALENDAR_URL, session)
    urls = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        path = urlparse(href).path  # gère les liens relatifs et absolus
        if MONTH_URL_RE.match(path):
            urls.add(urljoin(BASE_URL, path))
    return sorted(urls, key=_month_sort_key)


MONTHS_ORDER = [
    "janvier", "fevrier", "mars", "avril", "mai", "juin",
    "juillet", "aout", "septembre", "octobre", "novembre", "decembre",
]


def _month_sort_key(url: str):
    slug = url.rstrip("/").rsplit("/", 1)[-1]  # ex: septembre-2026
    name, year = slug.rsplit("-", 1)
    name = slugify(name)
    month_index = MONTHS_ORDER.index(name) if name in MONTHS_ORDER else 99
    return (int(year), month_index)


def extract_events_from_col(col_div) -> list[dict]:
    """Extrait les événements (texte + liens) contenus dans une colonne .event"""
    events = []
    if col_div is None:
        return events
    text = col_div.get_text(" ", strip=True)
    if not text:
        return events

    is_multi_day = "multi-day-event" in col_div.get("class", [])
    is_start = "start" in col_div.get("class", [])
    is_end = "end" in col_div.get("class", [])

    links = [
        {"text": a.get_text(strip=True), "url": urljoin(BASE_URL, a["href"])}
        for a in col_div.find_all("a", href=True)
    ]
    links = strip_kanpai_links(links)

    events.append(
        {
            "text": text,
            "links": links,
            "type": "multi-day" if is_multi_day else "unknown",
            "is_start": is_start,
            "is_end": is_end,
            "filtres": classify_kanpai_filtres(text),
        }
    )
    return events


def extract_one_day_events(day_block) -> list[dict]:
    events = []
    if day_block is None:
        return events
    for one_day in day_block.find_all("div", class_="one-day-event"):
        text = one_day.get_text(" ", strip=True)
        if not text:
            continue
        links = [
            {"text": a.get_text(strip=True), "url": urljoin(BASE_URL, a["href"])}
            for a in one_day.find_all("a", href=True)
        ]
        links = strip_kanpai_links(links)
        events.append({"text": text, "links": links, "type": "one-day", "filtres": classify_kanpai_filtres(text)})
    return events


def parse_month_page(url: str, session: requests.Session) -> dict:
    soup = get_soup(url, session)

    slug = url.rstrip("/").rsplit("/", 1)[-1]
    month_name_slug, year = slug.rsplit("-", 1)

    title_tag = soup.find("h1")
    title = title_tag.get_text(strip=True) if title_tag else None

    days = []
    # Chaque jour du mois est rendu dans un <div class="row event-container">
    for row in soup.find_all("div", class_="event-container"):
        day_number_tag = row.select_one(".day .number")
        if day_number_tag is None:
            # Ligne sans info de jour (garde-fou), on l'ignore
            continue

        try:
            day_number = int(day_number_tag.get_text(strip=True))
        except ValueError:
            continue

        weekday_tag = row.select_one(".day .fs-16")
        weekday_letter = weekday_tag.get_text(strip=True) if weekday_tag else None

        # Colonnes des événements multi-jours (jusqu'à 2 en parallèle sur le site)
        multi_day_events = []
        multi_cols = row.select(".col-lg-13.col-xl-11 .event")
        for col in multi_cols:
            multi_day_events.extend(extract_events_from_col(col))

        # Événement(s) sur un seul jour, affiché(s) à droite
        one_day_block = row.select_one(".col-lg-8.col-xl-11.pb-3")
        one_day_events = extract_one_day_events(one_day_block)

        all_events = one_day_events + multi_day_events

        days.append(
            {
                "day": day_number,
                "weekday_letter": weekday_letter,
                "date_iso": f"{year}-{_month_number(month_name_slug):02d}-{day_number:02d}",
                "events": all_events,
            }
        )

    return {
        "url": url,
        "month_slug": month_name_slug,
        "year": int(year),
        "title": title,
        "days": days,
    }


def _month_number(month_name_slug: str) -> int:
    name = slugify(month_name_slug)
    return MONTHS_ORDER.index(name) + 1 if name in MONTHS_ORDER else 0


def _parse_jt_date(value: str | None) -> date | None:
    """Parse un champ 'YYYY-MM-DD HH:MM:SS' de l'API JapanTravel en objet date."""
    if not value:
        return None
    try:
        return datetime.strptime(value.split(" ", 1)[0], "%Y-%m-%d").date()
    except ValueError:
        return None


def is_future_event(raw_event_date: dict | None, today: date | None = None) -> bool:
    """
    Détermine si un événement est encore à venir (ou en cours), en se basant sur
    event_date.end si présent, sinon event_date.start. Retourne False si aucune
    date exploitable n'est disponible (on exclut par prudence).
    """
    if today is None:
        today = date.today()
    if not raw_event_date:
        return False

    end = _parse_jt_date(raw_event_date.get("end"))
    start = _parse_jt_date(raw_event_date.get("start"))
    reference = end or start
    if reference is None:
        return False

    return reference >= today


def add_months(d: date, months: int) -> date:
    """Ajoute `months` mois à une date, en calant le jour sur la fin de mois si besoin."""
    month_index = d.month - 1 + months
    year = d.year + month_index // 12
    month = month_index % 12 + 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def clamp_event_start_to_month(event: dict, month_start: date) -> dict:
    """
    Retourne une copie de `event` dont la date de début (event_date.start) est
    ramenée au 1er jour du mois en cours (`month_start`) si elle lui est
    strictement antérieure (événement en cours, commencé un mois précédent).
    L'heure d'origine est préservée si elle est connue (sinon 00:00:00).
    N'affecte jamais l'événement passé en argument ni les données conservées
    dans le state (fingerprint/reprise incrémentale) : seule la copie retournée,
    destinée à la sortie (fichier JSON / fusion avec les jours kanpai), est modifiée.
    """
    event_date = event.get("event_date") or {}
    start_raw = event_date.get("start")
    start = _parse_jt_date(start_raw)
    if start is None or start >= month_start:
        return event

    time_part = "00:00:00"
    if start_raw and " " in start_raw:
        time_part = start_raw.split(" ", 1)[1]

    new_event = dict(event)
    new_event["event_date"] = dict(event_date)
    new_event["event_date"]["start"] = f"{month_start.isoformat()} {time_part}"
    return new_event


def is_within_horizon(raw_event_date: dict | None, horizon_end: date | None) -> bool:
    """
    Détermine si un événement JapanTravel démarre avant (ou à) `horizon_end`.
    Se base sur event_date.start (date de début de l'événement), avec repli sur
    event_date.end si aucun début n'est renseigné. `horizon_end=None` désactive
    le filtre (tout est gardé). Par prudence, un événement sans aucune date
    exploitable est exclu (comme is_future_event).
    """
    if horizon_end is None:
        return True
    if not raw_event_date:
        return False

    start = _parse_jt_date(raw_event_date.get("start"))
    end = _parse_jt_date(raw_event_date.get("end"))
    reference = start or end
    if reference is None:
        return False

    return reference <= horizon_end


def compute_fingerprint(item: dict) -> str:
    """
    Calcule une empreinte des champs 'mutables' d'un événement, pour détecter les
    mises à jour (date ajoutée/changée, prix, etc.) sans se fier à un champ
    'updated_at' que l'API ne fournit pas de façon fiable.
    """
    relevant = {
        "title": item.get("title"),
        "event_date": item.get("event_date"),
        "event_general_price": item.get("event_general_price"),
        "event_free": item.get("event_free"),
        "category": (item.get("category") or {}).get("code"),
        "url": item.get("url"),
    }
    blob = json.dumps(relevant, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def load_json_file(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def save_json_file(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def default_jt_state() -> dict:
    return {"min_id_seen": None, "last_full_run_at": None, "last_run_at": None, "events": {}}


def prune_state_events(state: dict, cutoff: date) -> int:
    """
    Retire du state les événements dont la date de référence (fin, repli sur
    le début) est strictement antérieure à `cutoff`. En pratique `cutoff` est
    le premier jour du mois en cours : les mois déjà passés sont purgés, le
    mois en cours ne l'est jamais, même pour des jours déjà écoulés dans ce mois.
    """
    to_delete = []
    for id_str, entry in state["events"].items():
        ev = entry["event"]
        end = _parse_jt_date(ev["event_date"]["end"])
        start = _parse_jt_date(ev["event_date"]["start"])
        reference = end or start
        if reference is not None and reference < cutoff:
            to_delete.append(id_str)
    for id_str in to_delete:
        del state["events"][id_str]
    return len(to_delete)


def fetch_japantravel_page(session: requests.Session, params: dict, max_retries: int = 5,
                            base_delay: float = 2.0, timeout: float = 20.0) -> dict:
    """
    Récupère une page de l'API JapanTravel avec plusieurs tentatives en cas de coupure
    réseau (l'API semble parfois fermer la connexion sans réponse sur des sessions
    longues). Backoff exponentiel simple entre les tentatives.
    """
    last_exc: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            resp = session.get(JAPANTRAVEL_API_URL, headers=JAPANTRAVEL_HEADERS, params=params, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.RequestException as exc:
            last_exc = exc
            if attempt == max_retries:
                break
            wait = base_delay * (2 ** (attempt - 1))
            print(
                f"  [JapanTravel] Erreur réseau sur la page {params.get('page')} "
                f"(tentative {attempt}/{max_retries}) : {exc}. Nouvel essai dans {wait:.1f}s...",
                file=sys.stderr,
            )
            time.sleep(wait)
    assert last_exc is not None
    raise last_exc


def fetch_and_sync_japantravel_events(
    session: requests.Session,
    state_path: Path,
    checkpoint_path: Path,
    event_type: str | None = "event",
    lang: str | None = None,
    future_only: bool = True,
    horizon_end: date | None = None,
    delay: float = 0.5,
    max_pages: int | None = None,
    full_rescan: bool = False,
    write_every: int = 5,
    on_progress=None,
) -> tuple[list[dict], dict]:
    """
    Récupère les événements de l'API JapanTravel en ne retraitant que les
    nouveaux/modifiés, avec reprise sur interruption via un fichier checkpoint.

    Fonctionnement :
    - `state_path` conserve, entre les exécutions, l'empreinte de chaque événement
      déjà vu ainsi que le plus petit ID atteint lors de la dernière exécution
      complète ('frontière'). Comme l'API trie par date de publication décroissante
      et que les IDs y sont corrélés, dès qu'une page entière ne contient plus que
      des événements déjà connus ET inchangés ET sous cette frontière, on arrête
      la pagination : inutile de rebalayer les ~2400 pages à chaque lancement.
    - `checkpoint_path` est écrit après CHAQUE page lue. S'il existe au démarrage,
      on reprend directement à la page indiquée (interruption précédente).
    - `on_progress`, si fourni, est appelé tous les `write_every` événements
      nouveaux/modifiés trouvés (toutes sources de pages confondues), avec la
      liste normalisée des événements accumulés jusque-là. Permet une écriture
      progressive du fichier consolidé sans attendre la fin du scan complet.
    - LIMITE CONNUE : si un événement ancien (déjà sous la frontière) voit sa date
      corrigée après coup, l'arrêt anticipé peut l'empêcher d'être re-détecté lors
      d'une exécution normale. Utiliser `full_rescan=True` de temps en temps pour
      forcer un balayage complet et rattraper ce cas.
    - `horizon_end`, si fourni, ne garde comme "pertinent" que les événements
      dont la date de début (repli sur la fin si absente) est <= à cette date.
    - Un événement est considéré "pertinent" s'il n'est pas dans un mois déjà
      passé (le mois en cours est toujours gardé, même pour ses jours déjà
      écoulés) ET s'il respecte `horizon_end`. Seuls les événements pertinents
      sont accumulés dans `progress_events`/renvoyés dans le résultat final ;
      les mois passés sont purgés du state à la fin de chaque exécution
      (voir `prune_state_events`), les événements au-delà de l'horizon restent
      en state (pour ressortir plus tard, une fois dans la fenêtre).
    """
    today = date.today()
    month_start = date(today.year, today.month, 1)

    def is_relevant(event_date: dict | None) -> bool:
        if future_only and not is_future_event(event_date, month_start):
            return False
        return is_within_horizon(event_date, horizon_end)

    state = load_json_file(state_path) or default_jt_state()
    state.setdefault("events", {})

    checkpoint = load_json_file(checkpoint_path)
    if checkpoint:
        print(f"  [JapanTravel] Reprise d'une exécution interrompue (page {checkpoint['next_page']}).")
        page = checkpoint["next_page"]
        events_this_run = checkpoint["events_this_run"]
        min_id_seen_this_run = checkpoint["min_id_seen_this_run"]
        started_at = checkpoint["started_at"]
    else:
        page = 1
        events_this_run = {}
        min_id_seen_this_run = None
        started_at = datetime.now().isoformat()

    # Reconstitue la liste déjà accumulée (utile en cas de reprise sur checkpoint),
    # en ne gardant que les événements pertinents, pour que le compteur d'écriture
    # progressive reparte du bon endroit.
    progress_events = [
        clamp_event_start_to_month(entry["event"], month_start)
        for entry in events_this_run.values()
        if is_relevant(entry["event"]["event_date"])
    ]
    progress_since_last_write = len(progress_events) % write_every if write_every else 0

    frontier = None if full_rescan else state.get("min_id_seen")
    new_count = 0
    updated_count = 0
    unchanged_count = 0
    stopped_early = False

    while True:
        if max_pages is not None and page > max_pages:
            break

        params = {"page": page}
        if lang:
            params["lang"] = lang  # best-effort, non confirmé côté API

        payload = fetch_japantravel_page(session, params)

        items = payload.get("data", [])
        if not items:
            break

        page_all_known_unchanged = True
        page_all_below_frontier = True

        for item in items:
            item_id = item.get("id")
            if item_id is not None and (min_id_seen_this_run is None or item_id < min_id_seen_this_run):
                min_id_seen_this_run = item_id

            if event_type and item.get("type") != event_type:
                continue
            if lang and item.get("lang") != lang:
                continue

            fp = compute_fingerprint(item)
            existing = state["events"].get(str(item_id))

            if existing is None:
                category = "new"
            elif existing.get("fingerprint") != fp:
                category = "updated"
            else:
                category = "unchanged"

            if category == "unchanged":
                unchanged_count += 1
            else:
                page_all_known_unchanged = False
                normalized = normalize_japantravel_item(item)
                events_this_run[str(item_id)] = {"fingerprint": fp, "event": normalized}
                if category == "new":
                    new_count += 1
                else:
                    updated_count += 1

                # N'accumuler (et ne compter pour l'écriture progressive) que les
                # événements pertinents : mois passés et hors-horizon sont ignorés
                # ici, même s'ils restent trackés dans events_this_run/le state.
                if is_relevant(normalized["event_date"]):
                    progress_events.append(clamp_event_start_to_month(normalized, month_start))
                    progress_since_last_write += 1
                    if on_progress and write_every and progress_since_last_write >= write_every:
                        on_progress(list(progress_events), {
                            "new": new_count, "updated": updated_count, "unchanged": unchanged_count,
                        })
                        progress_since_last_write = 0

            if frontier is not None and (item_id is None or item_id > frontier):
                page_all_below_frontier = False

        meta = payload.get("meta", {})
        last_page = meta.get("last_page")
        print(f"  [JapanTravel] page {page}{f'/{last_page}' if last_page else ''} "
              f"-> nouveaux: {new_count}, maj: {updated_count}, inchangés: {unchanged_count}, "
              f"pertinents accumulés: {len(progress_events)}")

        # Checkpoint après chaque page : permet la reprise propre en cas d'interruption
        save_json_file(checkpoint_path, {
            "started_at": started_at,
            "next_page": page + 1,
            "min_id_seen_this_run": min_id_seen_this_run,
            "events_this_run": events_this_run,
        })

        if (
            not full_rescan
            and frontier is not None
            and page_all_known_unchanged
            and page_all_below_frontier
            and items
        ):
            print(f"  [JapanTravel] Frontière déjà connue atteinte (id <= {frontier}) sans changement : "
                  f"arrêt anticipé.")
            stopped_early = True
            break

        if last_page and page >= last_page:
            break
        if not payload.get("links", {}).get("next"):
            break

        page += 1
        time.sleep(delay)

    # Balayage terminé sans interruption : fusion dans le state, avancée de la frontière, checkpoint effacé
    state["events"].update(events_this_run)
    if min_id_seen_this_run is not None:
        state["min_id_seen"] = (
            min_id_seen_this_run if frontier is None else min(frontier, min_id_seen_this_run)
        )
    state["last_run_at"] = datetime.now().isoformat()
    if full_rescan or state.get("last_full_run_at") is None or frontier is None:
        state["last_full_run_at"] = datetime.now().isoformat()

    pruned = prune_state_events(state, month_start)

    save_json_file(state_path, state)
    if checkpoint_path.exists():
        checkpoint_path.unlink()

    result = [
        clamp_event_start_to_month(entry["event"], month_start)
        for entry in state["events"].values()
        if is_relevant(entry["event"]["event_date"])
    ]

    summary = {
        "new": new_count,
        "updated": updated_count,
        "unchanged": unchanged_count,
        "pruned_from_state": pruned,
        "stopped_early": stopped_early,
        "total_future_events": len(result),
    }
    return result, summary


def normalize_japantravel_item(item: dict) -> dict:
    """Extrait/normalise les champs demandés depuis un item brut de l'API JapanTravel."""
    event_date = item.get("event_date") or {}
    category = item.get("category") or {}

    title = item.get("title") or ""
    category_name = category.get("name") or ""
    # Même logique de classification "Filtres" que pour kanpai.fr, appliquée au
    # texte disponible côté JapanTravel (titre + nom de catégorie).
    classify_text = f"{title} {category_name}"

    return {
        "source": "japantravel",
        "id": item.get("id"),
        "title": item.get("title"),
        "slug": item.get("slug"),
        "lang": item.get("lang"),
        "event_date": {
            "start": event_date.get("start"),
            "end": event_date.get("end"),
            "unknown_start_time": event_date.get("unknown_start_time"),
            "unknown_end_time": event_date.get("unknown_end_time"),
        },
        "event_general_price": item.get("event_general_price"),
        "event_free": item.get("event_free"),
        "category": {
            "id": category.get("id"),
            "code": category.get("code"),
            "name": category.get("name"),
        },
        "url": item.get("url"),
        "filtres": classify_kanpai_filtres(classify_text),
    }


def date_iso_from_event_date_start(start: str | None) -> str | None:
    """Extrait la partie date (YYYY-MM-DD) d'un champ event_date.start du type 'YYYY-MM-DD HH:MM:SS'."""
    if not start:
        return None
    return start.split(" ", 1)[0]


def merge_japantravel_into_days(months: list[dict], jt_events: list[dict]) -> None:
    """
    Fusionne les événements JapanTravel dans la structure `days` des mois déjà
    scrapés sur kanpai.fr, en les rattachant par date_iso (jour de début de l'événement).
    Modifie `months` en place.
    """
    by_date = defaultdict(list)
    for ev in jt_events:
        d = date_iso_from_event_date_start(ev["event_date"]["start"])
        if d:
            by_date[d].append(ev)

    for month in months:
        for day in month.get("days", []):
            matches = by_date.get(day["date_iso"], [])
            if matches:
                day["events"].extend(matches)


def github_get_file_sha(session: requests.Session, repo: str, path: str, branch: str, token: str) -> str | None:
    """Récupère le sha du fichier existant sur GitHub (None s'il n'existe pas encore).
    L'API Contents exige ce sha pour toute mise à jour (sinon elle refuse, croyant à un conflit)."""
    url = f"{GITHUB_API_URL}/repos/{repo}/contents/{path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    resp = session.get(url, headers=headers, params={"ref": branch}, timeout=20)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json().get("sha")


def github_upload_file(
    session: requests.Session,
    repo: str,
    path: str,
    branch: str,
    token: str,
    local_path: Path,
    commit_message: str,
) -> None:
    """
    Crée ou met à jour un fichier dans un dépôt GitHub via l'API Contents
    (PUT /repos/{repo}/contents/{path}), sans passer par git/un clone local.
    """
    content_bytes = local_path.read_bytes()
    encoded = base64.b64encode(content_bytes).decode("ascii")

    sha = github_get_file_sha(session, repo, path, branch, token)

    url = f"{GITHUB_API_URL}/repos/{repo}/contents/{path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    payload = {"message": commit_message, "content": encoded, "branch": branch}
    if sha:
        payload["sha"] = sha  # présent = mise à jour ; absent = création

    resp = session.put(url, headers=headers, json=payload, timeout=30)
    resp.raise_for_status()
    commit_sha = resp.json().get("commit", {}).get("sha", "?")
    action = "mis à jour" if sha else "créé"
    print(f"  [GitHub] {path} {action} sur {repo}@{branch} (commit {commit_sha[:7] if commit_sha != '?' else '?'}).")


def purge_jsdelivr_cache(session: requests.Session, urls: list[str]) -> None:
    """
    Appelle purge.jsdelivr.net pour chaque URL fournie, afin de forcer le CDN à
    resservir la dernière version des fichiers statiques (CSS/JS) juste après
    qu'on les a republiés sur GitHub. N'échoue jamais le script en cas de souci
    réseau : c'est une étape "best effort" annexe à la mise à jour des données.
    """
    for url in urls:
        try:
            resp = session.get(url, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            status = data.get("status", "?")
            print(f"  [jsDelivr] Purge {url} -> statut: {status}")
        except Exception as exc:  # noqa: BLE001
            print(f"  [jsDelivr] Échec de la purge de {url}: {exc}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description="Audit du calendrier Japon kanpai.fr")
    parser.add_argument("--out", default="output", help="Dossier de sortie (défaut: ./output)")
    parser.add_argument("--split", action="store_true", help="Un fichier JSON par mois au lieu d'un seul fichier consolidé")
    parser.add_argument("--limit", type=int, default=None, help="Limiter le nombre de mois traités (utile pour tester)")
    parser.add_argument("--delay", type=float, default=1.0, help="Délai (s) entre deux requêtes, pour rester correct avec le serveur")
    parser.add_argument("--no-japantravel", dest="with_japantravel", action="store_false", default=True,
                         help="Désactive la récupération des événements de l'API "
                              "api.japantravel.com/api/articles (activée par défaut)")
    parser.add_argument("--jt-max-pages", type=int, default=None,
                         help="Nombre max de pages à lire sur l'API JapanTravel "
                              "(défaut: illimité, parcourt toutes les pages jusqu'à épuisement)")
    parser.add_argument("--jt-lang", default=None,
                         help="Filtre de langue best-effort pour l'API JapanTravel (ex: fr) — non garanti côté serveur")
    parser.add_argument("--jt-include-past", action="store_true",
                         help="Désactive le filtre 'événements à venir' (par défaut, seuls les événements dont "
                              "la fin — ou le début si pas de fin — est aujourd'hui ou après sont conservés)")
    parser.add_argument("--jt-horizon-months", type=int, default=12,
                         help="N'inclure dans le résultat que les événements JapanTravel démarrant dans les N "
                              "prochains mois (défaut: 12). Mettre -1 pour désactiver cet horizon et garder "
                              "tous les événements à venir. Ne restreint que le résultat final, pas le "
                              "balayage/l'état interne (japantravel_state.json).")
    parser.add_argument("--jt-full-rescan", action="store_true",
                         help="Force un balayage complet de toutes les pages, en ignorant l'arrêt anticipé "
                              "basé sur la frontière connue. Utile de temps en temps pour rattraper d'éventuelles "
                              "corrections de date sur des événements déjà vus.")
    parser.add_argument("--jt-state-file", default=None,
                         help="Chemin du fichier d'état JapanTravel (défaut: <out>/japantravel_state.json)")
    parser.add_argument("--jt-checkpoint-file", default=None,
                         help="Chemin du fichier de checkpoint JapanTravel (défaut: <out>/japantravel_checkpoint.json)")
    parser.add_argument("--jt-delay", type=float, default=0.5,
                         help="Délai (s) entre deux requêtes vers l'API JapanTravel")
    parser.add_argument("--jt-merge", action="store_true",
                         help="Fusionne les événements JapanTravel dans les jours du calendrier kanpai.fr (par date). "
                              "Sinon, ils sont écrits séparément.")
    parser.add_argument("--no-purge-jsdelivr", dest="purge_jsdelivr", action="store_false", default=True,
                         help="Désactive la purge automatique du cache jsDelivr (CSS/JS) en fin d'exécution "
                              "(activée par défaut).")
    parser.add_argument("--github-repo", default="Evasionsrebelles/evenements_japon",
                         help="Dépôt GitHub 'owner/repo' où publier le JSON consolidé (défaut: %(default)s)")
    parser.add_argument("--github-path", default="calendrier-japon.json",
                         help="Chemin du fichier dans le dépôt GitHub (défaut: %(default)s)")
    parser.add_argument("--github-branch", default="main",
                         help="Branche cible sur GitHub (défaut: %(default)s)")
    parser.add_argument("--github-token", default=None,
                         help="Token GitHub avec droit d'écriture sur le dépôt. Peut aussi être fourni via "
                              "la variable d'environnement GITHUB_TOKEN (recommandé, évite de l'exposer "
                              "dans l'historique du shell).")
    parser.add_argument("--no-push-github", dest="push_github", action="store_false", default=True,
                         help="Désactive la publication automatique sur GitHub (par défaut, activée dès "
                              "qu'un token est disponible via --github-token ou $GITHUB_TOKEN).")
    args = parser.parse_args()

    if args.jt_max_pages == -1:
        args.jt_max_pages = None

    github_token = GITHUB_TOKEN_HARDCODED or args.github_token or os.environ.get("GITHUB_TOKEN")
    if args.push_github and not github_token:
        print(
            "  [GitHub] Aucun token fourni (GITHUB_TOKEN_HARDCODED / --github-token / $GITHUB_TOKEN "
            "absents) : la publication automatique sur GitHub sera ignorée.",
            file=sys.stderr,
        )
        args.push_github = False

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    session = requests.Session()

    print(f"Lecture de la page calendrier : {CALENDAR_URL}")
    month_urls = find_month_urls(session)
    print(f"{len(month_urls)} mois trouvés.")

    # Ne garder que le mois en cours et les suivants (pas de mois passés).
    today = date.today()
    current_key = (today.year, today.month - 1)  # aligné sur _month_sort_key (index 0-based)
    month_urls = [u for u in month_urls if _month_sort_key(u) >= current_key]
    print(f"{len(month_urls)} mois à partir du mois en cours.")

    if args.limit:
        month_urls = month_urls[: args.limit]

    all_months = []
    kanpai_event_count = 0  # Compteur pour écrire tous les 5 événements

    for i, url in enumerate(month_urls, 1):
        print(f"[{i}/{len(month_urls)}] Extraction : {url}")
        try:
            month_data = parse_month_page(url, session)
        except Exception as exc:  # noqa: BLE001
            print(f"  -> Erreur sur {url}: {exc}", file=sys.stderr)
            continue

        # Le mois en cours est partiellement passé : ne garder que les jours
        # à partir d'aujourd'hui (les mois suivants ont déjà tous leurs jours >= today).
        today_iso = today.isoformat()
        month_data["days"] = [d for d in month_data.get("days", []) if d["date_iso"] >= today_iso]

        all_months.append(month_data)
        
        # Compter les événements pour l'écriture progressive
        for day in month_data.get("days", []):
            kanpai_event_count += len(day.get("events", []))

        if args.split:
            fname = out_dir / f"{month_data['month_slug']}-{month_data['year']}.json"
            fname.write_text(json.dumps(month_data, ensure_ascii=False, indent=2), encoding="utf-8")
        else:
            # Écrire tous les 5 événements dans le fichier consolidé
            if kanpai_event_count >= 5:
                fname = out_dir / "calendrier-japon.json"
                payload = {"source": CALENDAR_URL, "months": all_months}
                fname.write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                kanpai_event_count = 0  # Réinitialiser le compteur
                print(f"  -> Sauvegarde intermédiaire : {len(all_months)} mois")

        time.sleep(args.delay)

    jt_events = []
    if args.with_japantravel:
        state_path = Path(args.jt_state_file) if args.jt_state_file else out_dir / "japantravel_state.json"
        checkpoint_path = Path(args.jt_checkpoint_file) if args.jt_checkpoint_file else out_dir / "japantravel_checkpoint.json"

        horizon_end = (
            None if args.jt_horizon_months is not None and args.jt_horizon_months < 0
            else add_months(date.today(), args.jt_horizon_months)
        )

        pages_desc = "illimitée (jusqu'à la dernière page ou arrêt anticipé)" if args.jt_max_pages is None else str(args.jt_max_pages)
        horizon_desc = "illimité" if horizon_end is None else f"jusqu'au {horizon_end.isoformat()} ({args.jt_horizon_months} mois)"
        print(f"Lecture de l'API JapanTravel : {JAPANTRAVEL_API_URL} "
              f"(pages: {pages_desc}, lang={args.jt_lang}, full_rescan={args.jt_full_rescan}, "
              f"horizon: {horizon_desc})")
        print(f"  État: {state_path}")
        print(f"  Checkpoint: {checkpoint_path}")

        def write_jt_progress(events_so_far, counts):
            """Écrit le fichier consolidé tous les 5 événements JapanTravel nouveaux/modifiés."""
            if args.split:
                return  # en mode --split, pas de fichier consolidé à mettre à jour
            fname = out_dir / "calendrier-japon.json"
            payload = {"source": CALENDAR_URL, "months": all_months}
            if not args.jt_merge:
                payload["japantravel_source"] = JAPANTRAVEL_API_URL
                payload["japantravel_events"] = events_so_far
            fname.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"  -> Sauvegarde intermédiaire (JapanTravel) : {len(events_so_far)} événements accumulés")

        try:
            jt_events, jt_summary = fetch_and_sync_japantravel_events(
                session,
                state_path=state_path,
                checkpoint_path=checkpoint_path,
                event_type="event",
                lang=args.jt_lang,
                future_only=not args.jt_include_past,
                horizon_end=horizon_end,
                delay=args.jt_delay,
                max_pages=args.jt_max_pages,
                full_rescan=args.jt_full_rescan,
                write_every=5,
                on_progress=write_jt_progress,
            )
            print(
                f"JapanTravel : {jt_summary['new']} nouveaux, {jt_summary['updated']} mis à jour, "
                f"{jt_summary['unchanged']} inchangés (ignorés), {jt_summary['pruned_from_state']} "
                f"purgés (passés) -> {jt_summary['total_future_events']} événements à venir au total"
                f"{' [arrêt anticipé]' if jt_summary['stopped_early'] else ''}."
            )
        except KeyboardInterrupt:
            print(
                "\nInterrompu : la progression JapanTravel a été sauvegardée dans "
                f"{checkpoint_path}. Relance le script pour reprendre automatiquement."
            )
            raise
        except Exception as exc:  # noqa: BLE001
            print(f"  -> Erreur lors de la lecture de l'API JapanTravel: {exc}", file=sys.stderr)

        if args.jt_merge and jt_events:
            merge_japantravel_into_days(all_months, jt_events)
            print("Événements JapanTravel fusionnés dans les jours du calendrier kanpai.fr.")

    if not args.split:
        fname = out_dir / "calendrier-japon.json"
        payload = {"source": CALENDAR_URL, "months": all_months}
        if jt_events and not args.jt_merge:
            payload["japantravel_source"] = JAPANTRAVEL_API_URL
            payload["japantravel_events"] = jt_events
        fname.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"Fichier consolidé écrit : {fname}")
    else:
        print(f"{len(all_months)} fichiers JSON écrits dans {out_dir}/")
        if jt_events and not args.jt_merge:
            jt_fname = out_dir / "japantravel-events.json"
            jt_fname.write_text(
                json.dumps({"source": JAPANTRAVEL_API_URL, "events": jt_events}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            print(f"Fichier JapanTravel écrit séparément : {jt_fname}")

    if args.push_github:
        if args.split:
            print(
                "  [GitHub] Publication automatique ignorée : --split produit plusieurs fichiers, "
                "seul le mode consolidé (calendrier-japon.json) est géré pour l'instant.",
                file=sys.stderr,
            )
        else:
            print(f"Publication sur GitHub : {args.github_repo}@{args.github_branch} ({args.github_path})...")
            try:
                github_upload_file(
                    session,
                    repo=args.github_repo,
                    path=args.github_path,
                    branch=args.github_branch,
                    token=github_token,
                    local_path=out_dir / "calendrier-japon.json",
                    commit_message=f"Mise à jour automatique du calendrier ({datetime.now().isoformat(timespec='seconds')})",
                )
            except Exception as exc:  # noqa: BLE001
                print(f"  [GitHub] Échec de la publication : {exc}", file=sys.stderr)

    if args.purge_jsdelivr:
        print("Purge du cache jsDelivr (CSS/JS du calendrier)...")
        purge_jsdelivr_cache(session, JSDELIVR_PURGE_URLS)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrompu par l'utilisateur.", file=sys.stderr)
        print("Aucune reprise automatique côté kanpai : relance le script pour tout "
              "re-scraper depuis le mois en cours.", file=sys.stderr)
        sys.exit(130)
