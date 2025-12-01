import base64
import json
import os
import random
import re
import string
import time
import urllib.parse
import uuid

from yt_dlp.aes import aes_cbc_decrypt_bytes
from yt_dlp.extractor.common import InfoExtractor
from yt_dlp.extractor.sonyliv import SonyLIVIE as _SonyLIVIE
from yt_dlp.extractor.stv import STVPlayerIE as _STVPlayerIE
from yt_dlp.extractor.tvp import TVPVODVideoIE as _TVPVODVideoIE
from yt_dlp.networking import HEADRequest
from yt_dlp.utils import (
    NO_DEFAULT,
    ExtractorError,
    InAdvancePagedList,
    float_or_none,
    int_or_none,
    jwt_decode_hs256,
    parse_duration,
    parse_iso8601,
    require,
    traverse_obj,
    update_url_query,
    urlencode_postdata,
    variadic,
)


class Channel4IE(InfoExtractor):
    _VALID_URL = r'https://www\.channel4\.com/programmes/(?P<programme>[a-z0-9\-]+)(?:/on-demand/(?P<id>[a-z0-9\-]+))?'
    _GEO_COUNTRIES = ['GB']
    _NETRC_MACHINE = 'channel4'
    _API_BASE = 'https://api.channel4.com/online'
    _USERTOKEN = None

    def _real_extract(self, url):
        programme_id, video_id = self._match_valid_url(url).group('programme', 'id')
        headers = self._get_auth_headers()

        if not video_id:
            json_data = self._download_json(
                f'{self._API_BASE}/v1/views/content-hubs/{programme_id}.json?client=amazonfire-dash',
                programme_id, headers=headers)

            return {
                '_type': 'playlist',
                'id': programme_id,
                **traverse_obj(json_data, ('brand', {
                    'title': 'title',
                    'description': 'summary',
                    'thumbnail': ('image', 'href', {lambda href: href.replace('{&resize}', '')}),
                    'categories': 'categories',
                    'entries': (
                        'episodes', lambda _, ep: 'assetInfo' in ep, 'programmeId',
                        {lambda ep: self.url_result(
                            f'https://www.channel4.com/programmes/{programme_id}/on-demand/{ep}', ie=self)}),
                })),
            }

        ep_info = self._download_json(
            f'{self._API_BASE}/v1/programmes/episode/{video_id}.json?client=amazonfire-dash',
            video_id, headers=headers)
        content = self._download_json(
            f'{self._API_BASE}/v1/vod/stream/{video_id}?client=amazonfire-dash',
            video_id, headers=headers)

        dashwv_stream = next(
            stream for profile in content['videoProfiles']
            for stream in profile['streams'] if 'dashwv' in profile['name'])

        def license_callback(challenge):
            license_url, token = aes_cbc_decrypt_bytes(
                base64.b64decode(dashwv_stream['token']),
                b'\x4B\x32\x43\x38\x51\x30\x39\x44\x37\x48\x4A\x33\x38\x35\x41\x42',
                b'\x42\x33\x4C\x4B\x56\x55\x30\x35\x46\x33\x49\x44\x4C\x56\x4D\x45',
            ).decode('ascii').split('|')

            resp = self._download_json(
                license_url, video_id,
                data=json.dumps({
                    'token': token,
                    'video': {'type': 'ondemand', 'url': dashwv_stream['uri']},
                    'message': base64.b64encode(challenge).decode(),
                }).encode(),
                headers={'Content-Type': 'application/json'})

            return base64.b64decode(resp['license'])

        return {
            'id': video_id,
            **traverse_obj(ep_info, ('episode', {
                'title': (('title', 'originalTitle'), filter, any),
                'description': 'summary',
                'thumbnail': ('image', 'href', {lambda href: href.replace('{&resize}', '')}),
                'season_number': ('seriesNumber', {int_or_none}),
                'episode_number': ('episodeNumber', {int_or_none}),
                'timestamp': ('firstTXDate', {parse_iso8601}),
                'categories': ('brand', 'categories'),
            })),
            'formats': self._extract_mpd_formats(dashwv_stream['uri'], video_id),
            'chapters': self._get_chapters(content),
            **traverse_obj(content, {
                'duration': 'duration',
                'age_limit': 'rating',
                'series': 'brandTitle',
                'subtitles': {'eng': ('subtitlesAssets', ..., {'url': 'url'})},
            }),
            '_license_callback': license_callback,
        }

    def _perform_login(self, username, password):
        token = self.cache.load(self._NETRC_MACHINE, username) or {}

        if not self._is_token_expired(token):
            self.write_debug('Skipping logging in')
            return

        self._USERTOKEN = self._get_token(
            {'grant_type': 'refresh_token', 'refresh_token': token.get('refreshToken')},
            'Refreshing tokens',
        ) if not self._is_token_expired(token, 'refreshTokenExpiresAt') else self._get_token(
            {'grant_type': 'password', 'username': username, 'password': password},
            'Logging in', errnote='Unable to log in',
        )

        self.cache.store(self._NETRC_MACHINE, username, self._USERTOKEN)

    def _get_auth_headers(self):
        username, _ = self._get_login_info()

        if username:
            token = self._USERTOKEN or self.cache.load(self._NETRC_MACHINE, username)
        elif (token := self.cache.load(self.IE_NAME, 'token')) and not self._is_token_expired(token):
            pass
        else:
            token = self._get_token({'grant_type': 'client_credentials'}, 'Downloading access token')
            self.cache.store(self.IE_NAME, 'token', token)

        return {'authorization': 'Bearer ' + token.get('accessToken')}

    def _get_token(self, data, note, **kwargs):
        token, now = self._download_json(
            f'{self._API_BASE}/v2/auth/token?client=amazonfire-dash', None, note,
            data=urlencode_postdata(data),
            headers={
                'content-type': 'application/x-www-form-urlencoded',
                'authorization': 'Basic eUExTHB6dGtHZUhaRDZuU2E3QzFBQUY2dkhwelZOblU6UXFFbUVnVVVVT1hUa3piNg==',
            },
            **kwargs,
        ), time.time()

        return traverse_obj(token, {
            'accessToken': 'accessToken',
            'refreshToken': 'refreshToken',
            'expiresAt': ('expiresIn', {int_or_none}, {lambda t: now + t}),
            'refreshTokenExpiresAt': ('refreshTokenExpiresIn', {int_or_none}, {lambda t: now + t}),
        })

    def _is_token_expired(self, token, key='expiresAt'):
        return int_or_none(token.get(key), default=0) < time.time() + 300

    def _get_chapters(self, content):
        chapters = []

        if traverse_obj(content, ('skipIntro', 'skip')):
            intro = traverse_obj(content, ('skipIntro', {
                'start_time': 'skipStart', 'end_time': 'skipEnd'}))
            chapters.append({**intro, 'title': 'Intro'})
            chapters.append({'start_time': intro['end_time']})

        if traverse_obj(content, ('endCredits', 'squeeze')):
            chapters.append({
                'start_time': content['endCredits']['squeezeIn'],
                'title': 'End Credits',
            })

        for start_time in traverse_obj(content, ('adverts', 'breaks', ..., 'breakOffset')):
            if start_time not in traverse_obj(chapters, (..., 'start_time')):
                chapters.append({'start_time': start_time})

        chapters.sort(key=lambda x: x['start_time'])

        return traverse_obj(chapters, (..., {
            'start_time': ('start_time', {float_or_none(scale=1000)}),
            'end_time': ('end_time', {float_or_none(scale=1000)}),
            'title': 'title',
        }))


class Channel5IE(InfoExtractor):
    _VALID_URL = r'https://www\.channel5\.com/(?:show/)?(?P<show>[a-z0-9\-]+)(?:/(?P<season>[a-z0-9\-]+)(?:/(?P<id>[a-z0-9\-]+))?)?'
    _GEO_COUNTRIES = ['GB']
    _API_BASE = 'https://cassie-auth.channel5.com/api/v2/media'
    _GUIDANCE = {
        'Guidance': 16,
        'GuidancePlus': 18,
    }

    def set_downloader(self, downloader):
        super().set_downloader(downloader)

        if downloader:
            self._add_handler(downloader._request_director)

    def _real_extract(self, url):
        show, season, episode = self._match_valid_url(url).group('show', 'season', 'id')

        if not season:
            data_url_base = f'https://corona.channel5.com/shows/{show}'
            show_data = self._download_json(f'{data_url_base}.json?platform=my5desktop', show)

            if show_data.get('standalone'):
                return self._get_episode(self._download_json(
                    f'{data_url_base}/episodes/next.json?platform=my5desktop', show))

            seasons_data = self._download_json(
                f'https://corona.channel5.com/shows/{show}/seasons.json?platform=my5desktop&friendly=1', show)

            return {
                '_type': 'playlist',
                **traverse_obj(show_data, {
                    'id': 'id',
                    'title': 'title',
                    'description': 'm_desc',
                    'genres': (('genre',),),
                }),
                'entries': traverse_obj(seasons_data, (
                    'seasons', ..., 'sea_f_name',
                    {lambda season: self.url_result(
                        f'https://www.channel5.com/show/{show}/{season}', ie=self)},
                )),
            }

        data_url_base = f'https://corona.channel5.com/shows/{show}/seasons/{season}'

        if not episode:
            season_data = self._download_json(
                f'{data_url_base}/episodes.json?platform=my5desktop', season)

            return {
                '_type': 'playlist',
                **traverse_obj(season_data, ('episodes', 0, {
                    'id': 'sh_id',
                    'title': 'sh_title',
                })),
                'entries': InAdvancePagedList(
                    lambda idx: (yield self._get_episode(season_data['episodes'][idx])),
                    len(season_data['episodes']), 1),
            }

        return self._get_episode(self._download_json(
            f'{data_url_base}/episodes/{episode}.json?platform=my5desktop', episode))

    def _get_episode(self, data):
        info_dict = traverse_obj(data, {
            'id': 'id',
            'title': 'title',
            'description': 'm_desc',
            'series': 'sh_title',
            'season_number': ('sea_num', {int_or_none}),
            'episode_number': ('ep_num', {int_or_none}),
            'genres': (('genre',),),
            'timestamp': 'vod_s',
            'age_limit': ('rat', {self._GUIDANCE.get}),
        })

        formats, subtitles, license_urls = [], {}, {}
        video_id = data['id']

        for platform in ('my5firetv', 'my5firetvhydradash'):
            media = self._download_json(
                f'{self._API_BASE}/{platform}/{video_id}.json', video_id)

            if asset := traverse_obj(media, ('assets', 0)):
                for rendition in asset.get('renditions', []):
                    mpd_url = rendition['url'].replace('_SD-tt', '-tt')
                    fmts, subs = self._extract_mpd_formats_and_subtitles(mpd_url, video_id)
                    formats.extend(fmts)
                    self._merge_subtitles(subs, target=subtitles)

                if sub_url := asset.get('subtitleurl'):
                    self._merge_subtitles({'eng': [{'url': sub_url}]}, target=subtitles)

                info_dict['duration'] = asset['duration']
                license_urls[mpd_url] = asset['keyserver']

        return {
            **info_dict,
            'formats': formats,
            'subtitles': subtitles,
            '_license_url': license_urls,
        }

    def _add_handler(self, director):
        req = self._create_request(self._API_BASE)
        default_handler = director._get_handlers(req)[0]

        class Channel5RH(type(default_handler)):
            def _make_sslcontext(self, *args, **kwargs):
                context = super()._make_sslcontext(*args, **kwargs)
                context.set_ciphers('ALL:@SECLEVEL=0')
                context.load_cert_chain(certfile=os.path.join(os.path.dirname(__file__), 'c5.pem'))

                return context

        handler = Channel5RH(ie=self, logger=None)
        director.add_handler(handler)
        director.preferences.add(
            lambda rh, req:
            500 if rh == handler and req.url.startswith(self._API_BASE) else 0)


class DAZNIE(InfoExtractor):
    _VALID_URL = r'https://www\.dazn\.com/(?P<lang>[a-z]{2})-(?P<country>[A-Z]{2})/(?:[^/]+/)+(?P<id>[0-9a-z]{20,})'
    _NETRC_MACHINE = 'dazn'
    _USERTOKEN = None

    def _real_extract(self, url):
        lang, country, content_id = self._match_valid_url(url).group('lang', 'country', 'id')
        user_agent, urlh = self._download_webpage_handle(
            'https://ifconfig.me/ua', None, 'Checking user agent', impersonate=True)
        target = urlh.extensions.get('impersonate', True)
        auth = 'Bearer ' + self._get_token(country)

        data = self._download_json(
            'https://api.playback.indazn.com/v5/Playback', content_id,
            query={
                'AppVersion': '0.79.0', 'DrmType': 'WIDEVINE', 'Format': 'MPEG-DASH',
                'PlayerId': '@dazn/peng-html5-core/web/web', 'Platform': 'web',
                'Model': 'unknown', 'Secure': 'true', 'Manufacturer': 'microsoft',
                'AssetId': content_id, 'LanguageCode': lang,
            },
            headers={'accept': '*/*', 'authorization': auth, 'x-dazn-device': uuid.uuid4()},
            impersonate=target,
        )

        formats, license_urls = [], {}

        for source in data['PlaybackDetails']:
            cdn_token = {source['CdnToken']['Name']: source['CdnToken']['Value']} if 'CdnToken' in source else {}
            mpd_url = update_url_query(source['ManifestUrl'], cdn_token)
            fmts = self._extract_mpd_formats(
                mpd_url, content_id, headers={'user-agent': user_agent}, fatal=False)

            for fmt in fmts:
                fmt.update({
                    'extra_param_to_segment_url': urllib.parse.urlencode(cdn_token),
                    'http_headers': {'user-agent': user_agent},
                })

            formats.extend(fmts)
            license_urls[mpd_url] = source['LaUrl']

        def license_callback(challenge, license_url):
            return self._request_webpage(
                license_url, content_id, note='Fetching keys', data=challenge,
                headers={'authorization': auth, 'content-type': 'application/octet-stream'},
                impersonate=target,
            ).read()

        return {
            **traverse_obj(data, {
                'id': ('Asset', 'Id'),
                'title': ('Asset', 'Title'),
                'series': ('Asset', 'Competition', 'Title'),
            }),
            'formats': formats,
            '_license_url': license_urls,
            '_license_callback': license_callback,
        }

    def _perform_login(self, username, password):
        if self.cache.load(self._NETRC_MACHINE, username):
            self.write_debug('Skipping logging in')
            return

        device_id = f'{random.randint(0, 0x7fffffff):x}'.zfill(10)
        session_id = ''.join(random.choices(string.ascii_lowercase + string.digits, k=24))

        login_response = self._download_json(
            'https://authentication-prod.ar.indazn.com/v5/SignIn', None, 'Logging in',
            data=json.dumps({
                'Email': username, 'Password': password,
                'Platform': 'web',
                'DeviceId': device_id, 'ProfilingSessionId': session_id,
            }).encode(),
            headers={'accept': '*/*', 'content-type': 'application/json'})

        self._USERTOKEN = login_response['AuthToken']['Token']
        self.cache.store(self._NETRC_MACHINE, username, {
            'device_id': device_id,
            'response': login_response,
        })

    def _get_token(self, country):
        if not self._USERTOKEN:
            username, _ = self._get_login_info()

            if not username:
                self._USERTOKEN = self._get_anonymous_token()
            elif user_data := self.cache.load(self._NETRC_MACHINE, username):
                token = user_data['response']['AuthToken']['Token']
                token_country = jwt_decode_hs256(token)['country']

                if country.lower() != token_country or parse_iso8601(
                        user_data['response']['AuthToken']['Expires']) < time.time() + 5:
                    self.cache.store(self._NETRC_MACHINE, username, None)
                    refresh_response = self._download_json(
                        'https://ott-authz-bff-prod.ar.indazn.com/v5/RefreshAccessToken', None,
                        'Refreshing access token',
                        data=json.dumps({'DeviceId': user_data['device_id']}).encode(),
                        headers={
                            'accept': 'application/json',
                            'content-type': 'application/json',
                            'authorization': 'Bearer ' + token,
                        })

                    user_data['response'] = refresh_response
                    self.cache.store(self._NETRC_MACHINE, username, user_data)

                self._USERTOKEN = user_data['response']['AuthToken']['Token']

        return self._USERTOKEN

    def _get_anonymous_token(self):
        device_id = f'{random.randint(0, 0x7fffffff):x}'.zfill(10)
        return self._download_json(
            'https://authentication-prod.ar.indazn.com/v1/anonymous-user', None,
            'Fetching anonymous token',
            headers={'accept': '*/*', 'content-type': 'application/json'},
            data=json.dumps({'deviceId': device_id}).encode(),
        )['token']


class ITVXIE(InfoExtractor):
    _VALID_URL = r'https://www\.itv\.com/watch/(?P<slug>[0-9a-z-]+)/(?P<brand>[0-9a]+B?)(?:/(?P<id>[0-9a]+))?'

    def _real_extract(self, url):
        slug, brand_id, video_id = self._match_valid_url(url).group('slug', 'brand', 'id')

        if not video_id and not brand_id.endswith('B'):
            if info_dict := self._get_brand(brand_id, slug):
                return info_dict

        query = '''
            query GetProgramme(
                $brandId: BrandLegacyId!
                $id: TitleLegacyId
            ) {
                titles(filter: { brandLegacyId: $brandId, legacyId: $id }) {
                    titleType
                    title
                    broadcastDateTime
                    imageUrl
                    brand {
                        title
                        genres {
                            name
                        }
                    }
                    synopses {
                        epg
                    }
                    latestAvailableVersion {
                        duration
                        playlistUrl
                        visuallySigned
                        tier
                        audioDescribed
                        bsl {
                            playlistUrl
                        }
                    }
                    ... on Episode {
                        episodeNumber
                        seriesNumber
                    }
                    ... on Special {
                        episodeNumber
                        productionYear
                    }
                    ... on Film {
                        productionYear
                    }
                }
            }
        '''

        title_id, video_id = (video_id.replace('a', '/'), video_id) if video_id else (None, brand_id)
        titles = self._download_json(
            'https://content-inventory.prd.oasvc.itv.com/discovery', video_id,
            note='Downloading title metadata',
            query={
                'query': re.sub(r'\n\s+', ' ', query.strip()),
                'variables': json.dumps({
                    'brandId': brand_id.replace('a', '/'),
                    'id': title_id,
                }, separators=(',', ':')),
            })['data']['titles']

        if not traverse_obj(titles, (0, 'latestAvailableVersion')):
            raise ExtractorError('Episode not found', video_id=video_id, expected=True)

        return {
            'id': video_id,
            'title': traverse_obj(titles[0], ((
                ('seriesNumber', {lambda n: n and f'Series {n}'}),
                ('episodeNumber', {lambda n: n and f'Episode {n}'}),
            ), all, {', '.join})),
            **traverse_obj(titles[0], {
                'title': 'title',
                'description': ('synopses', 'epg'),
                'release_year': 'productionYear',
                'season_number': ('seriesNumber', {int_or_none}),
                'episode_number': ('episodeNumber', {int_or_none}),
                'genres': ('brand', 'genres', ..., 'name'),
                'thumbnail': ('imageUrl', {lambda i: i.format(
                    width=1920, height=1080, quality=100, blur=0, bg='false', image_format='jpg')}),
                'timestamp': ('broadcastDateTime', {parse_iso8601}),
                'duration': ('latestAvailableVersion', 'duration', {parse_duration}),
            }),
            **self._get_episode(titles[0]['latestAvailableVersion'], video_id),
        }

    def _get_brand(self, brand_id, slug):
        query = '''
            query GetProgramme(
                $id: BrandLegacyId!
            ) {
                brands(filter: { legacyId: $id }) {
                    title
                    imageUrl(imageType: ITVX)
                    synopses {
                        epg
                    }
                    genres {
                        name
                    }
                    titles(sortBy: SEQUENCE_DESC) {
                        legacyId
                        title
                        latestAvailableVersion {
                            duration
                        }
                    }
                }
            }
        '''

        brands = self._download_json(
            'https://content-inventory.prd.oasvc.itv.com/discovery', brand_id,
            note='Downloading brand metadata',
            query={
                'query': re.sub(r'\n\s+', ' ', query.strip()),
                'variables': json.dumps({
                    'id': brand_id.replace('a', '/'),
                }, separators=(',', ':')),
            })['data']['brands']

        if not brands:
            return False

        return {
            '_type': 'playlist',
            'id': brand_id,
            **traverse_obj(brands[0], {
                'title': 'title',
                'description': ('synopses', 'epg'),
                'genres': ('brand', 'genres', ..., 'name'),
                'thumbnail': ('imageUrl', {lambda i: i.format(
                    width=1920, height=1080, quality=100, blur=0, bg='false', image_format='jpg')}),
                'entries': (
                    'titles', lambda _, t: t['latestAvailableVersion'],
                    {lambda title: self.url_result(
                        f'https://www.itv.com/watch/{slug}/{brand_id}/{title["legacyId"].replace("/", "a")}',
                        ie='ITVX', video_id=title['legacyId'].replace('/', 'a'),
                        video_title=title['title'], url_transparent=True,
                    )},
                ),
            }),
        }

    def _get_formats(self, episode, video_id, platform):
        featureset = ['mpeg-dash', 'widevine', 'outband-webvtt', 'hd', 'single-track']

        if episode.get('audioDescribed'):
            featureset.append('inband-audio-description')

        get_bsl = 'bsl' in episode and bool(self._configuration_arg('bsl'))

        data = self._download_json(
            episode['bsl']['playlistUrl'] if get_bsl else episode['playlistUrl'], video_id,
            data=json.dumps({
                'client': {
                    'version': '4.1',
                    'id': 'browser',
                    'supportsAdPods': False,
                },
                'device': {
                    'manufacturer': 'Chrome',
                    'model': '131.0.0.0',
                    'os': {
                        'name': 'Windows',
                        'version': '10',
                        'type': 'desktop',
                    },
                },
                **self._get_user(video_id),
                'variantAvailability': {
                    'featureset': featureset,
                    **platform,
                    'drm': {
                        'system': 'widevine',
                        'maxSupported': 'L3',
                    },
                },
            }).encode(),
            headers={
                'Accept': 'application/vnd.itv.vod.playlist.v4+json',
                'Content-Type': 'application/json',
            })

        files = traverse_obj(data, ('Playlist', 'Video', 'MediaFiles', ..., {
            'url': 'Href',
            'license_url': 'KeyServiceUrl',
            'resolution': ('Resolution', {int_or_none(default=0)}),
        }))

        return {
            **traverse_obj(data, ('Playlist', 'Video', {
                'duration': ('Duration', {parse_duration}),
                'subtitles': ('Subtitles', ..., {'url': 'Href'}),
            })),
            'files': {file['resolution']: file for file in files},
            'chapters': self._get_chapters(data),
        }

    def _get_episode(self, episode, video_id):
        if 'FREE' not in episode['tier'] and not self._get_user(video_id):
            self.raise_login_required('This video is only available for premium users')

        hd_data = self._get_formats(episode, video_id, {'platformTag': 'ctv'})
        sd_data = self._get_formats(episode, video_id, {'player': 'dash', 'platformTag': 'dotcom'})

        info_dict = {
            'formats': [],
            'subtitles': {},
            'chapters': hd_data['chapters'],
            'duration': hd_data['duration'],
            '_license_url': {},
        }

        if 720 in hd_data['files'] and 1080 in hd_data['files']:
            del hd_data['files'][720]
        if 0 in hd_data['files'] and 0 in sd_data['files']:
            del sd_data['files'][0]

        for data in (hd_data, sd_data):
            if 'subtitles' in data:
                self._merge_subtitles({'eng': data['subtitles']}, target=info_dict['subtitles'])

            for _, file in data['files'].items():
                if '.mp4' in file['url']:
                    info_dict['formats'].append({'url': file['url']})
                else:
                    info_dict['formats'].extend(self._extract_mpd_formats(file['url'], video_id))
                if 'license_url' in file:
                    info_dict['_license_url'][file['url']] = file['license_url']

        return info_dict

    def _get_chapters(self, data):
        chapters = traverse_obj(data, (
            'Playlist', 'Video', 'Timecodes',
            {'Opening Titles': 'OpeningTitles', 'End Credits': 'EndCredits', 'Recap': 'Recap'},
            {dict.items}, ...,
            {
                'start_time': (1, 'StartTime', {parse_duration}),
                'end_time': (1, 'EndTime', {parse_duration}),
                'title': 0,
            },
        ))

        for start_time in traverse_obj(data, ('Playlist', 'ContentBreaks', ..., 'TimeCode', {parse_duration})):
            if start_time not in traverse_obj(chapters, (..., 'start_time')):
                chapters.append({'start_time': start_time})

        chapters.sort(key=lambda x: x['start_time'])
        return chapters

    def _get_user(self, video_id):
        for cookie in self.cookiejar.get_cookies_for_url('https://www.itv.com'):
            if cookie.name == 'Itv.Session' and (session := self._parse_json(cookie.value, video_id)) and \
                    (token := traverse_obj(session, ('tokens', 'content', 'access_token'))):

                if jwt_decode_hs256(token)['exp'] < time.time() + 300:
                    response = self._download_json(
                        'https://auth.prd.user.itv.com/token', video_id,
                        note='Refreshing access token',
                        query={'refresh': traverse_obj(session, ('tokens', 'content', 'refresh_token'))},
                        headers={'accept': 'application/vnd.user.auth.v2+json'})

                    if (token := response.get('access_token')):
                        session['tokens']['content'] = response
                        cookie.value = json.dumps(session, separators=(',', ':'))

                if token:
                    return {'user': {'token': token}}

        return {}


class MytvSuperIE(InfoExtractor):
    _VALID_URL = r'https://www\.mytvsuper\.com/(?:(?P<lang>tc|en)/)?programme/[a-z0-9]+_(?P<pid>\d+)/([^/#]+)/(?:e/(?P<id>\d+)/)?'
    _ANON_TOKEN = 'eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.eyJib3NzX2lkIjoiMDAwMDAwMDAxIiwiZGV2aWNlX3Rva2VuIjoiQ3ZmTUNzVTh4UGlpYmtDUUVrSzM5NUpnIiwiZGV2aWNlX2lkIjoiMCIsImRldmljZV90eXBlIjoid2ViIiwiZGV2aWNlX29zIjoiYnJvd3NlciIsImRybV9pZCI6bnVsbCwiZXh0cmEiOnsicHJvZmlsZV9pZCI6MX0sImlhdCI6MTY0NjI5MzQxNCwiZXhwIjoxNjQ2Mjk3MDE0fQ.t5qYMiV4RJkAZ9FfmmJtigpzNca0P5ZnI4AEXU61HWVIJd5cIUQlNufOJbN4R3MPJxs7msOVBdosIMaIhF49so_ubufqSNDDK9s3qZRpAUaHvRtiXQWCuuL3Am07IwaR6vO-yNFpNtnhTWp7V-5KkmJjmjgwtbQlwK5FU424Ef9iFu64aeounen8o5cuBuql5nRl6mFOX7QMx3Cr0XmLyJBRsuuoXlivaGzNchqT4rkmck0SUqeeBSzcpoDdFry4SXZO9I_CIK75bOX4Icw5p8ZFwAzYvE5xhTpAEdRUKMPSDMRD9Vak-WKPWhQBeV8X5LJONhaofMaq0j0HC5sM6arPQR6x2r5y5IPZwVOcUaYqJVlgXOAP72iFwCkZBm30qJV9p5eLSNWizpVUbYIEiwjcqBQ9ZZR2jqszzSEZpsTO1kwQ3jIViewwFJjffBljrp5ZsRDj-vXrdZ-tXVY4ecsgrjUXJJEEMKMCBVFLzuu5is6Hgdr8BUdm8QAPQqvvkqu7W0Gt-2YAgcU4eEG2wzx1485wxNxLgXXG10SwzH12OHxqoMl3_KP22JN9JgP6uS1Br4yLFqo-v3Z-UOAo3x_yfivgcW34uI4VHSF1JiQfJinsSWeHOGPJrDSDvrCNLZbFonX2xaWVOQ3Uf8hXum55xNufLM8Trt4Ga8CBZMY'
    _USERTOKEN = None

    def _real_extract(self, url):
        lang, programme_id, episode_id = self._match_valid_url(url).group('lang', 'pid', 'id')

        if not episode_id:
            return self._get_playlist(programme_id, lang or 'tc')

        episode = self._download_json(
            'https://content-api.mytvsuper.com/v2/episode/id', episode_id,
            query={'episode_id': episode_id})
        programme = self._get_programme(episode['programme_id'])

        return self._get_episode(programme, episode['currEpisode'], lang or 'tc')

    def _get_playlist(self, programme_id, lang):
        programme = self._get_programme(programme_id)
        episodes = self._download_json(
            'https://content-api.mytvsuper.com/v1/episode/list', programme_id,
            query={
                'programme_id': programme_id,
                'start_episode_no': 1,
                'end_episode_no': programme['latest_episode_no'],
                'sort_desc': 'true',
            })

        return {
            '_type': 'playlist',
            'id': programme_id,
            'title': programme['name_' + lang],
            'description': programme['long_desc_' + lang],
            'thumbnails': [{'id': size, 'url': programme['image'][size]} for size in programme['image']],
            **self._get_programme_info(programme, lang),
            'entries': InAdvancePagedList(
                lambda idx: (yield {
                    **self._get_episode(programme, episodes['items'][idx], lang),
                    'ie_key': 'MytvSuper',
                }),
                len(episodes['items']), 1),
        }

    def _get_token(self):
        if self._USERTOKEN:
            return self._USERTOKEN

        session = self._download_json(
            'https://www.mytvsuper.com/api/auth/getSession/self/', None,
            note='Downloading session')

        if not session.get('supported_country', True):
            self._initialize_geo_bypass({'countries': ['MO']})

        if self._cookies_passed:
            if not session.get('error') and (token := traverse_obj(session, ('user', 'token'))):
                self.cache.store('mytvsuper', 'token', token)
                self._USERTOKEN = token
                return token

        self._USERTOKEN = self.cache.load('mytvsuper', 'token') or self._ANON_TOKEN
        return self._USERTOKEN

    def _get_episode(self, programme, episode, lang):
        episode_name = self._get_mytv_episode_name(episode, lang)
        episode_id = episode['episode_id']

        data = self._download_json(
            'https://user-api.mytvsuper.com/v1/video/checkout', episode_id,
            query={'platform': 'android_tv', 'video_id': episode['video_id']},
            headers={'Authorization': 'Bearer ' + self._get_token()})

        formats = []
        profiles = {profile['quality']: profile['streaming_path'] for profile in data['profiles']}
        profiles = traverse_obj(profiles, (('auto', 'high'),)) or profiles.values()

        for profile in profiles:
            formats.extend(self._extract_mpd_formats(profile.replace('https://', 'http://'), episode_id))

        def license_callback(challenge):
            return self._request_webpage(
                'https://wv.drm.tvb.com/wvproxy/mlicense', episode_id, 'Fetching keys',
                query={'contentid': data['content_id']},
                data=challenge,
                headers={
                    'Content-Type': 'application/octet-stream',
                    'x-user-token': self._get_token(),
                }).read()

        return {
            'id': str(episode_id),
            'title': '%s %s' % (programme['name_' + lang], episode_name),
            'formats': formats,
            'description': episode['desc_' + lang],
            'subtitles': {sub['language']: [{'url': sub['path']}] for sub in data['subtitles']},
            'thumbnails': [{'id': size, 'url': episode['image'][size]} for size in episode['image']],
            'duration': episode['duration'],
            **self._get_programme_info(programme, lang),
            'series': programme['name_' + lang],
            'episode': episode_name,
            'episode_number': episode['episode_no'],
            '_license_callback': license_callback,
        }

    def _get_mytv_episode_name(self, episode, lang='tc'):
        if episode['name_tc']:
            return episode['name_' + lang]
        if episode['episode_no'] < 1e7:
            return ('第%d集' if lang == 'tc' else 'Episode %d') % episode['episode_no']

        name = str(episode['episode_no'])
        return '%s/%s/%s' % (name[0:4], name[4:6], name[6:8])

    def _get_programme(self, programme_id):
        return self._download_json(
            'https://content-api.mytvsuper.com/v1/programme/details', programme_id,
            'Downloading programme data',
            query={'programme_id': programme_id, 'platform': 'web'})

    def _get_programme_info(self, programme, lang):
        def tag_filter(types):
            return lambda _, t: t['type'] in variadic(types)

        return traverse_obj(programme, {
            'release_year': ('tags', tag_filter('prod_year'), 'name_en', any, {int_or_none}),
            'location': ('tags', tag_filter('country_of_origin'), 'name_' + lang, any),
            'age_limit': ('parental_lock', {lambda x: 18 if x else None}),
            'categories': ('tags', tag_filter(('main_cat', 'category', 'sub_category')), 'name_' + lang),
            'cast': ('artists', ..., 'name_' + lang),
            'timestamp': ('start_time', {parse_iso8601}),
        })


class NHKOneIE(InfoExtractor):
    _VALID_URL = r'https://www\.web\.nhk/tv/(?:[^/]+/)*ep/(?P<id>[A-Z0-9]+)'

    def _real_extract(self, url):
        content_id = self._match_id(url)
        info = self._download_json(
            f'https://api.web.nhk/r8/l/bundle/te/{content_id}.json', content_id,
            headers={'cookie': 'z_at=' + self._get_user_token()})
        video_url = traverse_obj(info, (
            'tvepisode', 'result', 0,
            'video', 0, 'detailedVideoDescriptor', {require('VideoDescriptor')}))
        data = self._download_json(video_url, content_id, headers={'referer': 'https://www.web.nhk/'})

        for playlist in data['manifests']:
            if playlist['drm_type'] == 'cenc':
                fmts, subs = self._extract_m3u8_formats_and_subtitles(playlist['url'], content_id, 'mp4')
                self._remove_duplicate_formats(fmts)

                for fmt in fmts:
                    if fmt.get('vcodec') == 'none':
                        fmt.setdefault('abr', int_or_none(self._search_regex(
                            r'/a[sm](\d+)/\w+\.m3u8$', fmt['url'], 'abr', default=None)))

                        if fmt.get('source_preference') == -2:
                            fmt['preference'] = fmt['source_preference']

                def license_callback(challenge):
                    return self._request_webpage(
                        'https://licence.hsk.st.nhk/widevine/license', content_id,
                        note='Fetching keys', data=challenge,
                        headers={'authorization': 'Bearer ' + self._get_user_token()}).read()

                return {
                    **traverse_obj(info, ('tvepisode', 'result', 0, {
                        'id': 'id',
                        'title': 'name',
                        'description': 'description',
                        'series': ('partOfSeries', 'name'),
                        'genres': ('identifierGroup', 'formatGenreTag', ..., 'name'),
                        'thumbnail': ('eyecatch', 'main', 'url'),
                        'timestamp': ('releasedEvent', 'startDate', {parse_iso8601}),
                    })),
                    'formats': fmts,
                    'subtitles': subs,
                    '_license_callback': license_callback,
                }

    def _get_user_token(self):
        if (token := self.cache.load(self.IE_NAME, 'token')) and \
                jwt_decode_hs256(token)['exp'] >= time.time() + 300:
            return token

        self._download_webpage(
            'https://www.web.nhk/tix/build_authorize', None, 'Get user token',
            query={
                'idp': 'r-alaz',
                'profileType': 'anonymous',
                'redirect_uri': 'https://www.web.nhk/tv/g1',
                'ctu': 'in',
            },
            headers={
                'cookie': 'consentToUse={"status":"optedin","entity":"household"}',
            })

        token = self._get_cookies('https://www.web.nhk').get('z_at').value
        self.cache.store(self.IE_NAME, 'token', token)

        return token


class SonyLIVIE(_SonyLIVIE, plugin_name='yt-dlp-mp4decrypt'):
    _license_info = {}

    def _download_json(self, url, video_id, *args, **kwargs):
        is_vod = '/VOD/' in url

        if is_vod:
            url = url.replace('/AGL/1.5/', '/AGL/3.8/').replace('/IN/CONTENT/', '/IN/DL/CONTENT/')
            kwargs['headers']['content-type'] = 'application/json'
            kwargs['data'] = json.dumps({
                'deviceId': self._get_device_id(),
                'hasLAURLEnabled': True,
                'platform': 'web',
                'actionType': 'play',
                'browser': 'Chrome',
                'os': 'Windows',
            }).encode()

        response = super()._download_json(url, video_id, *args, **kwargs)

        if is_vod and response.get('resultObj', {}).get('isEncrypted'):
            self._license_info[video_id] = response['resultObj'].get('LA_Details')

        return response

    def _real_extract(self, url):
        info_dict = super()._real_extract(url)

        if details := self._license_info.get(info_dict['id']):
            info_dict['_license_url'] = details.get('laURL')

        return info_dict


class STVPlayerIE(_STVPlayerIE, plugin_name='yt-dlp-mp4decrypt'):
    def _real_extract(self, url):
        self.BRIGHTCOVE_URL_TEMPLATE = self.BRIGHTCOVE_URL_TEMPLATE.replace('6204867266001', '1486976045')
        return super()._real_extract(url)

    def report_drm(self, video_id, partial=NO_DEFAULT):
        self.BRIGHTCOVE_URL_TEMPLATE = self.BRIGHTCOVE_URL_TEMPLATE.replace('1486976045', '6204867266001')


class TVBNewsIE(InfoExtractor):
    _VALID_URL = r'https://news\.tvb\.com/(?:[^/]+/){2}(?P<id>[0-9a-f]+)'
    _GEO_COUNTRIES = ['HK']

    def _real_extract(self, url):
        video_id = self._match_id(url)
        webpage = self._download_webpage(url, video_id)
        nextjs = self._search_nextjs_data(webpage, video_id)
        videos = traverse_obj(nextjs, ('props', 'pageProps', 'newsItems', 'media', 'video', ..., ..., 'url'))
        license_url = traverse_obj(nextjs, ('runtimeConfig', 'playerConfig', 'wv'))

        self._x_forwarded_for_ip = None
        mpds = set()
        formats = []
        content_id = ''

        for video in videos:
            checkout = self._download_json(video + '?profile=chrome', video_id)
            mpds.update(traverse_obj(checkout, ('content', 'url', ...)))
            content_id = traverse_obj(checkout, ('content', 'content_id'))

        for mpd in mpds:
            formats.extend(self._extract_mpd_formats(mpd, video_id))

        return {
            **traverse_obj(nextjs, ('props', 'pageProps', 'newsItems', {
                'id': 'id',
                'title': 'title',
                'description': 'desc',
                'categories': 'tags',
                'timestamp': ('publish_datetime', {parse_iso8601}),
            })),
            'formats': formats,
            '_license_url': license_url + content_id,
        }


class TVNZIE(InfoExtractor):
    _VALID_URL = r'https://www\.tvnz\.co\.nz/(?P<id>[0-9a-z\-\/]+)'
    BRIGHTCOVE_URL_TEMPLATE = 'http://players.brightcove.net/%s/%s_default/index.html?videoId=%s'
    _API_BASE = 'https://apis-public-prod.tech.tvnz.co.nz'

    def _real_extract(self, url):
        video_id = self._match_id(url)
        data = self._download_json(self._API_BASE + '/api/v1/web/play/page/' + video_id, video_id)

        if seasons := traverse_obj(data, (
                'layout', 'defaultSectionLayout', 'slots', 'main', 'modules', ..., 'lists', ...)):
            return {
                '_type': 'playlist',
                **traverse_obj(data, ('_embedded', data['layout']['showHref'], {
                    'id': 'showId',
                    'title': 'title',
                    'description': 'synopsis',
                    'age_limit': ('rating', 'classification', {int_or_none}),
                    'categories': ('categories', ..., 'label'),
                    'thumbnails': (('coverImage', 'tileImage'), {'url': 'src', 'ext': 'extension'}),
                    'timestamp': ('lastPublishedEpisodeDate', {parse_iso8601}),
                })),
                'entries': InAdvancePagedList(
                    lambda idx: (yield self._get_season(seasons[idx], data, video_id)),
                    len(seasons), 1),
            }

        return self._get_video(data, data['layout']['video']['href'])

    def _get_season(self, season, data, video_id):
        if season['href'] in data['_embedded']:
            season_data = data['_embedded'][season['href']]
            season_data['_embedded'] = data['_embedded']
        else:
            season_data = self._download_json(self._API_BASE + season['href'], video_id)

        contents = traverse_obj(season_data, ('content', ..., 'href'))
        return {
            '_type': 'playlist',
            'title': season_data.get('label'),
            'entries': InAdvancePagedList(
                lambda idx: (yield self._get_video(season_data, contents[idx])),
                len(contents), 1),
            'extractor': self.IE_NAME,
            'extractor_key': self.ie_key(),
        }

    def _get_video(self, data, href):
        video = data['_embedded'][href]

        if video['type'] == 'showVideo':
            return {
                '_type': 'url_transparent',
                'id': video['publisherMetadata']['brightcoveVideoId'],
                'url': self.BRIGHTCOVE_URL_TEMPLATE % (
                    video['publisherMetadata']['brightcoveAccountId'],
                    video['publisherMetadata']['brightcovePlayerId'],
                    video['publisherMetadata']['brightcoveVideoId']),
                **traverse_obj(video, {
                    'title': 'title',
                    'thumbnail': ('image', 'src'),
                    'description': 'synopsis',
                    'series': 'title',
                    'season_number': ('seasonNumber', {int_or_none}),
                    'episode_number': ('episodeNumber', {int_or_none}),
                    'timestamp': ('onTime', {parse_iso8601}),
                }),
                'ie_key': 'BrightcoveNew',
            }

        if video['type'] == 'sportVideo':
            return {
                '_type': 'url_transparent',
                'id': video['media']['id'],
                'url': self.BRIGHTCOVE_URL_TEMPLATE % (
                    video['media']['accountId'], 'default', video['media']['id']),
                **traverse_obj(video, {
                    'title': 'phase',
                    'alt_title': 'subtext',
                    'description': 'description',
                    'thumbnails': ('images', ..., {'url': 'src'}),
                    'series': 'title',
                    'episode': 'phase',
                    'timestamp': ('onTime', {parse_iso8601}),
                }),
                'ie_key': 'BrightcoveNew',
            }

        if video['type'] == 'newsVideo':
            return {
                '_type': 'url_transparent',
                'id': video['media']['id'],
                'url': self.BRIGHTCOVE_URL_TEMPLATE % (
                    video['media']['accountId'], 'default', video['media']['id']),
                **traverse_obj(video, {
                    'title': 'title',
                    'description': 'description',
                    'thumbnails': ('images', ..., {'url': 'src'}),
                    'timestamp': ('onTime', {parse_iso8601}),
                }),
                'ie_key': 'BrightcoveNew',
            }


class TVPVODVideoIE(_TVPVODVideoIE, plugin_name='yt-dlp-mp4decrypt'):
    def _real_extract(self, url):
        real_call_api = self._call_api
        drm = None

        def _call_api(resource, *args, **kwargs):
            nonlocal drm
            document = real_call_api(resource, *args, **kwargs)

            if resource.endswith('/videos/playlist'):
                drm = document.get('drm')

            return document

        self._call_api = _call_api
        info_dict = super()._real_extract(url)
        self._call_api = real_call_api

        if url := traverse_obj(drm, ('WIDEVINE', 'src')):
            info_dict['_license_url'] = url

        if traverse_obj(drm, ('FAIRPLAY', 'src')):
            for fmt in info_dict['formats']:
                if fmt.get('protocol') == 'm3u8_native':
                    fmt['has_drm'] = True

        return info_dict


class UIE(InfoExtractor):
    _VALID_URL = r'https?://u\.co\.uk/shows/(?:[^/]+/)*(?P<id>\d+)'
    BRIGHTCOVE_URL_TEMPLATE = 'http://players.brightcove.net/1242911124001/0RyQs9qPh_default/index.html?videoId=%s'

    def _real_extract(self, url):
        video_id = self._match_id(url)
        webpage = self._download_webpage(url, video_id)
        app_link = self._html_search_meta('twitter:app:url:iphone', webpage, 'twitter url')
        house_number = self._search_regex(r'uktvplay://video/(\w+)/', app_link, 'house number')

        info = self._download_json(
            'https://myapi.uktvapi.co.uk/brand/', video_id,
            query={'platform_type': 'mobile', 'platform_name': 'ios', 'house_number': house_number})

        episode = info['landing_episode']
        title = episode['name'] if not episode['hide_episode_title'] \
            else 'S%s E%d' % (episode['series_number'], episode['episode_number'])

        return {
            '_type': 'url_transparent',
            'id': episode['video_id'],
            'title': episode['brand_name'] + ' - ' + title,
            'url': self.BRIGHTCOVE_URL_TEMPLATE % episode['video_id'],
            **traverse_obj(episode, {
                'thumbnail': 'image',
                'description': 'synopsis',
                'duration': 'content_duration',
                'series': 'brand_name',
                'series_id': 'brand_id',
                'season_number': ('series_number', {int_or_none}),
                'season_id': 'series_id',
                'episode_number': ('episode_number', {int_or_none}),
                'episode_id': 'id',
            }),
            'episode': episode['name'] if episode['hide_episode_title'] else None,
            'ie_key': 'BrightcoveNew',
        }


class ViuTVIE(InfoExtractor):
    _VALID_URL = r'https://viu\.tv/encore/(?P<id>[a-z0-9\-]+)(?:/(?P<episode>[a-z0-9\-]+))?'

    def _real_extract(self, url):
        programme_slug, video_slug = self._match_valid_url(url).group('id', 'episode')
        programme_data = self._download_json(
            f'https://api.viu.tv/production/programmes/{programme_slug}', programme_slug)['programme']

        episodes = traverse_obj(programme_data, (('episodes', 'clips'), ...))

        if video_slug:
            if episode := traverse_obj(episodes, (lambda _, ep: ep['slug'] == video_slug, any)):
                return self._get_episode(episode)

            raise ExtractorError('Content not found')

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
            'entries': InAdvancePagedList(
                lambda idx: (yield self._get_episode(episodes[idx])), len(episodes), 1),
        }

    def _get_formats(self, product_id):
        proxy = self.geo_verification_headers()
        vod = self._download_json(
            'https://api.viu.now.com/p8/3/getVodURL', product_id,
            data=json.dumps({
                'contentId': product_id,
                'contentType': 'Vod',
                'deviceType': 'ANDROID_WEB',
            }).encode(),
            headers=proxy,
        )

        if vod['responseCode'] == 'GEO_CHECK_FAIL':
            self.raise_geo_restricted()

        self._request_webpage(HEADRequest(vod['asset'][0]), product_id, 'Touch asset', headers=proxy)

        if '.m3u8' in vod['asset'][0]:
            fmts, subs = self._extract_m3u8_formats_and_subtitles(vod['asset'][0], product_id)
            return {'formats': fmts, 'subtitles': subs}

        fmts, subs = self._extract_mpd_formats_and_subtitles(vod['asset'][0], product_id)
        return {
            'formats': fmts,
            'subtitles': subs,
            '_cenc_key': '91ba752a446148c68400d78374b178b4:a01d7dc4edf582496b7e73d67e9e6899',
        }

    def _get_episode(self, episode):
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
                'timestamp': ('onAirStartDate', {int_or_none(scale=1000)}),
            }),
            **self._get_formats(episode['productId']),
        }
