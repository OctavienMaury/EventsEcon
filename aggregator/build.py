"""Collecte les sources, dédoublonne, écrit le site statique."""

from __future__ import annotations

import json
import random
from datetime import datetime, timedelta
from pathlib import Path

import yaml

from .adapters import ADAPTERS
from .model import Event, classify, detect_kind, dedupe

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
TEMPLATE = Path(__file__).parent / "template.html"


def load_config(path: Path | None = None) -> dict:
    return yaml.safe_load((path or ROOT / "sources.yaml").read_text(encoding="utf-8"))


def collect(cfg: dict, only: str | None = None, verbose: bool = True):
    """Renvoie (events, rapport). Une source en échec ne bloque pas les autres."""
    events, report = [], []
    for src in cfg["sources"]:
        if only and src["id"] != only:
            continue
        fn = ADAPTERS.get(src["adapter"])
        if fn is None:
            report.append((src, 0, f"adaptateur inconnu : {src['adapter']}"))
            continue
        try:
            got = fn(src)
            got = [e for e in got if e.start and e.start > datetime.now() - timedelta(days=1)]
            events.extend(got)
            report.append((src, len(got), None))
        except Exception as exc:                      # noqa: BLE001 - on veut tout attraper
            report.append((src, 0, f"{type(exc).__name__}: {exc}"))
        if verbose:
            s, n, err = report[-1]
            mark = "!" if err else ("." if n else "0")
            print(f" {mark} {s['id']:<22} {n:>4} séance(s)" + (f"  {err}" if err else ""))
    return events, report


def finalize(events: list[Event], cfg: dict) -> list[Event]:
    for ev in events:
        ev.field = classify(ev, cfg.get("fields", {}))
        ev.kind = detect_kind(ev)
    return dedupe(events)


def write_site(events: list[Event], cfg: dict, demo: bool = False) -> None:
    DOCS.mkdir(exist_ok=True)
    payload = {
        "built": datetime.now().strftime("%d/%m/%Y à %Hh%M"),
        "demo": demo,
        "venues": cfg.get("venues", {}),
        "sources": [{"id": s["id"], "name": s["name"]} for s in cfg["sources"]],
        "events": [e.to_dict() for e in events],
    }
    (DOCS / "events.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    (DOCS / "index.html").write_text(
        TEMPLATE.read_text(encoding="utf-8").replace(
            "__DATA__", json.dumps(payload, ensure_ascii=False)),
        encoding="utf-8")
    (DOCS / "agenda.ics").write_text(to_ics(events, cfg), encoding="utf-8")


def to_ics(events: list[Event], cfg: dict) -> str:
    venues = cfg.get("venues", {})
    out = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//agenda-eco-paris//FR",
           "X-WR-CALNAME:Séminaires d'économie — Paris", "CALSCALE:GREGORIAN"]
    for e in events:
        end = e.end or (e.start + timedelta(minutes=75))
        place = ", ".join(x for x in [e.room, venues.get(e.institution, {}).get("address")] if x)
        summary = e.series + (f" — {e.speaker}" if e.speaker else "")
        out += ["BEGIN:VEVENT",
                f"UID:{e.uid()}@agenda-eco-paris",
                f"DTSTAMP:{datetime.now():%Y%m%dT%H%M%S}",
                f"DTSTART:{e.start:%Y%m%dT%H%M%S}",
                f"DTEND:{end:%Y%m%dT%H%M%S}",
                "SUMMARY:" + _esc(summary),
                "DESCRIPTION:" + _esc(" ".join(x for x in [e.title, e.affiliation, e.url] if x)),
                "LOCATION:" + _esc(place),
                "END:VEVENT"]
    out.append("END:VCALENDAR")
    return "\r\n".join(out)


def _esc(s: str) -> str:
    return (s or "").replace("\\", "\\\\").replace(",", "\\,").replace(";", "\\;").replace("\n", " ")


# ------------------------------------------------------------------ démo

def demo_events(cfg: dict) -> list[Event]:
    """Séances fictives, pour visualiser le site avant la première collecte."""
    seeds = [
        ("Macroeconomics Seminar", "PSE", "R2-01", "Exemple — collecte non lancée"),
        ("Paris Trade Seminar", "Sciences Po", "Salle H405", "Exemple — collecte non lancée"),
        ("Applied Micro Seminar", "CREST", "Salle 3001", "Exemple — collecte non lancée"),
        ("Roy Seminar (ADRES)", "PSE", "R1-09", "Exemple — collecte non lancée"),
        ("Lunch séminaire Droit et Économie", "Paris 2", "Salle des Conseils", "Exemple — collecte non lancée"),
        ("Séminaire d'économétrie", "CREST", "Salle 3001", "Exemple — collecte non lancée"),
    ]
    random.seed(3)
    base = datetime.now().replace(hour=12, minute=30, second=0, microsecond=0)
    out = []
    for i, (series, inst, room, title) in enumerate(seeds):
        start = base + timedelta(days=i + 1, hours=random.choice([0, 1, 3, 4]))
        out.append(Event(start=start, end=start + timedelta(minutes=75), series=series,
                         title=title, speaker="N. N.", affiliation="Université X",
                         room=room, institution=inst, city="Paris",
                         url="", source_id="demo"))
    return finalize(out, cfg)
