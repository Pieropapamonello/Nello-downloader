# Nello Downloader

[Deploy su Render](https://render.com/deploy?repo=https://github.com/Pieropapamonello/Nello-downloader)

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

## Sottotitoli italiani gratuiti

Il downloader usa le tracce disponibili per video singoli fino a 180 secondi,
solo quando i metadati della traccia audio o dei sottotitoli originali indicano
inglese. Un titolo inglese o una traduzione inglese non bastano. Se la lingua
non e verificabile, il video resta invariato. Su TikTok viene conservata la lingua
delle tracce ASR, distinguendole da quelle MT tradotte.

La traccia italiana del social ha priorita. Altrimenti le battute inglesi
vengono tradotte localmente con il modello incluso nell'immagine Docker,
senza richieste esterne, chiavi o abbonamenti. Per ciascun video sono ammessi
fino a 4000 caratteri e 300 battute; errori o limiti di risorse lasciano
il video originale.

Solo nelle installazioni senza modello locale viene usato MyMemory gratuito,
con riserva sul servizio pubblico Google Translate usato da googletrans.
Questi servizi ricevono solo testo, mai audio o cookie, e possono rifiutare
richieste per limiti di quota; il servizio Render distribuito non ne dipende.

I sottotitoli vengono impressi con FFmpeg (H.264/AAC, un thread, massimo 640px).
Sono gialli, maiuscoli e in gruppi di massimo tre parole, con una breve
animazione. I gruppi italiani sono distribuiti nei tempi delle frasi tradotte:
non e un allineamento fonetico parola per parola. Un controllo OCR limitato
a tre fotogrammi cerca una fascia stabile di grandi scritte maiuscole e
posiziona l'italiano subito sopra; se non la identifica usa una posizione
compatta al 64% dell'altezza. Le scritte originali restano nel video.
Ricerca/traduzione ha un budget separato di 60 secondi, conversione di 150 secondi;
la pressione della memoria interrompe soltanto questa elaborazione opzionale.
Se mancano le tracce, i video singoli fino a **90 secondi** passano al riconoscimento
locale `whisper.cpp` v1.7.6, modello multilingue tiny Q5_1 (circa 32 MB su disco).
Prima riconosce la lingua su 12 secondi: prosegue solo se rileva inglese con
confidenza almeno 0.85. Poi trascrive e traduce il testo, senza caricare audio
su servizi esterni. La traduzione viene eseguita sul server con il modello
Argos English-Italian 1.0 e CTranslate2 int8, un thread e una frase per volta.
Non dipende dalle quote dei servizi pubblici di traduzione. Il modello parte
solo dopo la chiusura di Whisper, nel processo temporaneo sorvegliato; non
rimane in memoria tra i download. Sono inclusi solo tokenizer e traduttore,
senza Stanza o PyTorch. Anche questo archivio ha un checksum verificato.
Nessuna API a pagamento, PyTorch, GPU o sessione permanente del modello.
Il modello e il binario sono inclusi nell'immagine; checksum verificato in build.

Il riconoscimento parte dopo la chiusura del processo downloader ed esegue un solo
thread. Il supervisore interrompe l'intero gruppo di processi al limite memoria
del container o dopo 240 secondi, conservando il video originale. Conversione
e riconoscimento non si sovrappongono. Lingua incerta, assenza di parlato, durata
eccessiva, quota o altri errori sono registrati solo nei log. La trascrizione
automatica puo sbagliare nomi propri e parole, specialmente con musica/rumore.
L'esito e nei log e nel campo API
`subtitles`: `burned_it`, `unavailable`, `skipped_resource_or_encoding`.

## Post Facebook condivisi

I link `/share/`, `/posts/` e permalink vengono risolti al post originale.
Il downloader accetta solo l'allegato collegato esplicitamente all'ID di quel
post, ignorando video suggeriti, avatar e metadati generici `og:type`.
Se non puo determinare l'allegato esatto o trova un album non supportato,
si ferma senza inviare un contenuto diverso. Foto e descrizione provengono
dal nodo del post identificato; i video vengono scaricati dal loro ID esatto.

## Verifica locale

```sh
pip install -r requirements.txt
python -m unittest test_remote_downloader test_youtube_duration test_youtube_job test_media_resources test_wa_media -q
```

Per eseguire il servizio completo usare Docker: include FFmpeg, Deno e il
provider PO token. Distribuire separatamente bot e downloader dopo modifiche
al protocollo API.
