"""
SharePoint Audio Downloader per UniBo.

Scarica l'audio delle lezioni da SharePoint (anche con download disabilitato).
Intercetta il manifest DASH, scarica i segmenti audio, decripta e converte in FLAC.

Uso:
    uv run main.py
    uv run main.py --urls URL1 URL2 ...
    uv run main.py --urls-file lista.txt
"""

import asyncio
import html
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import unquote, parse_qs, urlparse

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from playwright.async_api import async_playwright

import httpx

DOWNLOAD_DIR = Path("downloads")
BROWSER_DIR = Path(".browser_data")
BASE_URL = "https://liveunibo.sharepoint.com"
DASH_NS = "urn:mpeg:DASH:schema:MPD:2011"
SEA_NS = "urn:mpeg:dash:schema:sea:2012"
MAX_CONCURRENT = 15


async def main():
    DOWNLOAD_DIR.mkdir(exist_ok=True)

    print("=" * 55)
    print("  SharePoint Audio Downloader - UniBo")
    print("=" * 55)

    urls = _parse_args()

    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            user_data_dir=str(BROWSER_DIR.absolute()),
            headless=False,
            viewport={"width": 1280, "height": 720},
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        print("\n[1/3] Apro il browser per il login...")
        await page.goto(BASE_URL)
        input("\n  >>> Effettua il login nel browser, poi premi INVIO qui: ")

        if not urls:
            urls = _ask_urls()
        if not urls:
            print("\nNessun URL inserito. Uscita.")
            await ctx.close()
            return

        print(f"\n[2/3] Trovati {len(urls)} URL da scaricare")
        print(f"\n[3/3] Inizio download...\n")

        ok_count = 0
        for i, url in enumerate(urls, 1):
            print(f"--- Lezione {i}/{len(urls)} ---")
            success = await download_audio(page, ctx, url)
            if success:
                ok_count += 1
            print()

        print("=" * 55)
        print(f"  Completato: {ok_count}/{len(urls)} audio scaricati")
        print(f"  Cartella: {DOWNLOAD_DIR.absolute()}")
        print("=" * 55)
        await ctx.close()


# ---------------------------------------------------------------------------
# Main download flow
# ---------------------------------------------------------------------------

async def download_audio(page, ctx, url: str) -> bool:
    """Scarica l'audio di un video SharePoint come FLAC."""

    file_path = _extract_file_path(url)
    if not file_path:
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            await asyncio.sleep(2)
            file_path = _extract_file_path(page.url)
        except Exception as e:
            print(f"  Errore navigazione: {e}")
            return False

    if not file_path:
        file_path = await _extract_file_path_from_page(page)

    if not file_path:
        print("  ERRORE: impossibile trovare il percorso del file.")
        return False

    base_name = _sanitize_filename(Path(unquote(file_path)).stem)
    dest = DOWNLOAD_DIR / f"{base_name}.flac"
    print(f"  Output: {dest.name}")

    if dest.exists() and dest.stat().st_size > 10_000:
        print(f"  Gia' scaricato ({dest.stat().st_size / 1024 / 1024:.1f} MB), salto.")
        return True

    # --- Intercetta il manifest DASH dal traffico di rete ---
    captured = []  # (url, content_type)

    def on_response(response):
        rurl = response.url
        ct = response.headers.get("content-type", "").lower()
        if response.status in (200, 206):
            if any(x in ct for x in ["dash+xml", "video/", "octet-stream"]) or \
               any(x in rurl.lower() for x in ["videomanifest", "tempauth", ".mpd"]):
                if "stream.aspx" not in rurl:
                    captured.append((rurl, ct))

    page.on("response", on_response)

    try:
        # Naviga alla pagina se necessario
        current = page.url
        if file_path not in unquote(current):
            print(f"  Navigo alla pagina del video...")
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            except Exception:
                pass  # anche se va in timeout, la pagina potrebbe essere caricata
            await asyncio.sleep(3)

        # Avvia la riproduzione per far caricare il manifest
        print(f"  Attendo manifest DASH...")
        for sel in ['button[aria-label*="Play"]', 'button[aria-label*="play"]',
                    'button[aria-label*="Riproduci"]', '.vjs-big-play-button']:
            try:
                btn = page.locator(sel)
                if await btn.count() > 0:
                    await btn.first.click()
                    break
            except Exception:
                continue

        # Aspetta che il manifest venga caricato
        for _ in range(20):
            await asyncio.sleep(1)
            if captured:
                break

    finally:
        page.remove_listener("response", on_response)

    if not captured:
        print("  ERRORE: nessun manifest DASH catturato.")
        return False

    # --- Scarica il manifest e controlla se e' MPD ---
    mpd_content = None
    cookies = await ctx.cookies()

    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=httpx.Timeout(connect=30, read=120, write=30, pool=30),
    ) as client:
        _apply_cookies(client, cookies)

        for cap_url, cap_ct in captured:
            try:
                resp = await client.get(cap_url)
                if resp.status_code != 200:
                    continue
                text = resp.text
                if "<MPD" in text[:500]:
                    mpd_content = text
                    print(f"  Manifest DASH trovato!")
                    break
            except Exception:
                continue

    if not mpd_content:
        print("  ERRORE: impossibile ottenere il manifest DASH.")
        return False

    # --- Parse MPD e scarica audio ---
    return await _download_dash_audio(mpd_content, dest, cookies)


# ---------------------------------------------------------------------------
# DASH audio download
# ---------------------------------------------------------------------------

async def _download_dash_audio(mpd_content: str, dest: Path, cookies) -> bool:
    """Parsa il manifest DASH e scarica solo la traccia audio."""

    root = ET.fromstring(mpd_content)
    base_url = root.findtext(f"{{{DASH_NS}}}BaseURL", "")
    period = root.find(f"{{{DASH_NS}}}Period")

    # Trova la traccia audio (preferisci OriginalAudio)
    audio_track = None
    for adapt in period.findall(f"{{{DASH_NS}}}AdaptationSet"):
        if adapt.get("contentType") != "audio":
            continue
        label_el = adapt.find(f"{{{DASH_NS}}}Label")
        label = label_el.text if label_el is not None else "audio"

        track = _parse_adaptation_set(adapt, base_url)
        if not audio_track or label == "OriginalAudio":
            audio_track = track
            audio_track["label"] = label

    if not audio_track:
        print("  ERRORE: nessuna traccia audio nel manifest.")
        return False

    n_segments = len(audio_track["segment_times"])
    print(f"  Traccia: {audio_track['label']} ({n_segments} segmenti)")

    # --- Scarica chiave di decriptazione ---
    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=httpx.Timeout(connect=30, read=120, write=30, pool=30),
    ) as client:
        _apply_cookies(client, cookies)

        print(f"  Scarico chiave di decriptazione...")
        try:
            key_resp = await client.get(audio_track["key_url"])
            if key_resp.status_code != 200:
                print(f"    ERRORE: HTTP {key_resp.status_code}")
                return False
            key = key_resp.content
        except Exception as e:
            print(f"    ERRORE: {e}")
            return False

        iv = audio_track["iv"]

        # --- Scarica init segment ---
        print(f"  Scarico init segment...")
        init_resp = await client.get(audio_track["init_url"])
        if init_resp.status_code != 200:
            print(f"    ERRORE: HTTP {init_resp.status_code}")
            return False

        init_data = _decrypt(init_resp.content, key, iv)

        # --- Scarica tutti i segmenti audio ---
        temp_fmp4 = dest.with_suffix(".fmp4.tmp")
        sem = asyncio.Semaphore(MAX_CONCURRENT)

        async def fetch_segment(seg_time):
            seg_url = audio_track["media_tpl"].replace("$Time$", str(seg_time))
            async with sem:
                for attempt in range(3):
                    try:
                        r = await client.get(seg_url)
                        if r.status_code == 200:
                            return _decrypt(r.content, key, iv)
                    except Exception:
                        if attempt < 2:
                            await asyncio.sleep(1)
                return None

        print(f"  Scarico segmenti audio...")
        with open(temp_fmp4, "wb") as f:
            f.write(init_data)

            batch_size = 30
            for batch_start in range(0, n_segments, batch_size):
                batch_times = audio_track["segment_times"][batch_start:batch_start + batch_size]
                results = await asyncio.gather(*[fetch_segment(t) for t in batch_times])

                for data in results:
                    if data:
                        f.write(data)

                done = min(batch_start + batch_size, n_segments)
                print(f"\r    {done}/{n_segments} segmenti...", end="", flush=True)

        fmp4_mb = temp_fmp4.stat().st_size / (1024 * 1024)
        print(f"\r    {n_segments}/{n_segments} segmenti - {fmp4_mb:.1f} MB audio scaricato")

    # --- Converti in FLAC (mono 16kHz, ottimo per trascrizione) ---
    print(f"  Converto in FLAC (mono 16kHz)...")
    proc = subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", str(temp_fmp4),
            "-ac", "1",           # mono
            "-ar", "16000",       # 16kHz (gia' nativo)
            "-sample_fmt", "s16", # 16-bit
            str(dest),
        ],
        capture_output=True,
        text=True,
    )

    temp_fmp4.unlink(missing_ok=True)

    if proc.returncode != 0:
        print(f"    ERRORE ffmpeg: {proc.stderr[:300]}")
        return False

    size_mb = dest.stat().st_size / (1024 * 1024)
    print(f"  Salvato: {dest.name} ({size_mb:.1f} MB)")
    return True


def _parse_adaptation_set(adapt, base_url: str) -> dict:
    """Estrae info da un AdaptationSet del manifest DASH."""
    cp = adapt.find(f"{{{DASH_NS}}}ContentProtection")
    crypto = cp.find(f"{{{SEA_NS}}}CryptoPeriod") if cp is not None else None

    key_url = html.unescape(crypto.get("keyUriTemplate", "")) if crypto is not None else ""
    iv_hex = crypto.get("IV", "0x00000000000000000000000000000000") if crypto is not None else ""
    iv = bytes.fromhex(iv_hex.replace("0x", ""))

    seg_tpl = adapt.find(f"{{{DASH_NS}}}SegmentTemplate")
    init_tpl = html.unescape(seg_tpl.get("initialization", ""))
    media_tpl = html.unescape(seg_tpl.get("media", ""))

    rep = adapt.find(f"{{{DASH_NS}}}Representation")
    rep_id = rep.get("id", "")

    timeline = seg_tpl.find(f"{{{DASH_NS}}}SegmentTimeline")
    times = []
    t = 0
    for s_elem in timeline.findall(f"{{{DASH_NS}}}S"):
        d = int(s_elem.get("d"))
        r = int(s_elem.get("r", "0"))
        for _ in range(r + 1):
            times.append(t)
            t += d

    return {
        "key_url": key_url,
        "iv": iv,
        "init_url": base_url + init_tpl.replace("$RepresentationID$", rep_id),
        "media_tpl": base_url + media_tpl.replace("$RepresentationID$", rep_id),
        "segment_times": times,
    }


def _decrypt(data: bytes, key: bytes, iv: bytes) -> bytes:
    """Decripta un segmento con AES-128-CBC."""
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    dec = cipher.decryptor()
    result = dec.update(data) + dec.finalize()
    # PKCS7 unpadding manuale
    if result:
        pad = result[-1]
        if 1 <= pad <= 16 and all(b == pad for b in result[-pad:]):
            result = result[:-pad]
    return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _apply_cookies(client: httpx.AsyncClient, pw_cookies: list):
    """Applica i cookie di Playwright al client httpx."""
    for c in pw_cookies:
        client.cookies.set(c["name"], c["value"], domain=c.get("domain", "").lstrip("."))


def _extract_file_path(url: str) -> str | None:
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    if "id" in params:
        return params["id"][0]
    path = unquote(parsed.path)
    if re.search(r"\.(mp4|mkv|webm|avi|mov)$", path, re.IGNORECASE):
        return path
    return None


async def _extract_file_path_from_page(page) -> str | None:
    return await page.evaluate("""() => {
        const params = new URLSearchParams(window.location.search);
        const id = params.get('id');
        if (id) return id;
        const video = document.querySelector('video');
        if (video) {
            const src = video.src || video.currentSrc;
            if (src) {
                try {
                    const u = new URL(src);
                    if (u.pathname.match(/\\.(mp4|mkv|webm|avi|mov)$/i)) return u.pathname;
                } catch(e) {}
            }
        }
        return null;
    }""")


def _parse_args() -> list[str]:
    args = sys.argv[1:]
    urls = []
    if "--urls" in args:
        idx = args.index("--urls") + 1
        while idx < len(args) and not args[idx].startswith("--"):
            urls.append(args[idx])
            idx += 1
    if "--urls-file" in args:
        idx = args.index("--urls-file") + 1
        if idx < len(args):
            path = Path(args[idx])
            if path.exists():
                urls.extend(
                    line.strip()
                    for line in path.read_text(encoding="utf-8").splitlines()
                    if line.strip() and not line.strip().startswith("#")
                )
    return urls


def _ask_urls() -> list[str]:
    print("\n  Inserisci gli URL dei video (uno per riga, INVIO vuoto per terminare)")
    print("  Formati supportati:")
    print("    - .../stream.aspx?id=/sites/.../video.mp4")
    print("    - .../:v:/s/NomeCorso/...\n")
    urls = []
    while True:
        try:
            raw = input("  URL> ").strip()
        except EOFError:
            break
        if not raw:
            break
        urls.append(raw)
    return urls


def _sanitize_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*]', "_", name)
    name = name.strip(". ")
    return name[:200] if name else "audio"


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\nInterrotto.")
        sys.exit(1)
