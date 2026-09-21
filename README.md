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
Sono gialli, maiuscoli, con il carattere Luckiest Guy incluso con licenza Apache,
piu grandi anche nei video orizzontali e in basso per impostazione predefinita.
Silero VAD identifica le pause: i sottotitoli vocali non attraversano gli
intervalli senza parlato. La divisione interna delle frasi resta stimata.
L'OCR lavora su piccole fasce bianche, non sull'intero sfondo. Solo scritte
che cambiano fra fotogrammi e corrispondono al parlato inglese autorizzano
una maschera nera. Se la lettura e sufficientemente completa, i frammenti
originali vengono tradotti separatamente, con tempi campionati ogni 250 ms.
In quel caso l'italiano sostituisce l'inglese nella stessa fascia coperta.
Scritte statiche e loghi non autorizzano spostamenti o maschere. Incertezza o
limiti di tempo mantengono i sottotitoli vocali in basso.
Ricerca/traduzione ha un budget separato di 100 secondi, conversione di 150 secondi;
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


### Trascrizione automatica dei vocali italiani

Telegram, WhatsApp e Discord rispondono ai messaggi audio riconosciuti come italiani con il testo della trascrizione. Le altre lingue non vengono trascritte. Se parole o lingua sono incerte, il bot risponde con un breve avviso. Nessuna traduzione degli audio stranieri. Il messaggio originale resta disponibile.

Il riconoscitore Whisper locale usa la coda seriale del downloader e non richiede nuove chiavi o servizi a pagamento. Limiti per Render Free: 3 minuti e 8 MB per audio; massimo 3 richieste vocali contemporaneamente in attesa dal bot. I file temporanei vengono eliminati dopo il riconoscimento, senza salvare il testo nei log o nella cache dei social. Serve la configurazione DOWNLOADER_URL/DOWNLOADER_TOKEN gia usata per i video. La precisione dipende dalla chiarezza della voce e dal rilevamento della lingua.

I vocali fino a 90 secondi usano Small Q4_1; quelli piu lunghi usano Base Q5
per contenere i tempi. Entrambi usano flash attention, un thread e processi
separati per blocchi di massimo 20 secondi, con contesto audio proporzionato
alla durata del blocco e un limite complessivo di 900 secondi. I tagli preferiscono le pause
e non lasciano un frammento finale inferiore a 8 secondi. Tiny controlla la lingua;
Base verifica i casi incerti prima di consentire una trascrizione italiana.
Il testo viene normalizzato con spazi, maiuscole iniziali, paragrafi e correzioni
ortografiche deterministiche (accenti/apostrofi). LanguageTool 6.6 esegue poi un
controllo italiano offline, dopo la chiusura di Whisper: processo temporaneo,
heap massimo 160 MB, un processore e massimo 40 secondi. Si applicano soltanto
correzioni grammaticali non ambigue e refusi minuscoli con una sola proposta
e una sola modifica di carattere; nomi, numeri, negazioni e suggerimenti ambigui
non vengono sostituiti. Non viene riassunto, inviato a correttori esterni o
riscritto per indovinare parole incomprensibili. Un errore del correttore mantiene
la trascrizione gia riuscita. Dialetto, concordanze ambigue e parole mal
riconosciute possono comunque restare.
Il modello piu accurato puo impiegare alcuni minuti su Render Free. Se supera
il limite di memoria o il tempo disponibile, il processo viene chiuso prima
di riprovare una sola volta con Base, entro il budget totale di 900 secondi.
La variante Q4_1 riduce di circa 30 MB i pesi rispetto a Small Q5; il download
e vincolato a una revisione e a un checksum SHA-256.


### Sottotitoli multilingua

I video vengono controllati anche per scritte incorporate, compresi i video muti. Il testo straniero riconosciuto viene tradotto in italiano; scritte gia italiane eviteranno una seconda sovrapposizione. Il parlato non italiano usa Whisper e traduzione gratuita. Inglese: traduttore locale; altre lingue: provider pubblici gratuiti, soggetti a disponibilita e quote. Nessuna API a pagamento.

Su Render Free il riconoscimento audio e visivo e limitato a 90 secondi; le tracce sottotitoli native a 180 secondi. OCR, lingua o traduzione incerti e limiti di risorse conservano il video originale. I vocali italiani restano separati: massimo 180 secondi, elaborati in blocchi di massimo 20 secondi; nessuna traduzione dei vocali stranieri.

Il servizio autenticato accetta anche MP4 gia scaricati tramite POST /subtitle-jobs/{uuid} (body binario, massimo 20 MB), nella stessa coda e con le stesse API di stato, file e cancellazione degli altri lavori.
