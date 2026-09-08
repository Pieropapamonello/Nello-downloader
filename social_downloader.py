#!/usr/bin/env python3
"""
Social Media Downloader v4.2
- VIDEO + CAROSELLO FOTO (Instagram/TikTok/Facebook/etc.)
- Retry robusto
- URL cleaning (TikTok short + Facebook share)
- Return standardizzato per bot Telegram:
  - {"success": True, "type": "video", "file_path": "...", ...}
  - {"success": True, "type": "carousel", "files": ["...","..."], ...}
"""

import os
import asyncio
import logging
import tempfile
import time
from typing import Dict, Optional, List, Tuple
import http.cookiejar

try:
    import cloudscraper
except ImportError:
    cloudscraper = None

import yt_dlp
from yt_dlp.version import __version__ as yt_version
import requests
import json
import re
import html
from media_resources import limited_media
from youtube_job import run_youtube_job, YouTubeResourceError
from youtube_duration import youtube_duration
from datetime import datetime

logger = logging.getLogger(__name__)


from smd_tiktok import TikTokMixin
from smd_instagram import InstagramMixin
from smd_facebook import FacebookMixin, facebook_video_id, is_facebook_video_url
from smd_cobalt import CobaltMixin


class SocialMediaDownloader(TikTokMixin, InstagramMixin, FacebookMixin, CobaltMixin):
    def __init__(self, debug: bool = False):
        logger.info(f"Yt-dlp version: {yt_version}")
        self.temp_dir = tempfile.gettempdir()

        # Funzione helper per risolvere i path dei cookie (Supporto Render Secret Files & Env Vars)
        def resolve_cookie_path(filename, env_var_names=None):
            # 1. Cerca PRIMA nella directory corrente (git repo) per permettere l'override
            # Questo permette di fixare i cookie semplicemente pushando un nuovo file, ignorando i secret vecchi
            local_path = os.path.join(os.path.dirname(__file__), filename)
            if os.path.exists(local_path) and os.path.getsize(local_path) > 10:
                logger.info(f"Uso cookie locale (repository) prioritario: {local_path}")
                return local_path

            # 2. Cerca nelle variabili d'ambiente (PRIORITÀ ALTA per Render)
            if env_var_names:
                if isinstance(env_var_names, str):
                    env_var_names = [env_var_names]
                
                for env_var in env_var_names:
                    content = os.getenv(env_var)
                    if content and len(content.strip()) > 10: # Check minimo validità
                        try:
                            # Crea un file temporaneo con il contenuto
                            temp_cookie_path = os.path.join(self.temp_dir, f"env_{filename}")
                            with open(temp_cookie_path, 'w', encoding='utf-8') as f:
                                f.write(content)
                            logger.info(f"Cookie creato da variabile d'ambiente {env_var}: {temp_cookie_path}")
                            return temp_cookie_path
                        except Exception as e:
                            logger.error(f"Errore scrittura cookie da env {env_var}: {e}")

            # 3. Cerca in /etc/secrets/ (standard Render Secret Files)
            found_secret_path = None
            
            # 3a. Cerca con il filename originale (es. cookies.txt)
            path_orig = os.path.join('/etc/secrets', filename)
            if os.path.exists(path_orig):
                found_secret_path = path_orig

            # 3b. Cerca con i nomi delle variabili (es. INSTAGRAM_COOKIES) se non trovato prima
            if not found_secret_path and env_var_names:
                if isinstance(env_var_names, str):
                    check_names = [env_var_names]
                else:
                    check_names = env_var_names
                
                for name in check_names:
                    path_alias = os.path.join('/etc/secrets', name)
                    if os.path.exists(path_alias):
                        found_secret_path = path_alias
                        break
            
            # Se abbiamo trovato un secret, lo copiamo in TEMP perché /etc/secrets è Read-Only
            if found_secret_path:
                try:
                    logger.info(f"Cookie trovato in secrets: {found_secret_path}")
                    # Creiamo un nome file sicuro per la temp dir
                    safe_name = f"copy_{os.path.basename(found_secret_path)}_{filename}"
                    temp_copy_path = os.path.join(self.temp_dir, safe_name)
                    
                    # Copia contenuto
                    with open(found_secret_path, 'rb') as f_src:
                        content = f_src.read()
                    with open(temp_copy_path, 'wb') as f_dst:
                        f_dst.write(content)
                        
                    logger.info(f"Copiato cookie secret in scrivibile: {temp_copy_path}")
                    return temp_copy_path
                except Exception as e:
                    logger.error(f"Errore durante la copia del secret file {found_secret_path}: {e}")
                    # Se fallisce la copia, ritorniamo l'originale sperando che yt-dlp non debba scriverci
                    return found_secret_path

            logger.warning(f"Cookie {filename} non trovato da nessuna parte.")
            return local_path # Ritorna il percorso locale come default per evitare crash immediati su path null

        # Percorsi cookies
        self.instagram_cookies = resolve_cookie_path('instagram_cookies.txt', ['INSTAGRAM_COOKIES', 'COOKIES_TXT'])
        self.youtube_cookies = resolve_cookie_path('youtube_cookies.txt', 'YOUTUBE_COOKIES')
        self.tiktok_cookies = resolve_cookie_path('tiktok_cookies.txt', 'TIKTOK_COOKIES')
        self.facebook_cookies = resolve_cookie_path('facebook_cookies.txt', 'FACEBOOK_COOKIES')

        # Proxy opzionale (es. http://user:pass@host:port)
        self.proxy = (
            os.getenv('SMD_PROXY')
            or os.getenv('HTTPS_PROXY')
            or os.getenv('HTTP_PROXY')
        )
        self.proxy = self.proxy.strip() if self.proxy else None
        self.proxy_dict = {'http': self.proxy, 'https': self.proxy} if self.proxy else None
        
        # State per i fallback
        self.last_fallback_title = None

        # User-Agent pool
        self.user_agents = [
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        ]

        # Base options yt-dlp
        self.base_opts = {
            # Preferisci un mp4 progressivo CHE ABBIA AUDIO ([acodec!=none]); se il
            # "best" mp4 e' solo-video (capita sui reel Instagram in DASH), unisci
            # bestvideo+bestaudio (ffmpeg c'e'). Senza questo, certi video uscivano muti.
            'format': 'best[ext=mp4][acodec!=none]/bestvideo*+bestaudio/best',
            'merge_output_format': 'mp4',
            'outtmpl': os.path.join(self.temp_dir, '%(title).150s_%(id)s.%(ext)s'),
            'quiet': True,
            'no_warnings': True,
            'socket_timeout': 30,
            'max_filesize': 50 * 1024 * 1024,
        }

        self.max_retries = 3
        self.retry_delay = 2
        # YouTube non scarica mai oltre 3 minuti. La variabile permette solo un
        # limite piu' restrittivo, non di superare questo tetto.
        try:
            configured_youtube_limit = int(os.getenv('YOUTUBE_MAX_DURATION', '180'))
        except ValueError:
            configured_youtube_limit = 180
        self.youtube_max_duration = min(max(configured_youtube_limit, 1), 180)
        self.debug = bool(debug)
        self._last_info = None
        if self.debug:
            self.debug_dir = os.path.join(self.temp_dir, 'smd_debug')
            try:
                os.makedirs(self.debug_dir, exist_ok=True)
            except Exception:
                pass

    def _load_netscape_cookies(self, path: str) -> Optional[Dict[str, str]]:
        if not path or not os.path.exists(path):
            return None
        cookies: Dict[str, str] = {}
        try:
            with open(path, 'r', encoding='utf-8') as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith('#'):
                        continue
                    parts = line.split('\t')
                    if len(parts) >= 7:
                        name = parts[5]
                        value = parts[6]
                        cookies[name] = value
        except Exception:
            return None
        return cookies or None

    def _save_debug_info(self, note: str = '') -> Optional[str]:
        if not self.debug or not self._last_info:
            return None
        try:
            ts = datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')
            safe_note = ''.join([c for c in note if c.isalnum() or c in ('_', '-')])[:40]
            fname = f"info_{ts}_{safe_note}.json" if safe_note else f"info_{ts}.json"
            path = os.path.join(self.debug_dir, fname)
            with open(path, 'w', encoding='utf-8') as fh:
                json.dump(self._last_info, fh, default=str, indent=2, ensure_ascii=False)
            logger.info(f"Saved debug info to {path}")
            return path
        except Exception as e:
            logger.warning(f"Failed saving debug info: {e}")
            return None

    # --------------------------
    # Helpers
    # --------------------------

    def get_random_user_agent(self) -> str:
        import random
        return random.choice(self.user_agents)

    def get_ydl_opts(self, url: str, attempt: int = 0) -> Dict:
        """Opzioni yt-dlp personalizzate per piattaforma"""
        opts = self.base_opts.copy()

        opts['http_headers'] = {
            'User-Agent': self.get_random_user_agent(),
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        }

        if self.proxy:
            opts['proxy'] = self.proxy

        # --- STRATEGIA DI DOWNLOAD (Priorità No-Cookies su Render) ---
        # Attempt 0: NO Cookies (prova accesso pubblico/mobile client)
        # Attempt 1: SI Cookies (se disponibili, fallback authenticated)
        # Attempt 2: NO Cookies (fallback aggressivo / scraping)

        # Instagram
        if 'instagram' in url.lower():
            # Story/reel: il link punta a UN singolo item. Senza questo, con i cookie
            # yt-dlp scaricava l'intero "tray" di storie (3 reel invece di 1).
            if '/stories/' in url.lower() or '/reel/' in url.lower():
                opts['noplaylist'] = True
            # Tentativo 0: Senza cookies (spesso funziona meglio su IP server puliti)
            if attempt == 0:
                pass 
            # Tentativo 1: Con cookies (se esistono)
            elif attempt == 1 and os.path.exists(self.instagram_cookies):
                 opts['cookiefile'] = self.instagram_cookies
            # Tentativo 2: Con cookies (ultimo tentativo)
            elif attempt >= 2 and os.path.exists(self.instagram_cookies):
                 opts['cookiefile'] = self.instagram_cookies

        # YouTube: strategie anti-block
        if 'youtube' in url.lower() or 'youtu.be' in url.lower():
            # Preferisci il formato progressivo mp4 (es. itag 18: audio+video gia' uniti,
            # nessun merge necessario). Fallback su adattivo+merge solo se serve.
            opts['format'] = 'best[ext=mp4][acodec!=none]/bestvideo+bestaudio/best'
            opts['merge_output_format'] = 'mp4'

            opts['http_headers'].update({
                'Referer': 'https://www.youtube.com/',
            })

            # One client per disposable process; web clients support account cookies.
            opts['js_runtimes'] = {'deno': {}}
            clients = ('web_safari', 'mweb', 'tv')
            opts['extractor_args'] = {
                'youtube': {'player_client': [clients[min(attempt, 2)]]},
                'youtubepot-bgutilhttp': {'base_url': ['http://127.0.0.1:4416']},
            }
            if attempt == 0:
                # Safari HLS does not need a GVS token. Avoid generating an
                # unused HTTPS token alongside the JavaScript challenge solver.
                opts['extractor_args']['youtube']['fetch_pot'] = ['never']
                opts['format'] = 'best[protocol^=m3u8]/best'
            if os.path.exists(self.youtube_cookies):
                opts['cookiefile'] = self.youtube_cookies

        # Facebook
        if 'facebook' in url.lower() or 'fb.' in url.lower():
            opts['format'] = 'bestvideo[height<=480]+bestaudio/best[height<=480]/best'
            opts['http_headers'].update({
                'Referer': 'https://www.facebook.com/',
                'Origin': 'https://www.facebook.com',
            })
            
            # Facebook è difficile. 
            # Attempt 0: No Cookies (Public access)
            # Attempt 1+: Cookies
            if attempt > 0 and os.path.exists(self.facebook_cookies):
                opts['cookiefile'] = self.facebook_cookies

        # TikTok headers
        if 'tiktok' in url.lower():
            opts['http_headers'].update({
                'Referer': 'https://www.tiktok.com/',
                'Origin': 'https://www.tiktok.com',
            })
            # Use cookies only on retries or if forced
            if attempt > 0 and os.path.exists(self.tiktok_cookies):
                opts['cookiefile'] = self.tiktok_cookies

        return opts

    def clean_url(self, url: str) -> str:
        """Pulisce URL e risolve short link (TikTok / Facebook share)"""
        url = url.strip()

        # Facebook /share/
        if 'facebook.com/share/' in url:
            try:
                response = requests.head(
                    url,
                    allow_redirects=True,
                    timeout=10,
                    headers={'User-Agent': self.get_random_user_agent()},
                    proxies=self.proxy_dict
                )
                url = response.url
            except Exception:
                pass

        # TikTok short URLs
        if 'vm.tiktok.com' in url or 'vt.tiktok.com' in url:
            try:
                headers = {
                    'User-Agent': self.get_random_user_agent(),
                    'Referer': 'https://www.tiktok.com/',
                    'Origin': 'https://www.tiktok.com'
                }
                cookies = self._load_netscape_cookies(self.tiktok_cookies)

                response = requests.head(
                    url,
                    allow_redirects=True,
                    timeout=10,
                    headers=headers,
                    cookies=cookies,
                    proxies=self.proxy_dict
                )
                if response.url and '/login' not in response.url:
                    url = response.url
                else:
                    # Fallback GET se HEAD porta a /login
                    response = requests.get(
                        url,
                        allow_redirects=True,
                        timeout=15,
                        headers=headers,
                        cookies=cookies,
                        proxies=self.proxy_dict
                    )
                    if response.url:
                        url = response.url
            except Exception:
                pass

        # Smart clean parameters (strip tracking, keep functional)
        if '?' in url:
            from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
            parsed = urlparse(url)
            params = parse_qs(parsed.query)
            
            # List of params to remove
            tracking_params = [
                'fbclid', 'igsh', 'si', 'utm_source', 'utm_medium', 
                'utm_campaign', 'utm_term', 'utm_content', 'share_id',
                'mode', 'u_code', 'timestamp', 'user_id', 'sec_user_id',
                'utm_id', 'gclid', '_ga'
            ]
            
            # Keep only safe params
            new_params = {}
            for k, v in params.items():
                if k.lower() not in tracking_params:
                    new_params[k] = v
                    
            # Reconstruct
            new_query = urlencode(new_params, doseq=True)
            url = urlunparse((
                parsed.scheme,
                parsed.netloc,
                parsed.path,
                parsed.params,
                new_query,
                parsed.fragment
            ))

        return url

    def detect_platform(self, url: str) -> str:
        """Rileva piattaforma"""
        u = url.lower()
        if 'tiktok' in u:
            return 'tiktok'
        if 'instagram' in u or 'ig.tv' in u:
            return 'instagram'
        if 'facebook' in u or 'fb.' in u:
            return 'facebook'
        if 'youtube' in u or 'youtu.be' in u:
            return 'youtube'
        if 'twitter' in u or 'x.com' in u:
            return 'twitter'
        return 'unknown'

    def get_error_message_for_platform(self, platform: str, error_type: str) -> str:
        messages = {
            'youtube': {
                'extraction_failed': '🤖 YouTube chiede autenticazione. Riprova tra poco.',
                'download_failed': '⚠️ Non riesco a scaricare lo short. Riprova.',
            },
            'facebook': {
                'extraction_failed': '⚠️ Facebook ha cambiato struttura. Riprova con un altro reel.',
                'download_failed': '🔒 Non riesco a scaricare il reel. Potrebbe essere privato.',
            },
            'instagram': {
                'extraction_failed': '🔒 Post privato o cookies scaduti.',
                'download_failed': '📸 Non riesco a scaricare il contenuto Instagram.',
            },
            'tiktok': {
                'extraction_failed': '⚠️ Errore nel caricamento da TikTok.',
                'download_failed': '🔒 Contenuto TikTok non disponibile.',
            },
        }
        return messages.get(platform, {}).get(error_type, '❌ Errore nel download.')

    # --------------------------
    # Core: extract + download
    # --------------------------

    async def extract_info(self, url: str, attempt: int = 0) -> Optional[Dict]:
        """Estrae info (senza download)"""
        # Nota: rimuoviamo il try/catch interno per permettere a download_video
        # di intercettare errori specifici (es. Unsupported URL)
        if attempt > 0 and is_facebook_video_url(url) and facebook_video_id(url):
            url = 'https://www.facebook.com/watch/?v=' + facebook_video_id(url)
        opts = self.get_ydl_opts(url, attempt)
        opts['skip_download'] = True

        loop = asyncio.get_event_loop()

        @limited_media
        def _extract():
            if self.detect_platform(url) == 'youtube':
                return run_youtube_job(opts, url)['info']
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=False)

        return await loop.run_in_executor(None, _extract)

    @staticmethod
    def _youtube_duration_seconds(info: Dict) -> Optional[int]:
        """Restituisce la durata YouTube in secondi, oppure None se non verificabile."""
        value = info.get('duration')
        if isinstance(value, bool):
            value = None
        if value is not None:
            try:
                duration = int(float(value))
                if duration >= 0:
                    return duration
            except (TypeError, ValueError, OverflowError):
                pass

        duration_string = info.get('duration_string')
        if not isinstance(duration_string, str):
            return None
        parts = duration_string.strip().split(':')
        if len(parts) not in (2, 3) or not all(part.isdigit() for part in parts):
            return None
        values = [int(part) for part in parts]
        if len(values) == 2:
            minutes, seconds = values
            return minutes * 60 + seconds if seconds < 60 else None
        hours, minutes, seconds = values
        return hours * 3600 + minutes * 60 + seconds if minutes < 60 and seconds < 60 else None

    def _pick_best_image_url(self, entry: Dict) -> Optional[Tuple[str, str]]:
        """
        Ritorna (url, ext) migliore per una singola slide immagine.
        Prova entry['url'] oppure i formats.
        """
        # Caso semplice
        u = entry.get('url')
        ext = (entry.get('ext') or '').lower()
        if u and ext in ('jpg', 'jpeg', 'png', 'webp'):
            return u, ('jpg' if ext == 'jpeg' else ext)

        # Prova formats
        formats = entry.get('formats') or []
        candidates = []
        for f in formats:
            fu = f.get('url')
            fext = (f.get('ext') or '').lower()
            if fu and fext in ('jpg', 'jpeg', 'png', 'webp'):
                score = (f.get('width') or 0) * (f.get('height') or 0)
                score = score if score > 0 else (f.get('filesize') or 0)
                candidates.append((score, fu, ('jpg' if fext == 'jpeg' else fext)))

        if candidates:
            candidates.sort(key=lambda x: x[0], reverse=True)
            _, fu, fext = candidates[0]
            return fu, fext

        # Supporto campi immagine tipici di Instagram
        # display_url, original_url, thumbnail, image_url
        img_fields = [
            ('display_url', 'jpg'),
            ('original_url', 'jpg'),
            ('thumbnail', 'jpg'),
            ('thumbnail_url', 'jpg'),
            ('image_url', 'jpg'),
        ]
        for key, guessed_ext in img_fields:
            v = entry.get(key)
            if v and isinstance(v, str) and v.startswith('http'):
                return v, guessed_ext

        # Alcuni extractor (instagram) forniscono display_resources o image_versions2
        dr = entry.get('display_resources') or []
        if dr and isinstance(dr, list):
            candidates = []
            for r in dr:
                src = r.get('src') or r.get('url')
                width = r.get('config_width') or r.get('width') or 0
                if src and isinstance(src, str):
                    candidates.append((width or 0, src))
            if candidates:
                candidates.sort(key=lambda x: x[0], reverse=True)
                return candidates[0][1], 'jpg'

        iv2 = entry.get('image_versions2') or {}
        cand = iv2.get('candidates') or []
        if cand and isinstance(cand, list):
            candidates = []
            for c in cand:
                src = c.get('url')
                width = c.get('width') or 0
                if src:
                    candidates.append((width, src))
            if candidates:
                candidates.sort(key=lambda x: x[0], reverse=True)
                return candidates[0][1], 'jpg'

        return None

    def _is_playlist_like(self, info: Dict) -> bool:
        return isinstance(info, dict) and isinstance(info.get('entries'), list) and len(info.get('entries')) > 0

    def _pick_best_video_url(self, entry: Dict) -> Optional[Tuple[str, str]]:
        """
        Ritorna (url, ext) migliore per una singola slide video.
        Prova entry['url'] oppure i formats. Restituisce None se non trova video.
        """
        video_exts = ('mp4', 'm4v', 'mov', 'webm', 'mkv', 'ts', '3gp')

        # Cerca PRIMA nei formats, preferendo i progressivi CON audio: su Instagram
        # lo stream a risoluzione più alta è spesso DASH solo-video (audio separato),
        # e prenderlo dava video MUTI. Quindi ordina per (ha_audio, risoluzione).
        formats = entry.get('formats') or []
        candidates = []
        for f in formats:
            fu = f.get('url')
            fext = (f.get('ext') or '').lower()
            if fu and fext in video_exts:
                acodec = (f.get('acodec') or '').lower()
                has_audio = acodec not in ('', 'none')
                res = (f.get('width') or 0) * (f.get('height') or 0)
                res = res if res > 0 else (f.get('filesize') or 0)
                candidates.append(((1 if has_audio else 0, res), fu, ('mp4' if fext == 'm4v' else fext)))

        if candidates:
            candidates.sort(key=lambda x: x[0], reverse=True)
            (ha, _res), fu, fext = candidates[0]
            return fu, fext, bool(ha)

        # Niente formats utili: prova entry['url'] (assumiamo abbia audio: di solito
        # è il progressivo). Se fosse muto, il merge non servirebbe comunque qui.
        u = entry.get('url')
        ext = (entry.get('ext') or '').lower()
        if u and ext in video_exts:
            return u, ('mp4' if ext == 'm4v' else ext), True

        # Fallback: alcuni extractor mettono video_url o video
        for key in ('video_url', 'video', 'video_src'):
            v = entry.get(key)
            if v and isinstance(v, str) and v.startswith('http'):
                guessed = os.path.splitext(v.split('?')[0])[1].lstrip('.') or 'mp4'
                guessed = guessed if guessed in video_exts else 'mp4'
                return v, guessed, True

        return None

    def _pick_best_audio_url(self, entry: Dict) -> Optional[Tuple[str, str]]:
        """URL del miglior formato SOLO-audio (per i video DASH di Instagram, dove
        l'audio è in uno stream separato). Ritorna (url, ext) o None."""
        cands = []
        for f in entry.get('formats') or []:
            fu = f.get('url')
            if not fu:
                continue
            acodec = (f.get('acodec') or '').lower()
            vcodec = (f.get('vcodec') or '').lower()
            if acodec in ('', 'none'):
                continue          # deve avere audio
            if vcodec not in ('', 'none'):
                continue          # deve essere solo-audio (no video)
            abr = f.get('abr') or f.get('tbr') or 0
            ext = (f.get('ext') or 'm4a').lower()
            cands.append((abr, fu, ext))
        if cands:
            cands.sort(key=lambda x: x[0], reverse=True)
            return cands[0][1], cands[0][2]
        return None

    def _merge_audio_if_possible(self, entry, video_path, safe_id, idx, headers):
        """Scarica lo stream audio separato (DASH) e lo unisce al video con ffmpeg.
        Ritorna il path del file unito, o il video originale se non c'è audio/fallisce."""
        import subprocess
        a = self._pick_best_audio_url(entry)
        if not a:
            logger.info(f"Carousel idx={idx}: nessuno stream audio separato, resta muto")
            return video_path
        audio_url, aext = a
        audio_path = os.path.join(self.temp_dir, f"carousel_{safe_id}_{idx}_audio.{aext or 'm4a'}")
        try:
            r = requests.get(audio_url, headers=headers, stream=True, timeout=60, proxies=self.proxy_dict)
            r.raise_for_status()
            with open(audio_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=1024 * 256):
                    if chunk:
                        f.write(chunk)
        except Exception as e:
            logger.warning(f"Carousel idx={idx}: download audio fallito: {str(e)[:120]}")
            try:
                if os.path.exists(audio_path):
                    os.remove(audio_path)
            except Exception:
                pass
            return video_path
        if not (os.path.exists(audio_path) and os.path.getsize(audio_path) > 0):
            return video_path

        merged = os.path.join(self.temp_dir, f"carousel_{safe_id}_{idx}_av.mp4")
        cmd = ['ffmpeg', '-y', '-threads', '1', '-i', video_path, '-i', audio_path,
               '-c:v', 'copy', '-c:a', 'aac', '-map', '0:v:0', '-map', '1:a:0',
               '-shortest', merged]
        try:
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180)
        except Exception as e:
            logger.warning(f"Carousel idx={idx}: merge ffmpeg fallito: {str(e)[:120]}")

        if os.path.exists(merged) and os.path.getsize(merged) > 0:
            for p in (video_path, audio_path):
                try:
                    os.remove(p)
                except Exception:
                    pass
            logger.info(f"Carousel idx={idx}: audio DASH unito al video")
            return merged
        # merge fallito: tieni il video (muto), pulisci i temporanei
        for p in (audio_path, merged):
            try:
                if os.path.exists(p):
                    os.remove(p)
            except Exception:
                pass
        return video_path

    async def _download_carousel_items(self, info: Dict) -> List[str]:
        """
        Scarica immagini e video da info['entries'] (carosello) e ritorna file paths.
        Gestisce slide che possono essere immagini o video.
        """
        files: List[str] = []
        entries = info.get('entries') or []
        headers = {'User-Agent': self.get_random_user_agent()}

        for idx, entry in enumerate(entries, start=1):
            safe_id = entry.get('id') or f"{idx}"

            # Determina se la slide è video
            is_video = bool(entry.get('is_video'))
            # Alcuni extractor non impostano is_video ma hanno formats video
            if not is_video:
                fmts = entry.get('formats') or []
                for f in fmts:
                    fext = (f.get('ext') or '').lower()
                    if fext in ('mp4', 'webm', 'mov', 'mkv'):
                        is_video = True
                        break

            if is_video:
                best = self._pick_best_video_url(entry)
                if not best:
                    logger.warning(f"Nessun url video trovato per slide idx={idx}")
                    continue

                video_url, ext, has_audio = best
                filename = os.path.join(self.temp_dir, f"carousel_{safe_id}_{idx}.{ext}")

                try:
                    r = requests.get(
                        video_url,
                        headers=headers,
                        stream=True,
                        timeout=60,
                        proxies=self.proxy_dict
                    )
                    r.raise_for_status()
                    with open(filename, "wb") as f:
                        for chunk in r.iter_content(chunk_size=1024 * 256):
                            if chunk:
                                f.write(chunk)

                    if os.path.exists(filename) and os.path.getsize(filename) > 0:
                        # Video solo-video (DASH Instagram): scarica l'audio separato
                        # e uniscilo, altrimenti il video uscirebbe muto.
                        if not has_audio:
                            filename = self._merge_audio_if_possible(entry, filename, safe_id, idx, headers)
                        files.append(filename)
                    else:
                        try:
                            if os.path.exists(filename):
                                os.remove(filename)
                        except Exception:
                            pass

                except Exception as e:
                    logger.warning(f"Carousel video download failed idx={idx}: {str(e)[:120]}")
                    try:
                        if os.path.exists(filename):
                            os.remove(filename)
                    except Exception:
                        pass

            else:
                # Tratta come immagine
                best = self._pick_best_image_url(entry)
                if not best:
                    continue

                img_url, ext = best
                filename = os.path.join(self.temp_dir, f"carousel_{safe_id}_{idx}.{ext}")

                try:
                    r = requests.get(
                        img_url,
                        headers=headers,
                        stream=True,
                        timeout=20,
                        proxies=self.proxy_dict
                    )
                    r.raise_for_status()
                    with open(filename, "wb") as f:
                        for chunk in r.iter_content(chunk_size=1024 * 256):
                            if chunk:
                                f.write(chunk)

                    if os.path.exists(filename) and os.path.getsize(filename) > 0:
                        files.append(filename)
                except Exception as e:
                    logger.warning(f"Carousel img download failed idx={idx}: {str(e)[:120]}")
                    try:
                        if os.path.exists(filename):
                            os.remove(filename)
                    except Exception:
                        pass

        return files









    async def download_with_ytdlp(self, url: str, attempt: int = 0, info: Dict = None) -> Optional[str]:
        """Download singolo (video) con yt-dlp"""
        try:
            opts = self.get_ydl_opts(url, attempt)
            
            # Nota: Strategie specifiche (es. Android client per Youtube) sono ora gestite
            # direttamente dentro get_ydl_opts in base al numero del tentativo.
            
            loop = asyncio.get_event_loop()

            @limited_media
            def _download():
                if self.detect_platform(url) == 'youtube':
                    return run_youtube_job(opts, url, download=True, info=info)['filename']
                with yt_dlp.YoutubeDL(opts) as ydl:
                    # Reuse the metadata already fetched and duration-checked.
                    # Avoid two additional player/JS challenge extractions.
                    info2 = (ydl.process_ie_result(dict(info), download=True) if info
                             else ydl.extract_info(url, download=True))
                    candidates = [info2.get('filepath'), ydl.prepare_filename(info2)]
                    candidates.extend(d.get('filepath') for d in info2.get('requested_downloads', []))
                    for candidate in candidates:
                        if candidate and os.path.isfile(candidate):
                            return candidate
                    logger.warning('yt-dlp produced no expected file; expected=%s actual=%s',
                                   [os.path.basename(c) for c in candidates if c],
                                   [(n, os.path.getsize(os.path.join(self.temp_dir, n)))
                                    for n in os.listdir(self.temp_dir)
                                    if not n.endswith('.json') and os.path.isfile(os.path.join(self.temp_dir, n))])
                    return ydl.prepare_filename(info2)

            filename = await loop.run_in_executor(None, _download)

            if filename and os.path.exists(filename):
                return filename

            # Fallback estensioni
            if filename:
                base = os.path.splitext(filename)[0]
                for ext in ['.mp4', '.webm', '.mkv', '.mov', '.avi', '.flv']:
                    test_file = base + ext
                    if os.path.exists(test_file):
                        return test_file

            return None

        except YouTubeResourceError:
            raise
        except Exception as e:
            logger.error(f"Download attempt {attempt}: {str(e)[:200]}")
            return None

    async def get_random_video_url(self, user_url: str) -> Optional[str]:
        """Recupera un URL video casuale da un profilo (TikTok/Instagram/etc)"""
        import random
        try:
            opts = self.get_ydl_opts(user_url)
            opts.update({
                'extract_flat': True,
                'playlistend': 20, # Fetch latest 20
                'quiet': True,
            })
            
            loop = asyncio.get_event_loop()
            def _extract():
                with yt_dlp.YoutubeDL(opts) as ydl:
                    return ydl.extract_info(user_url, download=False)
            
            info = await loop.run_in_executor(None, _extract)
            if not info: 
                return None
            
            entries = info.get('entries')
            if not entries:
                return None
                
            # entries might be a generator or list
            valid_urls = []
            for e in entries:
                if e.get('url'):
                    valid_urls.append(e['url'])
            
            if valid_urls:
                return random.choice(valid_urls)
            return None
        except Exception as e:
            logger.warning(f"Failed getting random video from {user_url}: {e}")
            return None

    # --------------------------
    # Cobalt API Fallback (The "No Cookies" Savior)
    # --------------------------
    

    # --------------------------
    # Public API
    # --------------------------

    def _dedupe_files(self, files: List[str]) -> List[str]:
        """Rimuove file scaricati IDENTICI (stesso contenuto) -> niente duplicati nei caroselli."""
        import hashlib
        seen = {}
        out = []
        for f in files:
            try:
                with open(f, 'rb') as fh:
                    h = hashlib.md5(fh.read()).hexdigest()
            except Exception:
                out.append(f)
                continue
            if h in seen:
                try:
                    os.remove(f)  # elimina il duplicato dal disco
                except Exception:
                    pass
                continue
            seen[h] = f
            out.append(f)
        return out

    def _pack_media_result(self, files: List[str], title, uploader, platform, url) -> Dict:
        """Impacchetta un risultato: un solo VIDEO -> type 'video' (votabile inline);
        altrimenti carosello (con file deduplicati)."""
        files = self._dedupe_files(files)
        vids = ('.mp4', '.m4v', '.mov', '.webm', '.mkv', '.avi', '.flv', '.ts')
        if len(files) == 1 and os.path.splitext(files[0])[1].lower() in vids:
            return {'success': True, 'type': 'video', 'file_path': files[0],
                    'title': title, 'uploader': uploader, 'platform': platform, 'url': url}
        return {'success': True, 'type': 'carousel', 'files': files,
                'title': title, 'uploader': uploader, 'platform': platform, 'url': url}

    async def download_video(self, url: str, on_download_ready=None) -> Dict:
        """
        Main download.
        Ritorna:
        - success False => {success: False, error: "..."}
        - success True & video => {success: True, type:'video', file_path:'...', title/uploader/platform/url}
        - success True & carousel => {success: True, type:'carousel', files:[...], title/uploader/platform/url}
        """
        if os.getenv('DOWNLOADER_URL'):
            from remote_downloader import remote_download
            return await remote_download(url, target=getattr(self, 'delivery_platform', ''),
                                         max_bytes=getattr(self, 'delivery_max_bytes', 16 * 1024 * 1024),
                                         on_download_ready=on_download_ready)
        clean_url = self.clean_url(url)
        platform = self.detect_platform(clean_url)
        if platform == 'youtube':
            duration = await asyncio.to_thread(youtube_duration, clean_url,
                                              getattr(self, 'youtube_cookies', None),
                                              getattr(self, 'proxy_dict', None))
            if duration is None:
                logger.info('YouTube duration unavailable: left as link, no extraction attempted.')
                return {'success': False, 'error': (
                    'YouTube non permette di verificare la durata di questo video. '
                    'Non avvio il download: il link resta disponibile.')}
            if duration > self.youtube_max_duration:
                logger.info('YouTube duration %.1fs exceeds %ss: no extraction attempted.',
                            duration, self.youtube_max_duration)
                return {'success': False, 'skip_long': True}
        # Azzera il titolo dei fallback (il downloader è un singleton: evita titoli "vecchi")
        self.last_fallback_title = None

        # TikTok photo: prova subito fallback (yt-dlp spesso non supporta /photo/)
        if platform == 'tiktok' and '/photo/' in clean_url:
            try:
                files = await self._tiktok_photo_fallback(clean_url)
                if files:
                    return self._pack_media_result(
                        files,
                        getattr(self, 'last_fallback_title', None) or 'Contenuto',
                        'Sconosciuto', platform, clean_url)
            except Exception:
                pass

        force_fallback = False
        title = 'Contenuto'
        uploader = 'Sconosciuto'
        
        # --- PHASE 1: STANDARD YT-DLP ATTEMPTS ---
        for attempt in range(self.max_retries):
            try:
                logger.info(f"Tentativo {attempt + 1}/{self.max_retries} per {platform}: {clean_url}")

                info = await self.extract_info(clean_url, attempt)
                # conserva l'ultimo info estratto per debug
                self._last_info = info
                if not info:
                    logger.warning('Extractor returned no metadata: platform=%s attempt=%d', platform, attempt + 1)
                    if attempt < self.max_retries - 1:
                        delay = self.retry_delay * (2 ** attempt)
                        await asyncio.sleep(delay)
                        continue
                    # Se finiti tentativi yt-dlp, break e vai ai fallback
                    break 

                if platform == 'facebook' and is_facebook_video_url(clean_url):
                    expected_id = facebook_video_id(clean_url)
                    if expected_id and str(info.get('id')) != expected_id:
                        logger.warning('Facebook returned a different media ID; rejected')
                        continue
                    if self._is_playlist_like(info):
                        logger.warning('Facebook reel returned a playlist; rejected')
                        continue

                # Uploader/title (fallbacks)
                uploader = info.get('uploader') or info.get('channel') or info.get('creator') or 'Sconosciuto'
                title = info.get('title') or 'Contenuto'

                # YouTube: scarica solo i video <= 3 minuti; quelli più lunghi
                # vengono lasciati come link in chat (skip_long).
                if platform == 'youtube':
                    dur = self._youtube_duration_seconds(info)
                    if dur is None:
                        return {'success': False, 'error': 'Impossibile verificare la durata YouTube. Riprova tra poco.'}
                    if dur > self.youtube_max_duration:
                        logger.info(f"YouTube {dur}s > {self.youtube_max_duration}s: lasciato come link.")
                        return {'success': False, 'skip_long': True}

                    if on_download_ready:
                        try:
                            callback_result = on_download_ready()
                            if asyncio.iscoroutine(callback_result):
                                await callback_result
                        except Exception as e:
                            logger.warning(f"Impossibile mostrare lo stato di download: {e}")

                # 1) Se è carosello/playlist -> prova a scaricare immagini/video
                if self._is_playlist_like(info):
                    items = await self._download_carousel_items(info)
                    if items:
                        return self._pack_media_result(items, title, uploader, platform, clean_url)
                    # Se non riesce a scaricare immagini/video, prova comunque come video
                    logger.info("Carosello rilevato ma nessuna immagine/video scaricata. Provo come video...")
                    if self.debug:
                        self._save_debug_info('carousel_no_items')

                # 2) Prova come video singolo
                file_path = await self.download_with_ytdlp(clean_url, attempt, info=info)
                if not file_path or not os.path.exists(file_path):
                    logger.warning('No downloaded file: platform=%s id=%s format=%s size=%s',
                                   platform, info.get('id'), info.get('format_id'),
                                   info.get('filesize') or info.get('filesize_approx'))
                    if attempt < self.max_retries - 1:
                        delay = self.retry_delay * (2 ** attempt)
                        await asyncio.sleep(delay)
                        continue
                    break # Vai ai fallback

                return {
                    'success': True,
                    'type': 'video',
                    'file_path': file_path,
                    'title': title,
                    'uploader': uploader,
                    'duration': info.get('duration', 0),
                    'view_count': info.get('view_count'),
                    'like_count': info.get('like_count'),
                    'channel': info.get('channel'),
                    'platform': platform,
                    'url': clean_url
                }

            except YouTubeResourceError as e:
                logger.warning('YouTube resource guard: %s', e)
                return {'success': False, 'error': str(e)}
            except Exception as e:
                err = str(e).lower()
                logger.error(f"Tentativo {attempt + 1} fallito: {str(e)[:200]}")

                # Errori specifici
                # Video genuinamente non disponibile (privato/rimosso/riservato): inutile
                # insistere o passare a Cobalt -> messaggio chiaro all'utente.
                if ('video unavailable' in err or 'private video' in err
                        or 'video has been removed' in err or 'who has blocked it' in err
                        or 'this video is not available' in err
                        or ('not available' in err and 'country' in err)
                        or 'age' in err and 'confirm' in err):
                    return {'success': False, 'error': (
                        '🔒 Questo video non è disponibile: potrebbe essere privato, rimosso, '
                        'o riservato (età/area geografica). YouTube non lo concede.'
                    )}
                if platform == 'youtube' and attempt < self.max_retries - 1:
                    await asyncio.sleep(self.retry_delay)
                    continue
                if 'sign in' in err or 'bot' in err:
                    logger.warning("Bot detection! Breaking to shortcuts.")
                    break # break to safe fallbacks
                if 'cannot parse' in err or 'parse' in err:
                    break
                if 'does not pass match_filter' in err:
                    return {'success': False, 'error': '⚠️ Questo video è troppo lungo. Scarico solo YouTube Shorts (max 120s).'}
                if 'no video formats found' in err or 'unsupported url' in err:
                    break

                if attempt < self.max_retries - 1:
                    delay = self.retry_delay * (2 ** attempt)
                    await asyncio.sleep(delay)

        # --- PHASE 2: EMERGENCY FALLBACKS ---
        logger.info("Entering Emergency Fallback Phase...")

        # I fallback non forniscono una durata affidabile. Per YouTube non devono
        # mai aggirare il limite dei tre minuti.
        if platform == 'youtube':
            logger.info("YouTube senza durata verificata: fallback bloccati.")
            return {'success': False, 'error': 'YouTube non ha completato il download. Riprova tra poco.'}

        if platform == 'facebook' and is_facebook_video_url(clean_url):
            logger.warning('Facebook reel unavailable after extraction; no photo fallback: %s', clean_url)
            return {'success': False, 'error': 'Facebook reel video could not be downloaded'}

        # 1. COBALT API (The Magic Bullet for No-Cookie environments)
        # Proviamo Cobalt per tutto (YouTube, Instagram, TikTok, Twitter, Facebook)
        # Se yt-dlp ha fallito, Cobalt spesso riesce perché usa i propri IP puliti.
        cobalt_result = await self.download_with_cobalt(clean_url)
        if cobalt_result:
            return cobalt_result

        # 2. Platform-Specific Scrapers (Last Resort)

        if 'facebook' in platform:
             try:
                 fb_files = await self._facebook_fallback(clean_url)
                 if fb_files:
                     fb_title = getattr(self, 'last_fallback_title', None) or title
                     return self._pack_media_result(fb_files, fb_title, uploader, platform, clean_url)
             except Exception as e:
                 logger.warning(f"Facebook fallback failed: {e}")

        # Dopo i tentativi normali, prova fallback specifici per piattaforme
        if platform == 'tiktok':
            try:
                # Explicit unpacking check
                fallback_resp = await self._tiktok_photo_fallback(clean_url)
                if isinstance(fallback_resp, tuple) and len(fallback_resp) == 2:
                    res_files, res_title = fallback_resp
                else:
                    res_files = fallback_resp
                    res_title = ""
                
                if res_files:
                    tk_title = getattr(self, 'last_fallback_title', None) or (res_title or title)
                    return self._pack_media_result(res_files, tk_title, uploader, platform, clean_url)
            except Exception as e:
                logger.warning(f"TikTok fallback failed: {e}")

        if platform == 'instagram':
            try:
                # 1) Prova fallback API interna (più affidabile per caroselli)
                api_files = await self._instagram_api_fallback(clean_url)
                if api_files:
                    title_to_use = getattr(self, 'last_fallback_title', None) or title
                    return self._pack_media_result(api_files, title_to_use, uploader, platform, clean_url)
                
                # 2) Se API fallisce, prova scraping HTML (vecchio metodo)
                fallback_resp = await self._instagram_photo_fallback(clean_url)
                # Explicit unpacking
                if isinstance(fallback_resp, tuple) and len(fallback_resp) == 2:
                    res_files, res_desc = fallback_resp
                else:
                   res_files = fallback_resp
                   res_desc = ""
                
                # Double check filtering if fallback result contained static assets again
                # (Ideally _instagram_photo_fallback should have done it, but double safety)
                if res_files:
                    safe_files = []
                    for f in res_files:
                        if 'static.cdninstagram' not in f and 'rsrc.php' not in f:
                            safe_files.append(f)
                    
                    if safe_files:
                        ig_title = getattr(self, 'last_fallback_title', None) or (res_desc or title)
                        return self._pack_media_result(safe_files, ig_title, uploader, platform, clean_url)
            except Exception as e:
                logger.warning(f"Instagram fallback failed: {e}")


        return {'success': False, 'error': 'Download fallito dopo multiple tentativi. Riprova più tardi.'}

    async def download_audio(self, url: str) -> Dict:
        if os.getenv('DOWNLOADER_URL'):
            from remote_downloader import remote_download
            return await remote_download(url, kind='audio')
        """Estrae l'audio (MP3) dal contenuto. Usato dal bottone 'Audio'."""
        clean_url = self.clean_url(url)
        if self.detect_platform(clean_url) == 'youtube':
            duration = await asyncio.to_thread(youtube_duration, clean_url,
                                              getattr(self, 'youtube_cookies', None),
                                              getattr(self, 'proxy_dict', None))
            if duration is None or duration > self.youtube_max_duration:
                return {'success': False, 'error': 'Audio disponibile solo per video YouTube di massimo 3 minuti.'}
            try:
                info = await self.extract_info(clean_url)
            except Exception:
                info = None
            duration = self._youtube_duration_seconds(info or {})
            if duration is None or duration > self.youtube_max_duration:
                return {'success': False, 'error': 'Audio disponibile solo per video YouTube di massimo 3 minuti.'}

        opts = self.get_ydl_opts(clean_url, 0)
        opts['format'] = 'bestaudio/best'
        opts['outtmpl'] = os.path.join(self.temp_dir, 'audio_%(id)s.%(ext)s')
        opts['postprocessors'] = [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }]
        opts.pop('merge_output_format', None)
        opts.pop('max_filesize', None)

        loop = asyncio.get_event_loop()

        @limited_media
        def _dl():
            if self.detect_platform(clean_url) == 'youtube':
                result = run_youtube_job(opts, clean_url, download=True)
                data = result['info']
                return (os.path.splitext(result['filename'])[0] + '.mp3',
                        data.get('title') or 'audio', data.get('uploader') or data.get('channel'))
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(clean_url, download=True)
                base = ydl.prepare_filename(info)
                mp3 = os.path.splitext(base)[0] + '.mp3'
                return mp3, (info.get('title') or 'audio'), (info.get('uploader') or info.get('channel'))

        try:
            mp3, title, uploader = await loop.run_in_executor(None, _dl)
            if mp3 and os.path.exists(mp3) and os.path.getsize(mp3) > 0:
                return {'success': True, 'file_path': mp3, 'title': title, 'uploader': uploader}
        except Exception as e:
            logger.warning(f"Audio download fallito per {url}: {str(e)[:160]}")
        return {'success': False, 'error': 'Estrazione audio fallita.'}
