# virtuale-downloader

Scarica l'audio delle lezioni registrate da SharePoint UniBo, anche quando il download e' disabilitato dal docente. L'audio viene salvato in formato FLAC ottimizzato per la trascrizione.

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

## Output

I file FLAC vengono salvati nella cartella `downloads/`. La sessione del browser viene salvata in `.browser_data/` per non dover rifare il login ogni volta.
