"""
Conversione FLAC -> MP3.

Converte tutti i file .flac nella cartella downloads/ in MP3 (192kbps di default).
Gli MP3 vengono salvati in downloads/mp3/.

Uso:
    uv run convert_mp3.py                # converte tutti i FLAC
    uv run convert_mp3.py --bitrate 128  # bitrate personalizzato (kbps)
    uv run convert_mp3.py --delete       # elimina i FLAC dopo la conversione
"""

import shutil
import subprocess
import sys
from pathlib import Path

DOWNLOADS_DIR = Path(__file__).parent / "downloads"
MP3_DIR = DOWNLOADS_DIR / "mp3"
DEFAULT_BITRATE = 192  # kbps


def _find_ffmpeg() -> str:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        print("ERRORE: ffmpeg non trovato nel PATH.")
        print("Installalo da: https://ffmpeg.org/download.html")
        sys.exit(1)
    return ffmpeg


def convert_flac_to_mp3(ffmpeg: str, flac_path: Path, bitrate: int) -> bool:
    """Converte un singolo FLAC in MP3."""
    mp3_path = MP3_DIR / flac_path.with_suffix(".mp3").name

    if mp3_path.exists() and mp3_path.stat().st_size > 0:
        print(f"  Gia' convertito: {mp3_path.name}, salto.")
        return True

    print(f"  Converto: {flac_path.name} -> {mp3_path.name} ({bitrate}kbps)")

    proc = subprocess.run(
        [
            ffmpeg, "-y", "-loglevel", "error",
            "-i", str(flac_path),
            "-codec:a", "libmp3lame",
            "-b:a", f"{bitrate}k",
            str(mp3_path),
        ],
        capture_output=True,
        text=True,
    )

    if proc.returncode != 0:
        print(f"    ERRORE ffmpeg: {proc.stderr[:300]}")
        mp3_path.unlink(missing_ok=True)
        return False

    flac_mb = flac_path.stat().st_size / (1024 * 1024)
    mp3_mb = mp3_path.stat().st_size / (1024 * 1024)
    print(f"    {flac_mb:.1f}MB -> {mp3_mb:.1f}MB")
    return True


def main():
    DOWNLOADS_DIR.mkdir(exist_ok=True)
    MP3_DIR.mkdir(exist_ok=True)

    args = sys.argv[1:]

    bitrate = DEFAULT_BITRATE
    if "--bitrate" in args:
        idx = args.index("--bitrate") + 1
        if idx < len(args):
            bitrate = int(args[idx])

    delete_flac = "--delete" in args

    print("=" * 55)
    print("  Conversione FLAC -> MP3")
    print("=" * 55)

    flac_files = sorted(DOWNLOADS_DIR.glob("*.flac"))
    if not flac_files:
        print("\nNessun file FLAC trovato in downloads/")
        return

    print(f"\nTrovati {len(flac_files)} file FLAC (bitrate: {bitrate}kbps)\n")

    ok_count = 0
    for i, flac_file in enumerate(flac_files, 1):
        print(f"--- File {i}/{len(flac_files)} ---")
        success = convert_flac_to_mp3(_find_ffmpeg(), flac_file, bitrate)
        if success:
            ok_count += 1
            if delete_flac:
                flac_file.unlink()
                print(f"    FLAC eliminato: {flac_file.name}")

    print(f"\nCompletato: {ok_count}/{len(flac_files)} file convertiti.")

    if delete_flac and ok_count > 0:
        print(f"Eliminati {ok_count} file FLAC originali.")


if __name__ == "__main__":
    main()
