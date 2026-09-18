"""Preserve TikTok ASR language metadata discarded by the generic extractor."""
from yt_dlp.extractor.tiktok import TikTokIE
from subtitles import tiktok_source_language


class TikTokCaptionsIE(TikTokIE):
    @classmethod
    def ie_key(cls):
        return 'TikTok'

    def _parse_aweme_video_web(self, aweme_detail, webpage_url, video_id, extract_flat=False):
        result = super()._parse_aweme_video_web(aweme_detail, webpage_url, video_id, extract_flat)
        source = tiktok_source_language(aweme_detail)
        if source:
            result['language'] = source
        return result
