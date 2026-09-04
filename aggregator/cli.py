"""Commandes : check, run, demo, serve.

    python -m aggregator check          # teste chaque source, dit ce qui répond
    python -m aggregator run            # collecte + construit le site dans docs/
    python -m aggregator run --only pse-agenda
    python -m aggregator demo           # site avec des séances fictives
    python -m aggregator serve          # ouvre le site sur http://localhost:8000
"""

from __future__ import annotations

import argparse
import functools
import http.server
import socketserver
import sys

from .build import DOCS, collect, demo_events, finalize, load_config, write_site


def cmd_check(args):
    cfg = load_config()
    print(f"Test de {len(cfg['sources'])} sources…\n")
    _, report = collect(cfg, only=args.only)
    ok = [r for r in report if not r[2] and r[1]]
    vide = [r for r in report if not r[2] and not r[1]]
    ko = [r for r in report if r[2]]
    print(f"\n{len(ok)} source(s) exploitable(s), {len(vide)} sans séance à venir, "
          f"{len(ko)} en erreur.")
    if vide or ko:
        print("\nÀ corriger dans sources.yaml :")
        for src, _, err in vide + ko:
            print(f"  - {src['id']} ({src['url'] if 'url' in src else src.get('base')})"
                  f" : {err or 'répond mais aucune séance reconnue'}")
        print("\nPistes : l'URL a changé, ou l'adaptateur ne convient pas. "
              "Cherche d'abord un lien « iCal / Exporter / S'abonner » sur la page : "
              "l'adaptateur `ics` est toujours le plus robuste.")


def cmd_run(args):
    cfg = load_config()
    events, report = collect(cfg, only=args.only)
    events = finalize(events, cfg)
    write_site(events, cfg)
    erreurs = sum(1 for r in report if r[2])
    print(f"\n{len(events)} séances après dédoublonnage → {DOCS}/index.html"
          + (f"  ({erreurs} source(s) en erreur, voir ci-dessus)" if erreurs else ""))


def cmd_demo(args):
    cfg = load_config()
    write_site(demo_events(cfg), cfg, demo=True)
    print(f"Site de démonstration écrit dans {DOCS}/index.html")


def cmd_serve(args):
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(DOCS))
    with socketserver.TCPServer(("", args.port), handler) as httpd:
        print(f"http://localhost:{args.port}  (Ctrl+C pour arrêter)")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass


def main(argv=None):
    p = argparse.ArgumentParser(prog="aggregator", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("check", help="tester les sources")
    c.add_argument("--only", help="un seul identifiant de source")
    c.set_defaults(func=cmd_check)

    r = sub.add_parser("run", help="collecter et construire le site")
    r.add_argument("--only", help="un seul identifiant de source")
    r.set_defaults(func=cmd_run)

    d = sub.add_parser("demo", help="site avec des séances fictives")
    d.set_defaults(func=cmd_demo)

    s = sub.add_parser("serve", help="servir docs/ en local")
    s.add_argument("--port", type=int, default=8000)
    s.set_defaults(func=cmd_serve)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
