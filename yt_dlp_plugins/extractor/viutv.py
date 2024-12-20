import json

from yt_dlp.extractor.common import InfoExtractor
from yt_dlp.utils import (
    ExtractorError,
    traverse_obj,
)


class ViuTVIE(InfoExtractor):
    _VALID_URL = r'https://viu\.tv/encore/(?P<id>[a-z0-9\-]+)(?:/(?P<episode>[a-z0-9\-]+))?'

    def _real_extract(self, url):
        programme_slug, video_slug = self._match_valid_url(url).group('id', 'episode')
        programme_data = self._download_json(
            f'https://api.viu.tv/production/programmes/{programme_slug}', programme_slug)['programme']

        if video_slug:
            for vtype in ('episodes', 'clips'):
                if episode := next((ep for ep in programme_data[vtype] if ep['slug'] == video_slug), None):
                    return self._get_episode(episode)

            raise ExtractorError('Content not found')

        def get_entries():
            for episode in programme_data['episodes']:
                yield self._get_episode(episode)

        return {
            '_type': 'playlist',
            'id': programme_slug,
            **traverse_obj(programme_data, {
                'title': 'title',
                'description': 'synopsis',
                'cast': ('programmeMeta', 'actors', ..., 'name'),
                'genres': ('genres', ..., 'name'),
                'thumbnail': 'avatar',
            }),
            'entries': get_entries(),
        }

    def _get_formats(self, product_id):
        vod = self._download_json(
            'https://api.viu.now.com/p8/3/getVodURL', product_id,
            data=json.dumps({
                'contentId': product_id,
                'contentType': 'Vod',
                'deviceType': 'ANDROID_WEB',
            }).encode(),
        )

        if vod['responseCode'] == 'GEO_CHECK_FAIL':
            self.raise_geo_restricted()

        if '.m3u8' in vod['asset'][0]:
            return self._extract_m3u8_formats_and_subtitles(vod['asset'][0], product_id)

        return self._extract_mpd_formats_and_subtitles(vod['asset'][0], product_id)

    def _get_episode(self, episode):
        formats, subtitles = self._get_formats(episode['productId'])

        return {
            **traverse_obj(episode, {
                'id': 'productId',
                'title': 'episodeNameU3',
                'thumbnail': 'avatar',
                'description': 'program_synopsis',
                'cast': ('videoMeta', 'actors', ..., 'name'),
                'genres': ('programmeMeta', 'genre', ..., 'name'),
                'duration': 'totalDurationSec',
                'series': 'program_title',
                'episode': 'episodeNameU3',
                'episode_number': 'episodeNum',
            }),
            'formats': formats,
            'subtitles': subtitles,
            '_cenc_key': '91ba752a446148c68400d78374b178b4:a01d7dc4edf582496b7e73d67e9e6899',
        }
