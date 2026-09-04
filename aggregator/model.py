"""Modèle d'événement, normalisation et déduplication."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, asdict
from dataclasses import field as dfield
from datetime import datetime, date
from difflib import SequenceMatcher


@dataclass
class Event:
    """Une séance de séminaire, d'atelier ou de colloque."""

    start: datetime                    # début (heure locale Paris)
    end: datetime | None = None
    series: str = ""                   # nom du séminaire : "Macroeconomics Seminar"
    title: str = ""                    # titre de la communication
    speaker: str = ""
    affiliation: str = ""
    room: str = ""
    institution: str = ""
    city: str = ""
    url: str = ""
    source_id: str = ""
    kind: str = "seminaire"            # seminaire | workshop | colloque
    field: str = ""                    # rempli par classify()
    also_at: list[str] = dfield(default_factory=list)   # autres sources (séances jointes)

    # ------------------------------------------------------------------ utils
    @property
    def day(self) -> date:
        return self.start.date()

    def uid(self) -> str:
        raw = f"{self.day}|{_key(self.series)}|{_key(self.speaker)}|{_key(self.title)[:60]}"
        return hashlib.sha1(raw.encode()).hexdigest()[:12]

    def to_dict(self) -> dict:
        d = asdict(self)
        d["start"] = self.start.isoformat()
        d["end"] = self.end.isoformat() if self.end else None
        d["uid"] = self.uid()
        return d


# --------------------------------------------------------------------- helpers

def _key(s: str) -> str:
    """Clé de comparaison : sans accents, sans ponctuation, minuscules."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9 ]+", " ", s.lower()).strip()


def _similar(a: str, b: str) -> float:
    a, b = _key(a), _key(b)
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def classify(ev: Event, fields: dict[str, list[str]]) -> str:
    """Range la séance dans un champ disciplinaire d'après le nom du séminaire."""
    hay = _key(f"{ev.series} {ev.title}")
    best, best_hits = "", 0
    for name, keywords in fields.items():
        hits = sum(1 for kw in keywords if _key(kw) in hay)
        if hits > best_hits:
            best, best_hits = name, hits
    return best or "Autre"


def detect_kind(ev: Event) -> str:
    hay = _key(f"{ev.series} {ev.title}")
    if any(w in hay for w in ("workshop", "atelier", "working group", "groupe de travail",
                              "brown bag", "lunch")):
        return "workshop"
    if any(w in hay for w in ("conference", "colloque", "conferences", "forum",
                              "journee", "journees", "days", "summer school")):
        return "colloque"
    return "seminaire"


def dedupe(events: list[Event]) -> list[Event]:
    """Fusionne les séances communes à plusieurs institutions.

    Le Paris Trade Seminar, le Roy-ADRES ou le séminaire d'économétrie sont
    annoncés sur trois sites à la fois. On les regroupe sur : même jour +
    intervenant OU titre très proche.
    """
    by_day: dict[date, list[Event]] = {}
    for ev in events:
        by_day.setdefault(ev.day, []).append(ev)

    merged: list[Event] = []
    for day, group in by_day.items():
        kept: list[Event] = []
        for ev in group:
            twin = None
            for other in kept:
                same_speaker = ev.speaker and _similar(ev.speaker, other.speaker) > 0.85
                same_title = ev.title and _similar(ev.title, other.title) > 0.80
                same_series = _similar(ev.series, other.series) > 0.75
                if same_speaker or same_title or (same_series and ev.start == other.start):
                    twin = other
                    break
            if twin is None:
                kept.append(ev)
                continue
            # fusion : on garde le champ le plus informatif de chaque côté
            for attr in ("title", "speaker", "affiliation", "room", "url", "series"):
                if not getattr(twin, attr) and getattr(ev, attr):
                    setattr(twin, attr, getattr(ev, attr))
            if ev.source_id != twin.source_id and ev.source_id not in twin.also_at:
                twin.also_at.append(ev.source_id)
        merged.extend(kept)

    merged.sort(key=lambda e: (e.start, e.series))
    return merged
