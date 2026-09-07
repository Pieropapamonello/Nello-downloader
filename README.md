# Nello Downloader

Servizio di download separato dal bot [Nello](https://github.com/lamenDino/Nello).
Esegue estrazione e conversione su una propria istanza, con coda seriale,
controllo durata YouTube (massimo 180 secondi) e protezione della memoria.
Non invia messaggi alle chat. Il client di riferimento e i test sono inclusi.

## Render

Creare un Blueprint usando `render.yaml`, oppure un Web Service Docker con
`Dockerfile`, piano **Free**, regione Frankfurt e health check `/healthz`.
Impostare `DOWNLOADER_TOKEN` a un segreto casuale di almeno 32 caratteri.
Caricare nei Secret Files i cookie necessari: `YOUTUBE_COOKIES`,
`FACEBOOK_COOKIES`, `INSTAGRAM_COOKIES`, `TIKTOK_COOKIES`.
Non inserire cookie o credenziali nel repository.

Nel servizio del bot impostare:

- `DOWNLOADER_URL`: URL HTTPS di questo servizio.
- `DOWNLOADER_TOKEN`: lo stesso segreto del downloader.
- `DOWNLOAD_TIMEOUT`: `900`.

Attivare il collegamento solo dopo che il downloader risponde a `/healthz`.
Il controllo pubblico `/healthz` e utilizzabile dal cron esterno.
Gli endpoint `/jobs` richiedono `Authorization: Bearer <DOWNLOADER_TOKEN>`.
I file vengono trasferiti in streaming e rimossi dopo il recupero; i job
completati scadono dopo 20 minuti. La separazione della memoria non garantisce
che YouTube accetti ogni richiesta: cookie e blocchi della piattaforma restano
possibili cause di errore, registrate nei log.

## Verifica locale

```sh
pip install -r requirements.txt
python -m unittest test_remote_downloader test_youtube_duration test_youtube_job test_media_resources test_wa_media -q
```

Per eseguire il servizio completo usare Docker: include FFmpeg, Deno e il
provider PO token. Distribuire separatamente bot e downloader dopo modifiche
al protocollo API.
