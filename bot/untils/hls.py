# bot/utils/hls.py
from __future__ import annotations

import re
from typing import Dict, Any, List, Tuple
from urllib.parse import urljoin, urlparse

_MASTER_HINT = "#EXT-X-STREAM-INF"
_MEDIA_HINT = "#EXT-X-MEDIA"

_ATTR_RE = re.compile(r'(\w[\w-]*)=("[^"]*"|[^,]*)')

def is_master_playlist(text: str) -> bool:
    t = (text or "")
    return _MASTER_HINT in t

def _parse_attrs(line: str) -> Dict[str, str]:
    attrs: Dict[str, str] = {}
    for k, v in _ATTR_RE.findall(line):
        v = v.strip()
        if v.startswith('"') and v.endswith('"'):
            v = v[1:-1]
        attrs[k.upper()] = v
    return attrs

def parse_master(text: str, base_url: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Returns (variants, audios)
    variants: [{id,label,bandwidth,url}]
    audios:   [{id,label,lang,url,group_id}]
    """
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    variants: List[Dict[str, Any]] = []
    audios: List[Dict[str, Any]] = []

    # parse audios
    for ln in lines:
        if ln.startswith(_MEDIA_HINT) and "TYPE=AUDIO" in ln.upper():
            attrs = _parse_attrs(ln.split(":",1)[1] if ":" in ln else ln)
            uri = attrs.get("URI")
            if not uri:
                continue
            aurl = urljoin(base_url, uri)
            lang = attrs.get("LANGUAGE") or attrs.get("NAME") or "audio"
            name = attrs.get("NAME") or lang
            gid = attrs.get("GROUP-ID") or "audio"
            aid = f"{gid}:{name}"
            audios.append({
                "id": re.sub(r"[^a-zA-Z0-9:_-]+", "_", aid),
                "label": f"{name} ({lang})" if lang and name != lang else name,
                "lang": lang,
                "name": name,
                "group_id": gid,
                "url": aurl,
            })

    # parse variants
    i = 0
    vid = 0
    while i < len(lines):
        ln = lines[i]
        if ln.startswith(_MASTER_HINT):
            attrs = _parse_attrs(ln.split(":",1)[1] if ":" in ln else ln)
            # next line is URI
            j = i + 1
            while j < len(lines) and lines[j].startswith("#"):
                j += 1
            if j < len(lines):
                uri = lines[j]
                vurl = urljoin(base_url, uri)
                bw = attrs.get("BANDWIDTH") or ""
                res = attrs.get("RESOLUTION") or ""
                label = ""
                if res:
                    # RESOLUTION=1920x1080
                    try:
                        label = res.split("x")[1] + "p"
                    except Exception:
                        label = res
                if not label and bw:
                    try:
                        label = f"{int(bw)//1000}kbps"
                    except Exception:
                        label = bw
                vid += 1
                variants.append({
                    "id": f"v{vid}",
                    "label": label or f"variant{vid}",
                    "bandwidth": bw,
                    "url": vurl,
                    "attrs": attrs,
                })
                i = j + 1
                continue
        i += 1

    # Deduplicate by url
    seen=set()
    variants2=[]
    for v in variants:
        if v["url"] in seen: 
            continue
        seen.add(v["url"])
        variants2.append(v)
    return variants2, audios
