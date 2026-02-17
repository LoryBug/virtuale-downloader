"""
Trascrizione audio FLAC via Groq Whisper API.

Trascrive tutti i file .flac nella cartella downloads/ e salva i risultati
come JSON (con segmenti timestampati) e/o TXT (solo testo).

Uso:
    uv run transcribe.py                # trascrive tutti i FLAC
    uv run transcribe.py --flatten      # converte solo JSON -> TXT
    uv run transcribe.py --all          # trascrive + flatten
"""

import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Config da .env
# ---------------------------------------------------------------------------

load_dotenv()

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_SPLIT_THRESHOLD_MB = int(os.getenv("GROQ_SPLIT_THRESHOLD_MB", "25"))
WHISPER_LANGUAGE = os.getenv("WHISPER_LANGUAGE", "it")
HALLUCINATION_MAX_WORDS = int(os.getenv("HALLUCINATION_MAX_WORDS", "10"))
HALLUCINATION_MIN_DURATION = float(os.getenv("HALLUCINATION_MIN_DURATION", "2.5"))

DOWNLOADS_DIR = Path(__file__).parent / "downloads"
MAX_FILE_SIZE_MB = 100
MAX_PART_RETRIES = 3
RETRY_BASE_DELAY = 2  # seconds


# ---------------------------------------------------------------------------
# FFmpeg helpers
# ---------------------------------------------------------------------------

def _find_ffmpeg() -> str:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        print("ERRORE: ffmpeg non trovato nel PATH.")
        print("Installalo da: https://ffmpeg.org/download.html")
        sys.exit(1)
    return ffmpeg


def _find_ffprobe() -> str:
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        print("ERRORE: ffprobe non trovato nel PATH.")
        sys.exit(1)
    return ffprobe


def get_duration(file_path: Path) -> float:
    ffprobe = _find_ffprobe()
    result = subprocess.run(
        [ffprobe, "-v", "quiet", "-print_format", "json", "-show_format", str(file_path)],
        capture_output=True, text=True, check=True,
    )
    data = json.loads(result.stdout)
    return float(data["format"]["duration"])


def split_audio(input_path: Path, max_size_mb: int = 20) -> list[Path]:
    """Splitta un file audio in parti sotto max_size_mb con -ss/-t + re-encode FLAC."""
    file_size_mb = input_path.stat().st_size / (1024 * 1024)

    if file_size_mb <= max_size_mb:
        return [input_path]

    ffmpeg = _find_ffmpeg()
    duration = get_duration(input_path)
    mb_per_second = file_size_mb / duration
    segment_seconds = int(max_size_mb / mb_per_second * 0.9)  # 10% margine
    segment_seconds = max(segment_seconds, 60)

    num_parts = int(file_size_mb / max_size_mb) + 1
    print(f"  Split: {input_path.name} ({file_size_mb:.1f}MB) in ~{num_parts} parti da ~{segment_seconds}s")

    parts = []
    start = 0.0

    while start < duration:
        part_path = input_path.parent / f"{input_path.stem}_part{len(parts):03d}.flac"
        cmd = [
            ffmpeg, "-ss", str(start), "-i", str(input_path),
            "-t", str(segment_seconds), "-c:a", "flac", "-y", str(part_path),
        ]

        try:
            subprocess.run(cmd, capture_output=True, text=True, check=True)
        except subprocess.CalledProcessError as e:
            print(f"  ERRORE split parte {len(parts)}: {e.stderr[:200]}")
            for p in parts:
                p.unlink(missing_ok=True)
            raise

        if part_path.exists() and part_path.stat().st_size > 0:
            parts.append(part_path)

        start += segment_seconds

    print(f"  Splittato in {len(parts)} parti")
    return parts


# ---------------------------------------------------------------------------
# Groq API
# ---------------------------------------------------------------------------

def _get_groq_client():
    try:
        from groq import Groq
    except ImportError:
        print("ERRORE: pacchetto 'groq' non installato. Esegui: uv add groq")
        sys.exit(1)

    if not GROQ_API_KEY:
        print("ERRORE: GROQ_API_KEY non configurata nel file .env")
        sys.exit(1)

    return Groq(api_key=GROQ_API_KEY)


def _is_retryable_error(exc: Exception) -> bool:
    error_name = type(exc).__name__.lower()
    error_msg = str(exc).lower()

    indicators = ["connection", "timeout", "remotedisconnected", "connectionreset"]
    for ind in indicators:
        if ind in error_name or ind in error_msg:
            return True

    if hasattr(exc, "status_code") and isinstance(exc.status_code, int):
        if exc.status_code in (500, 502, 503):
            return True

    if hasattr(exc, "__cause__") and exc.__cause__ is not None:
        return _is_retryable_error(exc.__cause__)

    return False


def _transcribe_single(client, file_path: Path) -> list[dict]:
    """Trascrive un singolo file via Groq. Ritorna lista di segmenti {start, end, text}."""
    with open(file_path, "rb") as f:
        transcription = client.audio.transcriptions.create(
            file=(file_path.name, f),
            model="whisper-large-v3",
            language=WHISPER_LANGUAGE,
            response_format="verbose_json",
        )

    segments = []
    if hasattr(transcription, "segments") and transcription.segments:
        for seg in transcription.segments:
            segments.append({
                "start": seg.get("start", 0.0),
                "end": seg.get("end", 0.0),
                "text": seg.get("text", "").strip(),
            })
    else:
        segments.append({"start": 0.0, "end": 0.0, "text": transcription.text.strip()})

    return segments


def _transcribe_with_retry(client, part_path: Path, part_num: int, total: int) -> list[dict]:
    for attempt in range(1, MAX_PART_RETRIES + 1):
        try:
            return _transcribe_single(client, part_path)
        except Exception as e:
            if attempt < MAX_PART_RETRIES and _is_retryable_error(e):
                delay = RETRY_BASE_DELAY * (2 ** (attempt - 1))
                print(f"  Parte {part_num}/{total} fallita (tentativo {attempt}): {e}. Riprovo in {delay}s...")
                time.sleep(delay)
            else:
                raise


def _split_and_transcribe(client, file_path: Path) -> list[dict]:
    """Splitta e trascrive, unendo i segmenti con offset temporali corretti."""
    threshold = GROQ_SPLIT_THRESHOLD_MB
    parts = split_audio(file_path, max_size_mb=threshold - 5)

    all_segments = []
    time_offset = 0.0

    try:
        for i, part_path in enumerate(parts):
            print(f"  Trascrivo parte {i + 1}/{len(parts)}: {part_path.name}")

            part_segments = _transcribe_with_retry(client, part_path, i + 1, len(parts))

            for seg in part_segments:
                seg["start"] += time_offset
                seg["end"] += time_offset
                all_segments.append(seg)

            if part_segments:
                part_duration = max(s["end"] for s in part_segments) - time_offset
                time_offset += part_duration
            else:
                time_offset += get_duration(part_path)
    finally:
        for part_path in parts:
            if part_path.exists() and part_path != file_path:
                part_path.unlink()

    print(f"  Uniti {len(all_segments)} segmenti da {len(parts)} parti")
    return all_segments


# ---------------------------------------------------------------------------
# Filtro allucinazioni
# ---------------------------------------------------------------------------

HALLUCINATION_PATTERNS = [
    "[musica]", "[applausi]", "[music]", "[applause]",
    "sottotitoli", "sottotitoli a cura di",
    "grazie per aver guardato", "iscriviti al canale",
]


def _is_hallucination(seg: dict) -> bool:
    duration = seg["end"] - seg["start"]
    words = seg["text"].strip().split()

    if duration < HALLUCINATION_MIN_DURATION and len(words) > HALLUCINATION_MAX_WORDS:
        return True

    if not seg["text"].strip():
        return True

    text_lower = seg["text"].lower().strip()
    return any(p in text_lower for p in HALLUCINATION_PATTERNS)


def _filter_hallucinations(segments: list[dict]) -> tuple[list[dict], int]:
    valid = [s for s in segments if not _is_hallucination(s)]
    filtered = len(segments) - len(valid)
    return valid, filtered


# ---------------------------------------------------------------------------
# Trascrizione completa
# ---------------------------------------------------------------------------

def transcribe_file(client, file_path: Path) -> dict | None:
    """Trascrive un file FLAC e ritorna il risultato come dict."""
    file_size = file_path.stat().st_size
    file_size_mb = file_size / (1024 * 1024)

    if file_size_mb > MAX_FILE_SIZE_MB:
        print(f"  ERRORE: file troppo grande ({file_size_mb:.1f}MB, max {MAX_FILE_SIZE_MB}MB)")
        return None

    print(f"  Trascrivo via Groq: {file_path.name} ({file_size_mb:.1f}MB)")

    try:
        try:
            segments = _transcribe_single(client, file_path)
        except Exception as e:
            threshold = GROQ_SPLIT_THRESHOLD_MB
            if file_size_mb > threshold and _is_retryable_error(e):
                print(f"  Errore connessione su file da {file_size_mb:.1f}MB. Splitta in parti < {threshold}MB...")
                segments = _split_and_transcribe(client, file_path)
            else:
                raise
    except Exception as e:
        print(f"  ERRORE trascrizione: {e}")
        return None

    valid_segments, n_filtered = _filter_hallucinations(segments)
    if n_filtered > 0:
        print(f"  Filtrate {n_filtered} allucinazioni")

    stat = file_path.stat()

    return {
        "file_name": file_path.name,
        "file_path": str(file_path),
        "creation_date": datetime.fromtimestamp(stat.st_ctime).isoformat(),
        "modification_date": datetime.fromtimestamp(stat.st_mtime).isoformat(),
        "status": "completed",
        "language": WHISPER_LANGUAGE,
        "total_segments": len(valid_segments),
        "segments": valid_segments,
    }


# ---------------------------------------------------------------------------
# Flatten JSON -> TXT
# ---------------------------------------------------------------------------

def flatten_json(json_path: Path) -> str:
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if "segments" in data:
        return " ".join(s["text"] for s in data["segments"])

    if "testo" in data:
        return " ".join(item[2] for item in data["testo"] if len(item) > 2)

    raise ValueError(f"Formato JSON sconosciuto: {json_path.name}")


def do_flatten():
    """Converte tutti i JSON in downloads/ in file TXT."""
    json_files = sorted(DOWNLOADS_DIR.glob("*_transcription.json"))
    if not json_files:
        print("Nessun file JSON di trascrizione trovato in downloads/")
        return

    for json_file in json_files:
        txt_name = json_file.name.replace("_transcription.json", ".txt")
        txt_path = DOWNLOADS_DIR / txt_name

        text = flatten_json(json_file)
        txt_path.write_text(text, encoding="utf-8")
        print(f"  {json_file.name} -> {txt_name}")

    print(f"\nFlatten completato: {len(json_files)} file convertiti.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def do_transcribe():
    """Trascrive tutti i FLAC in downloads/."""
    flac_files = sorted(DOWNLOADS_DIR.glob("*.flac"))
    if not flac_files:
        print("Nessun file FLAC trovato in downloads/")
        return

    # Filtra file gia' trascritti
    to_transcribe = []
    for f in flac_files:
        if f.stem.endswith("_part"):
            continue
        json_path = DOWNLOADS_DIR / f"{f.stem}_transcription.json"
        if json_path.exists():
            print(f"  Gia' trascritto: {f.name}, salto.")
        else:
            to_transcribe.append(f)

    if not to_transcribe:
        print("Tutti i file sono gia' trascritti.")
        return

    client = _get_groq_client()
    ok_count = 0

    for i, flac_file in enumerate(to_transcribe, 1):
        print(f"\n--- File {i}/{len(to_transcribe)} ---")
        result = transcribe_file(client, flac_file)

        if result:
            json_path = DOWNLOADS_DIR / f"{flac_file.stem}_transcription.json"
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, indent=2)
            print(f"  Salvato: {json_path.name}")
            ok_count += 1

    print(f"\nCompletato: {ok_count}/{len(to_transcribe)} file trascritti.")


def main():
    DOWNLOADS_DIR.mkdir(exist_ok=True)

    args = sys.argv[1:]

    print("=" * 55)
    print("  Trascrizione Audio - Groq Whisper API")
    print("=" * 55)

    if "--flatten" in args:
        do_flatten()
    elif "--all" in args:
        do_transcribe()
        print()
        do_flatten()
    else:
        do_transcribe()


if __name__ == "__main__":
    main()
