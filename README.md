# virtuale-downloader

Scarica l'audio delle lezioni registrate da SharePoint UniBo, anche quando il download e' disabilitato dal docente. L'audio viene salvato in formato FLAC ottimizzato per la trascrizione e puo' essere trascritto automaticamente via Groq Whisper API.

## Come funziona

1. Apre un browser Chromium dove effettui il login SSO di ateneo
2. Intercetta il manifest DASH (streaming adattivo) dal traffico di rete del player
3. Scarica i segmenti audio criptati in parallelo
4. Li decripta (AES-128-CBC) usando la chiave dal manifest
5. Converte in FLAC mono 16kHz 16-bit (formato ideale per Whisper e altri tool di trascrizione)

## Requisiti

- Python 3.14+
- [uv](https://docs.astral.sh/uv/)
- [ffmpeg](https://ffmpeg.org/) installato e nel PATH

## Setup

```bash
git clone <repo-url>
cd virtuale-downloader
uv sync
uv run playwright install chromium
```

Per la trascrizione, crea un file `.env` con la tua chiave Groq (vedi `.env.example`):

```bash
cp .env.example .env
# Inserisci la tua GROQ_API_KEY
```

## Uso

### Interattivo

```bash
uv run main.py
```

Lo script apre un browser, fai il login, premi INVIO, e incolla gli URL dei video uno per riga.

### Da riga di comando

```bash
uv run main.py --urls "URL1" "URL2" "URL3"
```

### Da file

```bash
uv run main.py --urls-file lista.txt
```

Il file deve contenere un URL per riga. Le righe vuote e quelle che iniziano con `#` vengono ignorate.

## Formati URL supportati

- `https://liveunibo.sharepoint.com/.../stream.aspx?id=/sites/.../video.mp4`
- `https://liveunibo.sharepoint.com/:v:/s/NomeCorso/...`

## Trascrizione

Dopo aver scaricato i FLAC, puoi trascriverli automaticamente con Groq Whisper API:

```bash
# Trascrive tutti i FLAC in downloads/ -> JSON con segmenti timestampati
uv run transcribe.py

# Converte i JSON trascritti in TXT (solo testo piano)
uv run transcribe.py --flatten

# Trascrive + flatten in un colpo
uv run transcribe.py --all
```

Lo script gestisce automaticamente:
- Split dei file grandi (>25MB) con FFmpeg e re-encode FLAC
- Retry con backoff esponenziale su errori di connessione
- Filtro anti-allucinazioni Whisper (musica, sottotitoli fantasma, etc.)
- Skip dei file gia' trascritti

### Configurazione (.env)

| Variabile | Default | Descrizione |
|-----------|---------|-------------|
| `GROQ_API_KEY` | - | Chiave API Groq (obbligatoria) |
| `GROQ_SPLIT_THRESHOLD_MB` | `25` | Soglia split in MB (free: 25, dev: 100) |
| `WHISPER_LANGUAGE` | `it` | Lingua trascrizione |
| `HALLUCINATION_MAX_WORDS` | `10` | Soglia parole per filtro allucinazioni |
| `HALLUCINATION_MIN_DURATION` | `2.5` | Durata minima segmento (secondi) |

## Conversione MP3

Dopo aver scaricato i FLAC, puoi convertirli in MP3:

```bash
# Converte tutti i FLAC in downloads/ -> MP3 in downloads/mp3/
uv run convert_mp3.py

# Bitrate personalizzato (default: 192kbps)
uv run convert_mp3.py --bitrate 320

# Elimina i FLAC originali dopo la conversione
uv run convert_mp3.py --delete
```

### Uso con NotebookLM

Se vuoi usare [NotebookLM](https://notebooklm.google.com/) per studiare le lezioni, non serve la trascrizione: basta convertire i FLAC in MP3 e caricare direttamente i file MP3 come sorgenti nel notebook. NotebookLM trascrive e indicizza l'audio automaticamente.

```bash
uv run main.py                    # scarica le lezioni
uv run convert_mp3.py --delete    # converti in MP3 (ed elimina i FLAC)
# -> carica i file da downloads/mp3/ su NotebookLM
```

## Output

I file vengono salvati nella cartella `downloads/`:
- `*.flac` - Audio scaricato
- `transcriptions/*_transcription.json` - Trascrizione con segmenti timestampati
- `transcriptions/*.txt` - Testo piano (dopo flatten)
- `mp3/*.mp3` - Audio convertito in MP3

La sessione del browser viene salvata in `.browser_data/` per non dover rifare il login ogni volta.
