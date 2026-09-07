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

logger = logging.getLogger(__name__)


class FacebookMixin:
    async def _facebook_fallback(self, url: str) -> Optional[List[str]]:
        """Fallback for Facebook posts (images) using requests + regex"""
        try:
            headers = {
                'User-Agent': self.get_random_user_agent(),
                'Accept-Language': 'en-US,en;q=0.9',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                'Referer': 'https://www.facebook.com/',
                'Origin': 'https://www.facebook.com',
                'Sec-Fetch-Dest': 'document',
                'Sec-Fetch-Mode': 'navigate',
                'Sec-Fetch-Site': 'same-origin',
                'Upgrade-Insecure-Requests': '1',
            }
            # Load cookies if available
            cookies = self._load_netscape_cookies(self.facebook_cookies) if hasattr(self, 'facebook_cookies') else None

            loop = asyncio.get_event_loop()
            
            def _fetch():
                return requests.get(url, headers=headers, cookies=cookies, timeout=15)
            
            resp = await loop.run_in_executor(None, _fetch)
            if resp.status_code != 200:
                logger.warning(f"Facebook fallback: status code {resp.status_code} for {url}")
                return None

            resp.encoding = 'utf-8'  # evita mojibake (senza, requests decodifica come Latin-1)
            text = resp.text

            # --- DETECT VIDEO ---
            # Se il link è esplicitamente un video (controllato dalla presenza di video indicators nel meta), 
            # e siamo qui (fallback immagini), significa che yt-dlp ha fallito. 
            # L'utente non vuole la foto se è un video.
            video_indicators = [
                r'<meta\s+property="og:type"\s+content="video',
                r'<meta\s+property="og:video"',
                r'<meta\s+name="twitter:player"',
                r'"__typename":"Video"',
                r'"is_video":true'
            ]
            is_video_page = False
            for vi in video_indicators:
                if re.search(vi, text, re.IGNORECASE):
                    is_video_page = True
                    break
            
            # Se sembra un video, controlla se l'URL non era esplicitamente una foto
            if is_video_page and '/photo' not in url:
                # Tenta un ultimo fallback brutale: cerca .mp4 nel sorgente
                # A volte fb restituisce il link .mp4 in chiaro anche se yt-dlp non riesce a parsare
                logger.info("Facebook fallback: detected VIDEO page. Searching for raw .mp4 link...")
                # Estrai SOLO il video vero del post dalle chiavi note di Facebook,
                # non un mp4 qualsiasi: la pagina contiene anche video suggeriti/
                # correlati e prenderne uno a caso porta a inviare un video che non
                # c'entra nulla (specie sui post-foto o sui /share/ non-video).
                t_norm = text.replace('\\/', '/')
                best_mp4 = None
                _k = None
                for _k in ('playable_url_quality_hd', 'browser_native_hd_url',
                           'playable_url', 'browser_native_sd_url', 'hd_src', 'sd_src'):
                    _km = re.search(r'"' + _k + r'"\s*:\s*"(https?://[^"]+?\.mp4[^"]*)"', t_norm)
                    if _km:
                        best_mp4 = _km.group(1)
                        break
                if best_mp4:
                    logger.info(f"Facebook fallback: trovato video del post (chiave {_k}). Downloading...")
                    mp4_url = html.unescape(best_mp4)
                    ts = datetime.utcnow().strftime('%Y%m%d%H%M%S')
                    tmp_mp4 = os.path.join(self.temp_dir, f"fb_{ts}_fallback.mp4")
                    
                    def _dl_mp4():
                        try:
                            r = requests.get(mp4_url, headers=headers, stream=True, timeout=60)
                            if r.status_code == 200:
                                with open(tmp_mp4, 'wb') as f:
                                    for chunk in r.iter_content(chunk_size=1024*1024):
                                        if chunk:
                                            f.write(chunk)
                                return True
                        except Exception:
                            return False
                        return False
                    
                    try:
                        mp4_success = await loop.run_in_executor(None, _dl_mp4)
                        if mp4_success and os.path.exists(tmp_mp4) and os.path.getsize(tmp_mp4) > 1000:
                             # Estrai la descrizione del video per la didascalia
                             try:
                                 tm = (re.search(r'<meta\s+property="og:title"\s+content="(.*?)"', text, re.IGNORECASE)
                                       or re.search(r'<meta\s+property="og:description"\s+content="(.*?)"', text, re.IGNORECASE)
                                       or re.search(r'<title>(.*?)</title>', text, re.IGNORECASE))
                                 if tm:
                                     ttl = html.unescape(tm.group(1)).replace('| Facebook', '').strip()
                                     if ttl:
                                         self.last_fallback_title = ttl
                             except Exception:
                                 pass
                             return [tmp_mp4]
                    except Exception as e:
                        logger.warning(f"Facebook fallback MP4 download failed: {e}")
                # Nessun video reale del post: invece di non mandare nulla (o, peggio,
                # un video sbagliato), proviamo a estrarre l'IMMAGINE del post qui
                # sotto — caso tipico dei post-foto o dei /share/ che non sono video.
                logger.info("Facebook fallback: nessun video reale del post -> provo a estrarre l'immagine.")

            # Regex for og:image - try multiple patterns
            # Spesso l'ordine degli attributi cambia o ci sono spazi diversi
            img_url = None
            patterns = [
                r'<meta\s+property="og:image"\s+content="([^"]+)"',
                r'<meta\s+content="([^"]+)"\s+property="og:image"',
                r'<meta\s+name="og:image"\s+content="([^"]+)"',
                r'<meta\s+name="twitter:image"\s+content="([^"]+)"',
                r'"image":\s*"([^"]+)"',
                r'"contentUrl":\s*"([^"]+)"'
            ]
            
            for pat in patterns:
                m = re.search(pat, text)
                if m:
                    candidate = html.unescape(m.group(1))
                    candidate_lower = candidate.lower()
                    # Ignore common "ghost" or "placeholder" images
                    if 'profile_pic' in candidate_lower or 'static.xx' in candidate_lower or 'blank.jpg' in candidate_lower:
                        continue
                    if 's40x40' in candidate_lower or 's50x50' in candidate_lower: # Low res thumbnails
                        continue
                    if 'generic' in candidate_lower or 'ad_image' in candidate_lower:
                        continue
                    # Filtra immagini grigie di default
                    if 'gray_profile' in candidate_lower or 'silhouette' in candidate_lower:
                        continue
                        
                    img_url = candidate
                    break

            # Handle /share/ redirects if requests didn't follow them completely (JS redirects)
            if not img_url and 'share/' in url:
                # Try to find the canonical URL in the HTML
                # <link rel="canonical" href="...">
                can_pat = re.search(r'<link\s+rel="canonical"\s+href="([^"]+)"', text)
                if can_pat:
                    real_url = html.unescape(can_pat.group(1))
                    if real_url != url:
                        logger.info(f"Facebook fallback: found canonical url {real_url}")
                        # Recursive call with canonical URL might help if it's different
                        # But be careful of infinite loops. Just try to use it for next steps or return self._facebook_fallback(real_url)
                        pass
            
            # Se ancora nullo, cerca URL diretti di immagini fbcdn ad alta qualità
            if not img_url:
                # Cerca URL che iniziano con http/https e finiscono con .jpg (anche con parametri dopo)
                # Esclude escape json per ora, li gestiamo dopo
                raw_matches = re.findall(r'(https?:\/\/[^"\s]+\.jpg[^"\s]*)', text.replace(r'\/', '/'))
                for m in raw_matches:
                    u = html.unescape(m)
                    u_lower = u.lower()
                    # Filtri rigorosi per immagini spazzatura
                    bad_terms = [
                         's40x40', 'p50x50', 's80x80', 's200x200', 'width=40', 
                         'static.xx', 'emoji', 'profile_pic', 'blank', 
                         'rsrc.php', 'assets', 'sprite', 'icon',
                         'gray_profile', 'silhouette' # Also here
                    ]
                    if any(bt in u_lower for bt in bad_terms):
                        continue
                    
                    # Se è un link fbcdn valido, prendilo
                    if 'fbcdn.net' in u or 'facebook.com' in u:
                        img_url = u
                        break
                    if 'fbcdn.net' in u:
                        img_url = u
                        break

            if not img_url:
                logger.warning(f"Facebook fallback: no image found in page (len={len(text)})")
                if len(text) < 500:
                    logger.debug(f"Page content: {text}")
                return None

            
            # Download image
            ts = datetime.utcnow().strftime('%Y%m%d%H%M%S')
            tmp_name = os.path.join(self.temp_dir, f"fb_{ts}_fallback.jpg")
            
            def _dl_img():
                r = requests.get(img_url, headers=headers, timeout=15)
                if r.status_code == 200:
                    with open(tmp_name, 'wb') as f:
                        f.write(r.content)
                    return True
                return False
                
            success = await loop.run_in_executor(None, _dl_img)
            if success:
                # Try to get title too
                t_m = re.search(r'<title>(.*?)</title>', text)
                if t_m:
                    self.last_fallback_title = html.unescape(t_m.group(1))
                return [tmp_name]
            return None
            
        except Exception as e:
            logger.error(f"Facebook fallback error: {e}")
            return None

