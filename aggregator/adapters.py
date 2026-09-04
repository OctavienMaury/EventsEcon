"""Adaptateurs : un par façon dont un site publie son agenda.

Chaque adaptateur reçoit le dict de configuration de la source et renvoie une
liste d'Event. Il ne lève jamais d'exception vers l'appelant : les erreurs
remontent dans le rapport de collecte.
"""

from __future__ import annotations

import csv
import io
import json
import re
import time
from datetime import datetime, timedelta

import requests
from bs4 import BeautifulSoup

from .model import Event, _key

UA = {"User-Agent": "agenda-eco-paris/1.0 (agrégateur de séminaires académiques)"}
TIMEOUT = 45
DELAY = 1.0        # pause entre deux requêtes, en secondes

MOIS_FR = {
    "janvier": 1, "février": 2, "fevrier": 2, "mars": 3, "avril": 4, "mai": 5,
    "juin": 6, "juillet": 7, "août": 8, "aout": 8, "septembre": 9,
    "octobre": 10, "novembre": 11, "décembre": 12, "decembre": 12,
}
MOIS_ABBR = {
    "janv": 1, "fevr": 2, "févr": 2, "mars": 3, "avr": 4, "mai": 5, "juin": 6,
    "juil": 7, "aout": 8, "août": 8, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
    "déc": 12,
}
MONTHS_EN = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
}


def _get(url: str, **kw):
    """Une requête par seconde au maximum : on est invité chez ces labos."""
    time.sleep(DELAY)
    return requests.get(url, headers=UA, timeout=TIMEOUT, **kw)


BLOCKS = ["p", "li", "td", "th", "dd", "dt", "h1", "h2", "h3", "h4", "h5", "h6",
          "div", "tr", "blockquote", "article", "section"]


def _txt(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer"]):
        tag.decompose()
    return soup.get_text("\n", strip=True)


def _blocks(html: str) -> list[str]:
    """Texte bloc par bloc : une ligne = un paragraphe, une cellule, un item.

    Indispensable ici : les sites académiques emballent la moitié d'une ligne
    dans des <strong>, et get_text() naïf couperait « September 28th - » de
    « PARKER (MIT Sloan) ».
    """
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer"]):
        tag.decompose()
    for br in soup.find_all("br"):
        br.replace_with("\u2016")          # sentinelle : survit à strip=True
    out: list[str] = []
    for el in soup.find_all(BLOCKS):
        if el.find(BLOCKS):          # on ne garde que les blocs terminaux
            continue
        for line in el.get_text(" ", strip=True).split("\u2016"):
            line = re.sub(r"\s+", " ", line).strip()
            if line:
                out.append(line)
    if not out:
        out = [l.strip() for l in _txt(html).splitlines() if l.strip()]
    return out


def _speaker(chunk: str) -> tuple[str, str]:
    """Sépare « BONHOMME Stéphane (University of Chicago) » -> nom, affiliation."""
    m = re.match(r"^\s*(.+?)\s*\(([^)]*)\)\s*$", chunk.strip())
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return chunk.strip(), ""


# --------------------------------------------------------------------- iCal

def fetch_ics(src: dict) -> list[Event]:
    r = _get(src["url"])
    r.raise_for_status()
    events, cur = [], None
    lines = r.text.replace("\r\n ", "").replace("\r\n\t", "").splitlines()
    for line in lines:
        if line.startswith("BEGIN:VEVENT"):
            cur = {}
        elif line.startswith("END:VEVENT") and cur is not None:
            if cur.get("DTSTART"):
                events.append(Event(
                    start=cur["DTSTART"],
                    end=cur.get("DTEND"),
                    series=cur.get("CATEGORIES", "") or src["name"],
                    title=cur.get("SUMMARY", ""),
                    room=cur.get("LOCATION", ""),
                    url=cur.get("URL", src.get("url", "")),
                    institution=src["institution"],
                    city=src.get("city", ""),
                    source_id=src["id"],
                ))
            cur = None
        elif cur is not None and ":" in line:
            key, _, val = line.partition(":")
            key = key.split(";")[0].upper()
            if key in ("DTSTART", "DTEND"):
                cur[key] = _parse_ical_dt(val)
            else:
                cur[key] = val.replace("\\,", ",").replace("\\n", " ").strip()
    return events


def _parse_ical_dt(val: str):
    val = val.strip().rstrip("Z")
    for fmt in ("%Y%m%dT%H%M%S", "%Y%m%d"):
        try:
            return datetime.strptime(val, fmt)
        except ValueError:
            continue
    return None


# ----------------------------------------- WordPress « The Events Calendar »

def fetch_tribe_rest(src: dict) -> list[Event]:
    base = src["url"].rstrip("/")
    url = f"{base}/wp-json/tribe/events/v1/events"
    params = {
        "per_page": 50,
        "start_date": datetime.now().strftime("%Y-%m-%d"),
        "end_date": (datetime.now() + timedelta(days=180)).strftime("%Y-%m-%d"),
    }
    events, page = [], 1
    while page <= 6:
        r = _get(url, params={**params, "page": page})
        if r.status_code != 200:
            break
        data = r.json()
        batch = data.get("events", [])
        if not batch:
            break
        for e in batch:
            start = _iso(e.get("start_date"))
            if not start:
                continue
            venue = (e.get("venue") or {}).get("venue", "")
            events.append(Event(
                start=start,
                end=_iso(e.get("end_date")),
                series=_clean(e.get("title", "")),
                title=_clean(BeautifulSoup(e.get("description", ""), "html.parser").get_text(" ")[:300]),
                room=venue,
                url=e.get("url", src["url"]),
                institution=src["institution"],
                city=src.get("city", ""),
                source_id=src["id"],
            ))
        if len(batch) < params["per_page"]:
            break
        page += 1
    return events


def fetch_wp_rest(src: dict) -> list[Event]:
    base = src["url"].rstrip("/")
    post_type = src.get("post_type", "posts")
    r = _get(f"{base}/wp-json/wp/v2/{post_type}", params={"per_page": 50})
    r.raise_for_status()
    events = []
    for p in r.json():
        raw = f"{p.get('title', {}).get('rendered', '')} {p.get('content', {}).get('rendered', '')}"
        start = _first_date(_clean(BeautifulSoup(raw, 'html.parser').get_text(' ')))
        if not start:
            continue
        events.append(Event(
            start=start,
            series=_clean(BeautifulSoup(p.get("title", {}).get("rendered", ""), "html.parser").get_text()),
            url=p.get("link", src["url"]),
            institution=src["institution"],
            city=src.get("city", ""),
            source_id=src["id"],
        ))
    return events


# --------------------------------------------------------- schema.org / Event

def fetch_jsonld(src: dict) -> list[Event]:
    """Beaucoup de sites publient leurs événements en JSON-LD dans le HTML.

    Si la page n'en contient pas, on retombe sur une extraction par dates
    dans le texte, qui donne au moins la date et l'intitulé.
    """
    r = _get(src["url"])
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    events = []

    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        for node in _iter_nodes(data):
            if not isinstance(node, dict):
                continue
            if "Event" not in str(node.get("@type", "")):
                continue
            start = _iso(node.get("startDate"))
            if not start:
                continue
            loc = node.get("location")
            room = ""
            if isinstance(loc, dict):
                room = loc.get("name", "") or ""
            elif isinstance(loc, str):
                room = loc
            events.append(Event(
                start=start,
                end=_iso(node.get("endDate")),
                series=_clean(node.get("name", "")),
                title=_clean(BeautifulSoup(str(node.get("description", "")), "html.parser").get_text(" ")[:300]),
                room=room,
                url=node.get("url") or src["url"],
                institution=src["institution"],
                city=src.get("city", ""),
                source_id=src["id"],
            ))

    if not events:
        events = _fallback_text(src, _txt(r.text))
    return events


def _iter_nodes(data):
    if isinstance(data, list):
        for item in data:
            yield from _iter_nodes(item)
    elif isinstance(data, dict):
        yield data
        for value in data.values():
            if isinstance(value, (list, dict)):
                yield from _iter_nodes(value)


def _fallback_text(src: dict, text: str) -> list[Event]:
    """Dernier recours : « 17 septembre 2026 » ou « 17/09/2026 » + ligne suivante."""
    events = []
    lines = [l for l in text.splitlines() if l.strip()]
    for i, line in enumerate(lines):
        start = _first_date(line, assume_year=True)
        if not start:
            continue
        label = line
        if len(label) < 25 and i + 1 < len(lines):
            label = f"{label} — {lines[i + 1]}"
        events.append(Event(
            start=start,
            series=_clean(label)[:180],
            url=src["url"],
            institution=src["institution"],
            city=src.get("city", ""),
            source_id=src["id"],
        ))
    return events


# ------------------------------------------------------- XLAgenda (pseweb.eu)

DATE_BLOCK = re.compile(
    r"Du\s+(\d{2}/\d{2}/\d{4})\s+de\s+(\d{1,2}:\d{2})\s+à\s+(\d{1,2}:\d{2})", re.I)


def fetch_xlagenda(src: dict) -> list[Event]:
    """L'agenda mutualisé de PSE, mois par mois.

    Une entrée se lit :
        <nom du séminaire>
        Du 18/03/2025 de 14:00 à 15:15
        <salle éventuelle>
        CHOR Davin (Dartmouth)
        <titre de la communication>
    On découpe le flux de texte sur le motif de date : ça marche que le site
    mette chaque champ dans sa propre balise ou tout dans un seul bloc.
    """
    events = []
    today = datetime.now()
    for offset in range(int(src.get("months_ahead", 3)) + 1):
        month = today.replace(day=1) + timedelta(days=32 * offset)
        r = _get(src["url"], params={"month": month.month, "year": month.year})
        if r.status_code != 200:
            continue
        text = "\n".join(_blocks(r.text))
        parts = DATE_BLOCK.split(text)
        # parts = [avant, jj/mm/aaaa, h1, h2, entre, jj/mm/aaaa, h1, h2, entre, ...]
        for i in range(1, len(parts) - 3, 4):
            day, h1, h2 = parts[i], parts[i + 1], parts[i + 2]
            before, after = parts[i - 1], parts[i + 3]
            try:
                start = datetime.strptime(f"{day} {h1}", "%d/%m/%Y %H:%M")
                end = datetime.strptime(f"{day} {h2}", "%d/%m/%Y %H:%M")
            except ValueError:
                continue
            prev = [l for l in before.split("\n") if l.strip()]
            series = prev[-1].strip() if prev else src["name"]
            body = [l.strip() for l in after.split("\n") if l.strip()]
            if i + 4 < len(parts) - 3 and body:
                body = body[:-1]      # la dernière ligne annonce la séance suivante
            body = body[:5]
            room, speaker, affil, title = "", "", "", ""
            for part in body:
                if not room and re.match(
                        r"^(salle|room|amphi|R\d|PSE|Campus|MSE|\d{1,3}\s?(bd|boulevard|rue|av))",
                        part, re.I):
                    room = part
                elif not speaker and "(" in part and ")" in part and len(part) < 120:
                    speaker, affil = _speaker(part)
                elif not title and len(part) > 6 and part.strip("*") and not DATE_BLOCK.search(part):
                    title = part
            events.append(Event(
                start=start, end=end,
                series=series, title=title.strip("* "),
                speaker=speaker, affiliation=affil, room=room,
                url=f"{src['url']}?day={start.day}&month={start.month}&year={start.year}",
                institution=src["institution"], city=src.get("city", ""),
                source_id=src["id"],
            ))
    return events


# --------------------------------------------------- Sciences Po (dép. éco)

LINE_EN = re.compile(
    r"^\**\s*(January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+(\d{1,2})(?:st|nd|rd|th)?\s*[-–—]\s*(.+)$", re.I)
SEMESTER = re.compile(r"(FALL|WINTER|SPRING|AUTUMN|WINTER-SPRING)[^\d]{0,20}(\d{4})", re.I)


def fetch_sciencespo(src: dict) -> list[Event]:
    """Les pages « seminars » listent : « September 28th - Jonathan PARKER (MIT) Titre »."""
    events = []
    for sem in src.get("seminars", []):
        url = f"{src['base'].rstrip('/')}/{sem['slug']}/"
        try:
            r = _get(url)
            if r.status_code != 200:
                continue
        except requests.RequestException:
            continue
        lines = _blocks(r.text)
        year, semester = datetime.now().year, ""
        for line in lines:
            head = SEMESTER.search(line)
            if head and len(line) < 60:
                semester, year = head.group(1).upper(), int(head.group(2))
                continue
            m = LINE_EN.match(line)
            if not m:
                continue
            month = MONTHS_EN[m.group(1).lower()]
            # une page « FALL 2026 » couvre sept.-déc. ; « WINTER-SPRING 2026 » janv.-juin
            yr = year
            if semester.startswith("FALL") and month < 7:
                yr = year + 1
            rest = m.group(3).strip(" *-–")
            if rest.upper().startswith("TBA"):
                speaker, affil, title = "", "", "À confirmer"
            else:
                speaker, affil = _speaker(rest.split(")")[0] + ")") if "(" in rest else (rest[:60], "")
                title = rest.split(")", 1)[1].strip(" *:–-") if ")" in rest else ""
            h1, _, h2 = sem.get("default_time", "12:30-13:45").partition("-")
            try:
                start = datetime.strptime(f"{yr}-{month}-{m.group(2)} {h1}", "%Y-%m-%d %H:%M")
                end = datetime.strptime(f"{yr}-{month}-{m.group(2)} {h2}", "%Y-%m-%d %H:%M")
            except ValueError:
                continue
            events.append(Event(
                start=start, end=end,
                series=sem["title"], title=_clean(title)[:250],
                speaker=_clean(speaker), affiliation=_clean(affil),
                url=url, institution=src["institution"], city=src.get("city", ""),
                source_id=src["id"],
            ))
    return events


# --------------------------------------------------------------------- divers

def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[*_]{1,2}", "", str(s or ""))).strip()


def _iso(value):
    if not value:
        return None
    value = str(value).strip().replace("Z", "")
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(value.split("+")[0].strip(), fmt.replace("%z", "").strip())
        except ValueError:
            continue
    return None


def _first_date(text: str, assume_year: bool = False):
    m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", text)          # ISO
    if m:
        y, mo, d = map(int, m.groups())
        try:
            return datetime(y, mo, d, 12, 0)
        except ValueError:
            return None
    m = re.search(r"(\d{1,2})[/\.](\d{1,2})[/\.](\d{4})", text)
    if m:
        d, mo, y = map(int, m.groups())
        try:
            return datetime(y, mo, d, 12, 0)
        except ValueError:
            return None
    m = re.search(r"(\d{1,2})\s+(" + "|".join(MOIS_FR) + r")\s+(\d{4})", text, re.I)
    if m:
        try:
            return datetime(int(m.group(3)), MOIS_FR[m.group(2).lower()], int(m.group(1)), 12, 0)
        except ValueError:
            return None
    if assume_year:
        # « 12 Juin » sans année : on prend l'occurrence à venir la plus proche
        m = re.search(r"(\d{1,2})\s+(" + "|".join(MOIS_FR) + r")\b", text, re.I)
        if m:
            now = datetime.now()
            mo, d = MOIS_FR[m.group(2).lower()], int(m.group(1))
            for year in (now.year, now.year + 1):
                try:
                    cand = datetime(year, mo, d, 12, 0)
                except ValueError:
                    return None
                if cand > now - timedelta(days=15):
                    return cand
    return None


def fetch_csv(src: dict) -> list[Event]:
    """Une feuille Google Sheets publiée en CSV, pour la saisie manuelle.

    Dans Sheets : Fichier > Partager > Publier sur le Web > format CSV.
    Colonnes attendues (l'ordre est libre, les manquantes sont ignorées) :
        date, debut, fin, seminaire, intervenant, affiliation, titre, salle, url
    « date » au format JJ/MM/AAAA ou AAAA-MM-JJ ; « debut »/« fin » en HH:MM.
    """
    r = _get(src["url"])
    r.raise_for_status()
    r.encoding = r.encoding or "utf-8"
    events = []
    for row in csv.DictReader(io.StringIO(r.text)):
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
        start = _first_date(row.get("date", ""))
        if not start:
            continue
        h1 = row.get("debut") or row.get("début") or "12:30"
        h2 = row.get("fin") or ""
        try:
            hh, mm = (int(x) for x in h1.replace("h", ":").split(":")[:2])
            start = start.replace(hour=hh, minute=mm)
        except ValueError:
            pass
        end = None
        if h2:
            try:
                hh, mm = (int(x) for x in h2.replace("h", ":").split(":")[:2])
                end = start.replace(hour=hh, minute=mm)
            except ValueError:
                end = None
        events.append(Event(
            start=start, end=end,
            series=row.get("seminaire") or row.get("séminaire") or src["name"],
            title=row.get("titre", ""),
            speaker=row.get("intervenant", ""),
            affiliation=row.get("affiliation", ""),
            room=row.get("salle") or row.get("lieu", ""),
            url=row.get("url") or src.get("url", ""),
            institution=row.get("institution") or src["institution"],
            city=src.get("city", ""),
            source_id=src["id"],
        ))
    return events



# ------------------------------------------------------------- EconomiX

ECONOMIX_DATE = re.compile(
    r"^(?:date_range\s*)?(\d{1,2})\s+(" + "|".join(MOIS_FR) + r")\s+(\d{4})$", re.I)
ECONOMIX_NOISE = {
    "intervenant(s)", "informations complémentaires", "informations pratiques",
    "en savoir plus", "expand_more", "archive", "programme", "annule", "annulé",
    "séminaires à venir", "séminaires passés", "date_range",
}
HEURE = re.compile(r"(\d{1,2})\s*[hH:]\s*(\d{2})?")


def fetch_economix(src: dict) -> list[Event]:
    """EconomiX empile chaque séance en blocs successifs :

        date_range 22 SEPTEMBRE 2026
        Fighting Tax Evasion in the Gig Economy
        Intervenant(s)
        Louis Pape (Telecom Paris, CREST)
        Informations pratiques
        11h - 12h15 en salle 614A
    """
    events = []
    for sem in src.get("seminars", []):
        url = f"{src['base'].rstrip('/')}/{sem['slug']}"
        try:
            r = _get(url)
            if r.status_code != 200:
                continue
        except requests.RequestException:
            continue

        blocks = _blocks(r.text)
        hits = [i for i, b in enumerate(blocks) if ECONOMIX_DATE.match(b.strip())]
        for n, i in enumerate(hits):
            m = ECONOMIX_DATE.match(blocks[i].strip())
            stop = hits[n + 1] if n + 1 < len(hits) else min(i + 12, len(blocks))
            title, speaker, affil, room, heure = "", "", "", sem.get("room", ""), ""
            expect_speaker = False
            for b in blocks[i + 1:stop]:
                low = b.strip().lower()
                if low in ECONOMIX_NOISE:
                    expect_speaker = low == "intervenant(s)"
                    continue
                if expect_speaker and not speaker:
                    speaker, affil = _speaker(b)
                    expect_speaker = False
                elif not title:
                    title = b
                else:
                    if not heure and HEURE.search(b) and len(b) < 120:
                        heure = b
                    if "salle" in low and len(b) < 120:
                        room = b
            if not title or title.strip().upper().startswith("ANNUL"):
                continue

            h1, _, h2 = sem.get("default_time", "12:30-14:00").partition("-")
            hm = HEURE.search(heure) if heure else None
            if hm:
                h1 = f"{int(hm.group(1)):02d}:{hm.group(2) or '00'}"
            try:
                d = f"{m.group(3)}-{MOIS_FR[m.group(2).lower()]}-{m.group(1)}"
                start_dt = datetime.strptime(f"{d} {h1}", "%Y-%m-%d %H:%M")
                end_dt = datetime.strptime(f"{d} {h2}", "%Y-%m-%d %H:%M")
                if end_dt <= start_dt:
                    end_dt = start_dt + timedelta(minutes=75)
            except (ValueError, KeyError):
                continue
            events.append(Event(
                start=start_dt, end=end_dt, series=sem["title"],
                title=_clean(title)[:250], speaker=_clean(speaker),
                affiliation=_clean(affil), room=_clean(room),
                url=url, institution=src["institution"], city=src.get("city", ""),
                source_id=src["id"],
            ))
    return events


# --------------------------------------------- PSE, site institutionnel

PSE_ITEM = re.compile(
    r"^Du\s+\w+\.?\s+(\d{1,2})\s+([A-Za-zéûôà]+)\.?", re.I)
PSE_HEURE = re.compile(r"Heure\s*(\d{1,2}:\d{2})\s*[–\-]\s*(\d{1,2}:\d{2})")
PSE_LIEU = re.compile(r"Lieu\s+([^|]{2,60})")
PSE_KINDS = ("Séminaire", "Colloque", "Conférence", "Table ronde", "Symposium",
             "Formation", "Alumni", "Job Market", "Prix et distinctions")


def fetch_pse_events(src: dict) -> list[Event]:
    """Le nouvel agenda de PSE (WordPress), paginé.

    Une fiche se présente ainsi :
        Du lun. 07 sept.
        Séminaire
        Régulation et Environnement
        Bruno Conte (UPF)
        Lieu R1-09
        Heure 12:30 – 13:30

    L'année n'est pas indiquée : on prend l'occurrence à venir la plus proche.
    Cette page agrège aussi des séances tenues au CREST et à Sciences Po, d'où
    l'intérêt du dédoublonnage en aval.
    """
    events = []
    base = src["url"].rstrip("/")
    for page in range(1, int(src.get("pages", 4)) + 1):
        url = base if page == 1 else f"{base}/page/{page}/"
        try:
            r = _get(url)
            if r.status_code != 200:
                break
        except requests.RequestException:
            break
        blocks = _blocks(r.text)
        hits = [i for i, b in enumerate(blocks) if PSE_ITEM.match(b.strip())]
        if not hits:
            break
        for n, i in enumerate(hits):
            m = PSE_ITEM.match(blocks[i].strip())
            stop = hits[n + 1] if n + 1 < len(hits) else min(i + 10, len(blocks))
            segment = blocks[i:stop]
            joined = " | ".join(segment)

            title, speaker, affil = "", "", ""
            for b in segment[1:]:
                txt = b.strip()
                if txt in PSE_KINDS:          # étiquette « Séminaire » / « Colloque »
                    continue
                elif txt.startswith(("Lieu", "Heure", "Partager", "Voir")):
                    continue
                elif "(" in txt and ")" in txt and len(txt) < 90 and not speaker and title:
                    speaker, affil = _speaker(txt)
                elif not title and len(txt) > 5:
                    title = txt

            mois = MOIS_ABBR.get(_key(m.group(2))[:4])
            if not mois:
                continue
            now = datetime.now()
            heure = PSE_HEURE.search(joined)
            h1, h2 = (heure.group(1), heure.group(2)) if heure else ("12:30", "13:45")
            start = end = None
            for year in (now.year, now.year + 1):
                try:
                    start = datetime.strptime(f"{year}-{mois}-{m.group(1)} {h1}", "%Y-%m-%d %H:%M")
                except ValueError:
                    break
                if start > now - timedelta(days=2):
                    break
            if not start:
                continue
            try:
                end = datetime.strptime(f"{start:%Y-%m-%d} {h2}", "%Y-%m-%d %H:%M")
            except ValueError:
                end = start + timedelta(minutes=75)

            lieu = PSE_LIEU.search(joined)
            events.append(Event(
                start=start, end=end if end > start else start + timedelta(minutes=75),
                series=_clean(title)[:200], title="",
                speaker=_clean(speaker), affiliation=_clean(affil),
                room=_clean(lieu.group(1)) if lieu else "",
                url=src["url"], institution=src["institution"],
                city=src.get("city", ""), source_id=src["id"],
            ))
    return events


ADAPTERS = {
    "ics": fetch_ics,
    "csv": fetch_csv,
    "tribe_rest": fetch_tribe_rest,
    "wp_rest": fetch_wp_rest,
    "jsonld": fetch_jsonld,
    "xlagenda": fetch_xlagenda,
    "sciencespo": fetch_sciencespo,
    "economix": fetch_economix,
    "pse_events": fetch_pse_events,
}
