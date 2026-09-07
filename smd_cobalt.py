#!/usr/bin/env python3
"""Mixin estratto da social_downloader.py (refactoring per piattaforma).
Contiene metodi che operano su `self` del SocialMediaDownloader."""

import os
import re
import json
import html
import time
import asyncio
import logging
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import requests

try:
    import cloudscraper
except ImportError:
    cloudscraper = None

logger = logging.getLogger(__name__)


class CobaltMixin:
    async def download_with_cobalt(self, url: str) -> Optional[Dict]:
        """
        Usa Cobalt API v10 (https://github.com/imputnet/cobalt) per scaricare media 
        senza usare cookie locali né yt-dlp direttamente sulla macchina.
        Ottimo per YouTube/Insta/TikTok su server bloccati.
        Supporta failover su più istanze pubbliche.
        """
        # Lista di istanze pubbliche (V10 compatible)
        # Nota: api.cobalt.tools richiede Turnstile/Key ora, quindi, usiamo mirror community
        # Aggiornato al 2026/02
        # Lista ridotta: molte istanze pubbliche muoiono in fretta (DNS inesistente).
        # Sovrascrivibile via env COBALT_INSTANCES (separate da virgola).
        cobalt_instances = [
            "https://cobalt.stream",
            "https://cobalt.tools",
            "https://cobalt.q11.app",
            "https://cobalt.154.be",
        ]
        env_instances = os.getenv("COBALT_INSTANCES")
        if env_instances:
            cobalt_instances = [u.strip() for u in env_instances.split(",") if u.strip()]

        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        
        # Payload aggiornato per API v10 (camelCase)
        payload = {
            "url": url,
            "videoQuality": "1080",
            "audioFormat": "mp3",
            "filenameStyle": "basic",
            # "youtubeVideoCodec": "h264" # Default h264
        }
        
        logger.info(f"Cobalt fallback triggered for: {url}")
        
        loop = asyncio.get_event_loop()

        for base_url in cobalt_instances:
            api_url = f"{base_url}/" # V10 usa root endpoint
            logger.info(f"Trying Cobalt instance: {api_url}")

            try:
                def _req():
                    # Usa cloudscraper se disponibile per bypassare Cloudflare
                    # Abbassato timeout a 15s per saltare velocemente se lento
                    try:
                        if cloudscraper:
                            scraper = cloudscraper.create_scraper()
                            return scraper.post(api_url, json=payload, headers=headers, timeout=15)
                        else:
                            return requests.post(api_url, json=payload, headers=headers, timeout=15)
                    except Exception as e:
                        logger.warning(f"Cobalt request failed for {base_url}: {e}")
                        return None
                    
                r = await loop.run_in_executor(None, _req)
                
                if r and r.status_code == 200:
                    data = r.json()
                    
                    # Analisi risposta v10/v7 compatibile
                    # v10: status=tunnel/redirect/picker
                    status = data.get("status")
                    
                    download_url = None
                    
                    if status in ["tunnel", "redirect"]:
                        download_url = data.get("url")
                    elif status == "picker":
                        picker_items = data.get("picker", [])
                        if picker_items:
                            # Cerca video
                            for item in picker_items:
                                if item.get("type") == "video":
                                    download_url = item.get("url")
                                    break
                            # Se non trova video, prende il primo (es. foto)
                            if not download_url:
                                download_url = picker_items[0].get("url")
                    elif "url" in data: # Fallback per vecchie versioni (v7) se qualche istanza è vecchia
                        download_url = data["url"]

                    if download_url:
                        logger.info(f"Cobalt download URL found via {base_url}: {download_url}")
                        
                        # Scarica il file
                        def _dl_file():
                            return requests.get(download_url, stream=True, timeout=60)
                        
                        resp = await loop.run_in_executor(None, _dl_file)
                        
                        if resp.status_code == 200:
                            # Salva su file temp
                            ext = "mp4" # Default
                            # Tentativo di indovinare estensione da Content-Type
                            ctype = resp.headers.get("Content-Type", "")
                            if "image" in ctype:
                                ext = "jpg"
                            elif "audio" in ctype:
                                ext = "mp3"
                                
                            filename = os.path.join(self.temp_dir, f"cobalt_{int(time.time())}.{ext}")
                            
                            with open(filename, 'wb') as f:
                                for chunk in resp.iter_content(chunk_size=1024 * 1024):
                                    if chunk:
                                        f.write(chunk)
                                        
                            if os.path.getsize(filename) > 0:
                                # mp4 -> video singolo; immagini/audio -> 'carousel'
                                # (il bot gestisce solo 'video' e 'carousel', non 'image':
                                # restituire 'image' faceva sparire il media senza inviarlo).
                                if ext == "mp4":
                                    return {
                                        "success": True,
                                        "type": "video",
                                        "file_path": filename,
                                        "title": f"Downloaded via Cobalt ({base_url})",
                                        "platform": self.detect_platform(url),
                                        "url": url,
                                    }
                                else:
                                    return {
                                        "success": True,
                                        "type": "carousel",
                                        "files": [filename],
                                        "title": f"Downloaded via Cobalt ({base_url})",
                                        "platform": self.detect_platform(url),
                                        "url": url,
                                    }
                
                # Se status code != 200 o data parsing fallito, logga e continua
                if r:
                     logger.warning(f"Cobalt instance {base_url} failed with {r.status_code}: {r.text[:200]}")
                else:
                     logger.warning(f"Cobalt instance {base_url} failed (connection error)")

            except Exception as e:
                logger.warning(f"Cobalt instance {base_url} error: {e}")
                continue # Prova la prossima istanza

        logger.error("All Cobalt instances failed.")
        return None

