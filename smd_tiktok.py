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


class TikTokMixin:
    async def _tiktok_photo_fallback(self, url: str) -> List[str]:
        """
        Fallback per pagine TikTok /photo/ che yt-dlp non riconosce.
        Scarica la pagina HTML, estrae tutte le immagini (jpg/png/webp) e le salva.
        """
        files: List[str] = []
        found_title = ""
        self.last_fallback_title = None
        headers = {
            'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 14_8 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.1.2 Mobile/15E148 Safari/604.1',
            'Referer': 'https://www.tiktok.com/',
            'Origin': 'https://www.tiktok.com'
        }
        # Use resolved tiktok_cookies
        cookie_path = self.tiktok_cookies
        
        cookies = self._load_netscape_cookies(cookie_path)
        if cookies:
             logger.info(f"Loaded {len(cookies)} cookies from {os.path.basename(cookie_path)}")
        else:
             logger.warning(f"No cookies loaded from {os.path.basename(cookie_path)}")

        try:
            logger.info(f"TikTok Fallback: fetching {url}")
            r = requests.get(
                url,
                headers=headers,
                timeout=15,
                cookies=cookies,
                proxies=self.proxy_dict
            )
            logger.info(f"TikTok Fallback: status {r.status_code}, len {len(r.text)}")
            r.raise_for_status()
            r.encoding = 'utf-8'  # TikTok è UTF-8; senza, requests usa Latin-1 -> caratteri strani
            html = r.text

            # Dump HTML solo in debug (evita di sporcare il filesystem in produzione)
            if self.debug:
                try:
                    with open(os.path.join(self.debug_dir, "tiktok_dump.html"), "w", encoding="utf-8") as f:
                        f.write(html)
                except Exception:
                    pass
        except Exception as e:
            logger.warning(f"Failed fetching TikTok page for fallback: {e}")
            return files

        # Estrai url immagine con regex
        uniq = self._extract_tiktok_photo_urls_from_html(html)
        logger.info(f"TikTok Fallback: found {len(uniq)} images from HTML")

        # Descrizione: prima la vera caption (og:description / JSON desc), poi il <title>
        # come ripiego — ma SCARTA quello generico "TikTok - Make Your Day".
        try:
            import html as _htmlmod
            desc = None
            m = re.search(r'<meta\s+property=["\']og:description["\']\s+content=["\'](.*?)["\']', html, re.IGNORECASE)
            if m and m.group(1).strip():
                desc = m.group(1)
            if not desc:
                m = re.search(r'"desc"\s*:\s*"((?:[^"\\]|\\.)*)"', html)
                if m and m.group(1).strip():
                    try:
                        desc = json.loads('"' + m.group(1) + '"')  # decodifica \uXXXX correttamente
                    except Exception:
                        desc = m.group(1)
            if not desc:
                m = re.search(r'<title>(.*?)</title>', html, re.IGNORECASE)
                if m:
                    t = m.group(1).replace('| TikTok', '').strip()
                    if t and 'make your day' not in t.lower():
                        desc = t
            if desc:
                desc = _htmlmod.unescape(desc).strip()
                found_title = desc
                self.last_fallback_title = desc
        except Exception:
            pass

        # TIKWM: serve sia se mancano le immagini SIA se manca la descrizione (l'HTML su
        # IP datacenter spesso non contiene la caption -> altrimenti resta "Contenuto").
        if not uniq or not self.last_fallback_title:
             logger.info("TikTok Fallback: interrogo TIKWM API (immagini/titolo)...")
             try:
                 api_url = "https://www.tikwm.com/api/"
                 r = requests.post(api_url, data={'url': url, 'count': 35, 'cursor': 0, 'web': 1, 'hd': 1}, timeout=15, proxies=self.proxy_dict)
                 if r.status_code == 200:
                     data = r.json()
                     if data.get('code') == 0:
                         data_obj = data.get('data', {})
                         images = data_obj.get('images', [])
                         if not uniq and images:
                             logger.info(f"TikTok Fallback: TIKWM API found {len(images)} images")
                             uniq = images
                         # La descrizione del post (TIKWM la espone come 'title')
                         t = data_obj.get('title')
                         if t and t.strip() and not self.last_fallback_title:
                             found_title = t.strip()
                             self.last_fallback_title = found_title
             except Exception as e:
                 logger.warning(f"TikTok Fallback: TIKWM API failed: {e}")

        # Se ancora niente descrizione (es. meme col testo dentro l'immagine, caption
        # vuota), usa almeno l'autore preso dall'URL (sempre presente).
        if not self.last_fallback_title:
            am = re.search(r'tiktok\.com/@([\w.\-]+)', url)
            if am:
                self.last_fallback_title = f"Post di @{am.group(1)}"

        # Limita numero di immagini
        MAX = 35
        uniq = uniq[:MAX]

        for idx, img_url in enumerate(uniq, start=1):
            ext = os.path.splitext(img_url.split('?')[0])[1].lstrip('.').lower() or 'jpg'
            if ext not in ('jpg', 'jpeg', 'png', 'webp'):
                ext = 'jpg'

            filename = os.path.join(self.temp_dir, f"tiktok_photo_{idx}.{ext}")
            try:
                rr = requests.get(
                    img_url,
                    headers=headers,
                    stream=True,
                    timeout=20,
                    cookies=cookies,
                    proxies=self.proxy_dict
                )
                rr.raise_for_status()
                with open(filename, 'wb') as fh:
                    for chunk in rr.iter_content(1024 * 256):
                        if chunk:
                            fh.write(chunk)
                if os.path.exists(filename) and os.path.getsize(filename) > 0:
                    files.append(filename)
                else:
                    try:
                        if os.path.exists(filename):
                            os.remove(filename)
                    except Exception:
                        pass
            except Exception as e:
                logger.warning(f"Failed download fallback image {img_url}: {e}")
                try:
                    if os.path.exists(filename):
                        os.remove(filename)
                except Exception:
                    pass

        return files

    def _extract_tiktok_photo_urls_from_html(self, html: str) -> List[str]:
        import re

        def _dedupe(items: List[str]) -> List[str]:
            out = []
            for x in items:
                if x not in out:
                    out.append(x)
            return out

        urls: List[str] = []

        # 1) Prova SIGI_STATE o UNIVERSAL_DATA
        # Pattern vecchio: window.SIGI_STATE = {...}
        sigi_match = re.search(r'window\.__SIGI_STATE__\s*=\s*(\{.*?\})\s*;\s*</script>', html, re.DOTALL)
        if not sigi_match:
            # Pattern alternativo: "SIGI_STATE": {...}
            sigi_match = re.search(r'\"SIGI_STATE\"\s*:\s*(\{.*?\})\s*,\s*\"AppContext', html, re.DOTALL)
        if not sigi_match:
             # Pattern script tag: <script id="SIGI_STATE">...</script>
             sigi_match = re.search(r'<script[^>]+id="SIGI_STATE"[^>]*>(.*?)</script>', html, re.DOTALL)

        # Newer TikTok structure: UNIVERSAL_DATA_FOR_REHYDRATION
        # Pattern variabile:
        univ_match = re.search(r'__UNIVERSAL_DATA_FOR_REHYDRATION__\s*=\s*(\{.*?\})\s*;</script>', html, re.DOTALL)
        if not univ_match:
            # Pattern script tag ID (più comune ora):
            # Relaxed regex to handle spacing and ordering of attributes
            univ_match = re.search(r'<script[^>]+id=[\'"]__UNIVERSAL_DATA_FOR_REHYDRATION__[\'"][^>]*>(.*?)</script>', html, re.DOTALL)

        json_data_list = []
        if sigi_match:
            json_data_list.append(sigi_match.group(1))
        if univ_match:
            json_data_list.append(univ_match.group(1))
            
        logger.info(f"TikTok Fallback: extracted {len(json_data_list)} JSON blobs")

        for idx_json, json_str in enumerate(json_data_list):
            try:
                data = json.loads(json_str)

                # DEBUG: Dump JSON to file (solo in debug, dentro la debug_dir)
                if self.debug:
                    try:
                        with open(os.path.join(self.debug_dir, f"tiktok_json_dump_{idx_json}.json"), "w", encoding="utf-8") as f:
                            json.dump(data, f, indent=2)
                    except Exception:
                        pass

                # MIRATO: prendi SOLO le slide del post foto (imagePost.images), così
                # eviti cover del video / avatar / cloni. Una sola immagine per slide.
                def find_image_post(d):
                    if isinstance(d, dict):
                        ip = d.get('imagePost') or d.get('imagePostInfo')
                        if isinstance(ip, dict) and isinstance(ip.get('images'), list):
                            for img in ip['images']:
                                if not isinstance(img, dict):
                                    continue
                                iu = img.get('imageURL') or img.get('imageUrl') or {}
                                ul = iu.get('urlList') if isinstance(iu, dict) else None
                                if ul:
                                    urls.append(ul[0])
                            return True
                        for v in d.values():
                            if find_image_post(v):
                                return True
                    elif isinstance(d, list):
                        for i in d:
                            if find_image_post(i):
                                return True
                    return False

                # Ripiego: vecchia ricerca larga (se non trovo imagePost.images)
                def recursive_find_images(d):
                    if isinstance(d, dict):
                        img = None
                        if isinstance(d.get('imageURL'), dict) and d['imageURL'].get('urlList'):
                            img = d['imageURL']['urlList']
                        elif isinstance(d.get('displayImage'), dict) and d['displayImage'].get('urlList'):
                            img = d['displayImage']['urlList']
                        if img:
                            urls.append(img[0])
                        for k, v in d.items():
                            recursive_find_images(v)
                    elif isinstance(d, list):
                        for i in d:
                            recursive_find_images(i)

                if not find_image_post(data):
                    recursive_find_images(data)
                logger.info(f"TikTok Fallback: JSON blob {idx_json} processed, total urls: {len(urls)}")
            except Exception as e:
                logger.warning(f"TikTok Fallback: JSON blob {idx_json} parse error: {e}")
                pass

        # 2) Regex generico immagini
        # Cerca URL che sembrano immagini tiktok
        # Spesso sono https://p16-sign-va.tiktokcdn.com/...o qualcosa del genere
        if not urls:
            logger.info("TikTok Fallback: No JSON data found, trying regex...")
            # Pattern broad per URL immagini dentro stringhe
            pattern = re.compile(r'"(https?://[^"\s]+\.(?:jpeg|jpg|png|webp)[^"]*)"', re.IGNORECASE)
            matches = pattern.findall(html)
            # Filtra per domini tiktok se possibile, o prendi tutto
            for m in matches:
                # Decodifica unicode escape se presente
                m_dec = m.encode().decode('unicode_escape')
                if 'tiktokcdn' in m_dec or 'tiktok' in m_dec:
                    urls.append(m_dec)

        # 3) Meta og:image come ultima risorsa
        if not urls:
            meta_patterns = [
                re.compile(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', re.IGNORECASE),
                re.compile(r'<meta[^>]+name=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', re.IGNORECASE),
            ]
            for mp in meta_patterns:
                mm = mp.findall(html)
                urls.extend(mm)

        return _dedupe(urls)

