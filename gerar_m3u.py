#!/usr/bin/env python3
# TV+ - agregador incremental de M3U para SS IPTV
import concurrent.futures
import hashlib
import json
import logging
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "tvplus_state.json"
OUTPUT = ROOT / "TV+.m3u"
SOURCES_FILE = ROOT / "fontes.txt"

EPG_URL = "https://raw.githubusercontent.com/iptv-com/epg/main/guides/brazil.xml"
USER_AGENT = "TVplus/1.0 SSIPTV M3U Aggregator"

# Após 3 atualizações consecutivas sem validação, o canal é removido.
# Assim, uma falha temporária de uma fonte não apaga um canal que já funcionava.
FAIL_LIMIT = 3
MAX_WORKERS = 24
HTTP_TIMEOUT = (5, 9)
MAX_PLAYLIST_BYTES = 2_000_000

CATEGORY_RULES = {
    "news": "Notícias", "noticias": "Notícias", "notícia": "Notícias",
    "jornalismo": "Notícias", "news & information": "Notícias",
    "sports": "Esportes", "sport": "Esportes", "esporte": "Esportes",
    "entretenimento": "Entretenimento", "entertainment": "Entretenimento",
    "variedades": "Entretenimento", "general": "Geral", "tv": "Geral",
    "cultura": "Cultura", "culture": "Cultura", "educação": "Educação",
    "education": "Educação", "religioso": "Religioso", "religious": "Religioso",
    "kids": "Infantil", "children": "Infantil", "animation": "Infantil",
    "animação": "Infantil", "documentary": "Documentários",
    "documentários": "Documentários", "music": "Música", "música": "Música",
    "lifestyle": "Estilo de Vida", "family": "Família",
    "public": "Públicos", "público": "Públicos", "government": "Públicos",
    "regional": "Regionais", "local": "Regionais",
    "religion": "Religioso",
}

BAD_NAME = re.compile(r"\b(ao vivo|live|online|grátis|gratis|programação|programacao)\b", re.I)
BAD_URL = re.compile(r"(^https?://(www\.)?youtube\.com/|youtu\.be/|\.html?(?:$|[?#]))", re.I)
STREAM_EXT = re.compile(r"\.(m3u8|m3u|ts|mpd)(?:$|[?#])", re.I)

session = requests.Session()
session.headers.update({"User-Agent": USER_AGENT, "Accept": "*/*"})

def norm_text(s):
    s = (s or "").replace("&amp;", "&").strip()
    s = re.sub(r"\s+", " ", s)
    return s

def canonical_category(group):
    g = norm_text(group).lower()
    for key, value in CATEGORY_RULES.items():
        if key == g or key in g:
            return value
    return norm_text(group) or "Geral"

def clean_name(name):
    n = norm_text(name)
    n = re.sub(r"\s*\|\s*(AM Canais|KE TV|MultiRádio Web TV|Rádios e Web TVs|TVAOVIVO\.VIP)\s*$", "", n, flags=re.I)
    n = re.sub(r"\s+ao vivo(?: grátis)? online.*$", "", n, flags=re.I)
    n = re.sub(r"\s+\((?:1080p|720p|480p|360p|SD|HD|Not 24/7|Geo-blocked)[^)]*\)", "", n, flags=re.I)
    n = re.sub(r"\s+\[(?:Not 24/7|Geo-blocked)[^\]]*\]", "", n, flags=re.I)
    n = BAD_NAME.sub("", n)
    n = re.sub(r"\s*[—–-]\s*$", "", n)
    n = re.sub(r"\s{2,}", " ", n).strip(" -|")
    return n or "Canal"

def slug_id(name):
    x = re.sub(r"[^a-z0-9]+", "", name.lower())
    return (x[:48] or "canal") + ".br"

def url_key(url):
    # Não remove query strings: tokens podem ser necessários para o stream.
    return hashlib.sha256(url.strip().encode()).hexdigest()[:24]

def parse_m3u(text, source):
    lines = text.splitlines()
    found = []
    current = None
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if line.startswith("#EXTINF"):
            current = line
        elif current and not line.startswith("#"):
            url = line.strip()
            if url.startswith(("http://", "https://")):
                found.append(parse_entry(current, url, source))
            current = None
    return found

def parse_attr(extinf, attr):
    m = re.search(rf'{re.escape(attr)}="([^"]*)"', extinf, re.I)
    return norm_text(m.group(1)) if m else ""

def parse_entry(extinf, url, source):
    group = parse_attr(extinf, "group-title") or "Geral"
    tvgid = parse_attr(extinf, "tvg-id")
    logo = parse_attr(extinf, "tvg-logo")
    name = extinf.split(",", 1)[1].strip() if "," in extinf else parse_attr(extinf, "tvg-name")
    name = clean_name(name)
    return {
        "name": name,
        "url": url,
        "group": canonical_category(group),
        "tvg_id": tvgid or slug_id(name),
        "logo": logo,
        "source": source,
    }

def direct_candidate(url):
    if BAD_URL.search(url):
        return False
    return bool(STREAM_EXT.search(url)) or any(x in url.lower() for x in (
        "/hls/", "playlist", "chunklist", "/live/", "/stream", ".ts?"
    ))

def fetch_source(url):
    try:
        r = session.get(url, timeout=20)
        r.raise_for_status()
        if len(r.content) > MAX_PLAYLIST_BYTES:
            return []
        return parse_m3u(r.text, url)
    except Exception as e:
        logging.warning("Fonte indisponível: %s (%s)", url, e)
        return []

def stream_ok(item):
    url = item["url"]
    try:
        r = session.get(
            url,
            headers={"Range": "bytes=0-4095", "User-Agent": USER_AGENT},
            timeout=HTTP_TIMEOUT,
            stream=True,
            allow_redirects=True,
        )
        if r.status_code not in (200, 206):
            return False
        data = next(r.iter_content(4096), b"") or b""
        ct = (r.headers.get("content-type") or "").lower()
        r.close()
        if b"#EXTM3U" in data[:4096] or "mpegurl" in ct or "application/vnd.apple.mpegurl" in ct:
            return True
        if url.lower().endswith(".ts") or "video/" in ct or "octet-stream" in ct:
            return len(data) > 100
        return len(data) > 40
    except Exception:
        return False

def load_state():
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}

def save_state(state):
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)

def make_line(item):
    attrs = [f'tvg-id="{item["tvg_id"]}"']
    if item.get("logo"):
        attrs.append(f'tvg-logo="{item["logo"]}"')
    attrs.append(f'tvg-name="{item["name"]}"')
    attrs.append(f'group-title="{item["group"]}"')
    return "#EXTINF:-1 " + " ".join(attrs) + "," + item["name"] + "\n" + item["url"]

def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    if not SOURCES_FILE.exists():
        raise SystemExit("fontes.txt não encontrado")

    sources = [x.strip() for x in SOURCES_FILE.read_text(encoding="utf-8").splitlines()
               if x.strip() and not x.lstrip().startswith("#")]

    all_items = []
    for i, src in enumerate(sources, 1):
        items = fetch_source(src)
        logging.info("Fonte %02d/%02d: %d entradas | %s", i, len(sources), len(items), src)
        all_items.extend(items)

    # Primeiro dedupe pela URL. O melhor nome/logo/tvg-id vence.
    by_url = {}
    for item in all_items:
        if not direct_candidate(item["url"]):
            continue
        k = item["url"].strip()
        if k not in by_url:
            by_url[k] = item
        else:
            old = by_url[k]
            # Prefere metadados mais completos e nomes mais curtos/limpos.
            if len(item["name"]) < len(old["name"]) or not old.get("logo"):
                by_url[k] = {**old, **{k2: v for k2, v in item.items() if v}}

    candidates = list(by_url.values())
    logging.info("Entradas diretas únicas antes do teste: %d", len(candidates))

    # Validação concorrente, sem testar duas vezes a mesma URL.
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        statuses = list(ex.map(stream_ok, candidates))

    state = load_state()
    next_state = {}
    survivors = []

    for item, ok in zip(candidates, statuses):
        k = url_key(item["url"])
        old = state.get(k, {})
        if ok:
            rec = {**item, "fails": 0, "last_ok": int(time.time())}
            next_state[k] = rec
            survivors.append(item)
        else:
            fails = int(old.get("fails", 0)) + 1
            # Preserva somente um canal que já tenha sido validado anteriormente.
            if old.get("last_ok") and fails < FAIL_LIMIT:
                rec = {**old, "fails": fails}
                next_state[k] = rec
                survivors.append({
                    "name": old["name"], "url": old["url"], "group": old["group"],
                    "tvg_id": old.get("tvg_id") or slug_id(old["name"]),
                    "logo": old.get("logo", ""), "source": old.get("source", ""),
                })
            else:
                # Mantém estado de falha apenas até o limite; depois desaparece.
                if fails < FAIL_LIMIT:
                    next_state[k] = {**item, "fails": fails, "last_ok": old.get("last_ok", 0)}

    # Se uma fonte inteira cair, preservamos canais já conhecidos dessa fonte.
    known_urls = {x["url"] for x in survivors}
    for k, old in state.items():
        if old.get("url") in known_urls:
            continue
        if old.get("last_ok") and int(old.get("fails", 0)) < FAIL_LIMIT:
            next_state[k] = {**old, "fails": int(old.get("fails", 0)) + 1}
            survivors.append({
                "name": old["name"], "url": old["url"], "group": old["group"],
                "tvg_id": old.get("tvg_id") or slug_id(old["name"]),
                "logo": old.get("logo", ""), "source": old.get("source", ""),
            })

    # Dedupe final por URL e por identidade forte nome+URL.
    final = {}
    for x in survivors:
        final[x["url"]] = x
    survivors = list(final.values())
    survivors.sort(key=lambda x: (x["group"].lower(), x["name"].lower()))

    header = (
        '#EXTM3U x-tvg-url="' + EPG_URL + '" '
        'refresh="6" '
        'url-tvg="' + EPG_URL + '"\n'
        '# TV+ | SS IPTV | atualização automática a cada 6 horas\n'
    )
    body = "\n".join(make_line(x) for x in survivors)
    OUTPUT.write_text(header + body + "\n", encoding="utf-8")
    save_state(next_state)

    logging.info("FINAL: %d canais | %d URLs únicas | %s", len(survivors), len(final), OUTPUT)
    if not survivors:
        raise SystemExit("Nenhum canal válido. Playlist anterior não deve ser substituída.")

if __name__ == "__main__":
    main()
