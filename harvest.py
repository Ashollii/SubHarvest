#!/usr/bin/env python3
"""SubHarvest — harvest fresh V2Ray configs and MTProto proxies from the
public previews (https://t.me/s/<channel>) of Telegram channels.

Outputs (in output/):
  configs.txt   one config URI per line, deduplicated
  proxies.txt   one https://t.me/proxy link per line, deduplicated
  sub.txt       subscription file (configs + profile header lines)
  sub.b64.txt   the same subscription, base64-encoded
  stats.json    counts + per-channel results + timestamp

Stdlib only. If the SUBHARVEST_PROXY environment variable is set
(e.g. http://proxy:3128), all traffic goes through that proxy and
truncated reads are resumed with HTTP Range requests.
"""
import base64
import concurrent.futures
import datetime
import html
import ipaddress
import json
import os
import re
import urllib.parse
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
CHANNELS_PATH = os.path.join(BASE, "channels.txt")
OUT_DIR = os.path.join(BASE, "output")
PROXY = os.environ.get("SUBHARVEST_PROXY", "").strip()

if PROXY:
    _opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": PROXY, "https": PROXY}))
else:
    _opener = urllib.request.build_opener()

_CFG_RE = re.compile(
    r"(?:vless|vmess|trojan|ssr|ss|hy2|hysteria2|hysteria|tuic|"
    r"wireguard|slipnet)://[^\s<>\"']+")
_PROXY_RE = re.compile(r"(?:https://t\.me/proxy|tg://proxy)\?[^\s<>\"']+")
_POST_RE = re.compile(r'data-post="([A-Za-z0-9_]+)/(\d+)"')

SUB_HEADERS = [
    "#profile-title: ASHKAN",
    "#profile-update-interval: 1",
    "#subscription-userinfo: upload=0; download=0; total=0; expire=0",
    "#support-url: https://t.me/A5HKANI",
    "#profile-web-page-url: https://github.com/Ashollii/SubHarvest",
]


def load_channels():
    try:
        with open(CHANNELS_PATH, encoding="utf-8") as f:
            return [ln.strip().lstrip("@") for ln in f
                    if ln.strip() and not ln.startswith("#")]
    except Exception:
        return []


def _clean(u):
    return html.unescape(u).rstrip(").,;!?…")


def _fetch(url, timeout=25, tries=8):
    """Read a whole page, resuming truncated reads with Range requests."""
    buf = b""
    total = None
    for _ in range(tries):
        headers = {"User-Agent": "SubHarvest/1.0"}
        if buf:
            headers["Range"] = f"bytes={len(buf)}-"
        req = urllib.request.Request(url, headers=headers)
        try:
            with _opener.open(req, timeout=timeout) as r:
                cr = r.headers.get("Content-Range") or ""
                if "/" in cr:
                    try:
                        total = int(cr.rsplit("/", 1)[1])
                    except ValueError:
                        pass
                elif r.headers.get("Content-Length"):
                    try:
                        total = int(r.headers["Content-Length"])
                    except ValueError:
                        pass
                chunk = r.read()
                if r.status == 200 and buf and "Range" in headers:
                    buf = chunk  # Range ignored; start over
                else:
                    buf += chunk
        except Exception as e:
            partial = getattr(e, "partial", None)
            if partial:
                buf = buf + partial if "Range" in headers else partial
            elif not buf:
                raise
        if total is not None and len(buf) >= total:
            break
        if total is None and buf:
            break
    return buf


def _fetch_channel(username):
    """(username, configs, proxies, error)"""
    url = f"https://t.me/s/{username}"
    try:
        page = _fetch(url).decode("utf-8", "replace")
    except Exception as e:
        return username, [], [], f"{type(e).__name__}: {e}"
    if "tgme_widget_message" not in page:
        return username, [], [], "no public preview"
    configs = [_clean(u) for u in _CFG_RE.findall(page)]
    proxies = []
    for u in _PROXY_RE.findall(page):
        u = _clean(u)
        if u.startswith("tg://proxy?"):
            u = "https://t.me/proxy?" + u[len("tg://proxy?"):]
        proxies.append(u)
    return username, configs, proxies, None


def _dedupe(items):
    seen, out = set(), []
    for it in items:
        if it not in seen:
            seen.add(it)
            out.append(it)
    return out


# --- ASHKAN naming (same scheme as the main SUB) -------------------------
# Every config in the subscription files is renamed to
# "ASHKAN-<n> <country flag or 🌐> <IPv4|IPv6>". The flag is taken from the
# config's original name; when no country can be identified the generic
# 🌐 marker is used. configs.txt keeps the original names untouched.

def _b64url_nopad(data):
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


_KNOWN_CC = {'US', 'GB', 'UK', 'DE', 'NL', 'FR', 'CA', 'IR', 'FI', 'SE', 'NO', 'DK', 'RU', 'HK', 'TR', 'AE', 'SG', 'JP', 'KR', 'IN', 'IT', 'ES', 'CH', 'AT', 'PL', 'UA', 'CZ', 'RO', 'BG', 'ME', 'SC', 'BE', 'IE', 'LU', 'EE', 'LV', 'LT', 'GR', 'PT', 'HU', 'SK', 'SI', 'HR', 'RS', 'AL', 'KZ', 'UZ', 'AZ', 'GE', 'AM', 'IQ', 'SA', 'QA', 'OM', 'KW', 'BH', 'EG', 'ZA', 'NG', 'KE', 'BR', 'MX', 'AR', 'CL', 'CO', 'PE', 'AU', 'NZ', 'ID', 'MY', 'TH', 'VN', 'PH', 'TW', 'CN', 'PK', 'BD', 'LK', 'MM', 'KH'}
_COUNTRY_NAMES = {'germany': 'DE', 'netherlands': 'NL', 'holland': 'NL', 'france': 'FR', 'canada': 'CA', 'united states': 'US', 'america': 'US', 'usa': 'US', 'united kingdom': 'GB', 'england': 'GB', 'uk': 'GB', 'iran': 'IR', 'finland': 'FI', 'sweden': 'SE', 'russia': 'RU', 'hong kong': 'HK', 'turkey': 'TR', 'singapore': 'SG', 'japan': 'JP', 'korea': 'KR', 'india': 'IN', 'italy': 'IT', 'spain': 'ES', 'switzerland': 'CH', 'austria': 'AT', 'poland': 'PL', 'ukraine': 'UA', 'romania': 'RO', 'bulgaria': 'BG', 'montenegro': 'ME', 'seychelles': 'SC'}


def _flag_of_code(cc):
    cc = 'GB' if cc == 'UK' else cc
    if len(cc) != 2 or not cc.isalpha():
        return ''
    return ''.join(chr(0x1F1E6 + ord(c) - 65) for c in cc.upper())


def _host_of(uri):
    proto = uri.split('://', 1)[0]
    rest = uri.split('://', 1)[1] if '://' in uri else ''
    if proto == 'vmess':
        try:
            padded = rest.split('#', 1)[0]
            padded += '=' * (-len(padded) % 4)
            return str(json.loads(base64.b64decode(padded)).get('add', ''))
        except Exception:
            return ''
    rest = rest.split('#', 1)[0]
    if '@' in rest:
        rest = rest.rsplit('@', 1)[1]
    rest = rest.split('?', 1)[0].split('/', 1)[0]
    if rest.startswith('['):
        return rest[1:].split(']', 1)[0]
    return rest.rsplit(':', 1)[0] if ':' in rest else rest


def _ip_version(uri):
    host = _host_of(uri)
    try:
        return 'IPv6' if ipaddress.ip_address(host).version == 6 else 'IPv4'
    except ValueError:
        return 'IPv6' if ':' in host else 'IPv4'


def _original_name(uri):
    proto = uri.split('://', 1)[0].lower() if '://' in uri else ''
    if proto == 'vmess':
        payload = uri.split('://', 1)[1].split('#', 1)[0]
        try:
            padded = payload + '=' * (-len(payload) % 4)
            return str(json.loads(base64.b64decode(
                padded, altchars=b'-_')).get('ps', ''))
        except Exception:
            return ''
    if '#' in uri:
        return urllib.parse.unquote(uri.split('#', 1)[1])
    return ''


def _extract_flag(name):
    if not name:
        return ''
    m = re.search(r'[\U0001F1E6-\U0001F1FF]{2}', name)
    if m:
        f = m.group(0)
        code = ''.join(chr(ord(c) - 0x1F1E6 + 65) for c in f)
        return f if code != 'XX' else ''
    low = ' ' + name.lower() + ' '
    for cname, cc in _COUNTRY_NAMES.items():
        if re.search(r'[^a-z]' + re.escape(cname) + r'[^a-z]', low):
            return _flag_of_code(cc)
    for tok in re.findall(r'(?<![A-Za-z])[A-Z]{2}(?![A-Za-z])', name):
        if tok in _KNOWN_CC:
            return _flag_of_code(tok)
    return ''


def _rename_uri(uri, name):
    proto = uri.split('://', 1)[0].lower() if '://' in uri else ''
    if proto == 'vmess':
        payload = uri.split('://', 1)[1].split('#', 1)[0]
        try:
            padded = payload + '=' * (-len(payload) % 4)
            obj = json.loads(base64.b64decode(padded, altchars=b'-_'))
            obj['ps'] = name
            raw = json.dumps(obj, ensure_ascii=False,
                             separators=(',', ':')).encode()
            return 'vmess://' + base64.b64encode(raw).decode()
        except Exception:
            return (uri.split('#', 1)[0] + '#' +
                    urllib.parse.quote(name, safe=''))
    if proto == 'ssr':
        payload = uri.split('://', 1)[1].split('#', 1)[0]
        try:
            padded = payload + '=' * (-len(payload) % 4)
            decoded = base64.b64decode(
                padded, altchars=b'-_').decode('utf-8', 'ignore')
            main, _, query = decoded.partition('/?')
            params, found = [], False
            for part in (query.split('&') if query else []):
                if part.startswith('remarks='):
                    params.append('remarks=' + _b64url_nopad(name.encode()))
                    found = True
                elif part:
                    params.append(part)
            if not found:
                params.append('remarks=' + _b64url_nopad(name.encode()))
            rebuilt = main + '/?' + '&'.join(params)
            return 'ssr://' + _b64url_nopad(rebuilt.encode())
        except Exception:
            return uri
    base = uri.split('#', 1)[0]
    return base + '#' + urllib.parse.quote(name, safe='')


def rename_all(configs):
    out = []
    for idx, u in enumerate(configs, 1):
        flag = _extract_flag(_original_name(u)) or '\U0001F310'
        out.append(_rename_uri(u, f"ASHKAN-{idx} {flag} {_ip_version(u)}"))
    return out


def main():
    channels = load_channels()
    all_cfg, all_prx, per_channel = [], [], {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        for username, cfgs, prxs, err in ex.map(_fetch_channel, channels):
            per_channel[username] = {
                "configs": len(set(cfgs)), "proxies": len(set(prxs)),
                "error": err}
            all_cfg.extend(cfgs)
            all_prx.extend(prxs)
    configs = _dedupe(all_cfg)
    proxies = _dedupe(all_prx)

    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, "configs.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(configs) + ("\n" if configs else ""))
    with open(os.path.join(OUT_DIR, "proxies.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(proxies) + ("\n" if proxies else ""))

    sub_text = "\n".join(SUB_HEADERS + rename_all(configs)) + "\n"
    with open(os.path.join(OUT_DIR, "sub.txt"), "w", encoding="utf-8") as f:
        f.write(sub_text)
    with open(os.path.join(OUT_DIR, "sub.b64.txt"), "w", encoding="utf-8") as f:
        f.write(base64.b64encode(sub_text.encode()).decode() + "\n")

    stats = {
        "updated_at_utc": datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%d %H:%M UTC"),
        "channels": len(channels),
        "configs": len(configs),
        "proxies": len(proxies),
        "per_channel": per_channel,
    }
    with open(os.path.join(OUT_DIR, "stats.json"), "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=1)
    print(f"channels={len(channels)} configs={len(configs)} "
          f"proxies={len(proxies)}")
    for name, st in per_channel.items():
        if st["error"]:
            print(f"  ! {name}: {st['error']}")


if __name__ == "__main__":
    main()
