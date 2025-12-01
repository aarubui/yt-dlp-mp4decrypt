import hashlib
import os
import re
import subprocess
import tempfile

from pywidevine.cdm import Cdm
from pywidevine.device import Device
from pywidevine.pssh import PSSH
from yt_dlp.networking.common import Request
from yt_dlp.postprocessor.common import PostProcessor
from yt_dlp.utils import (
    Popen,
    PostProcessingError,
    UnavailableVideoError,
    prepend_extension,
    truncate_string,
    variadic,
)


def _inject_mixin(obj, mixin, pp):
    if obj.__module__ != __name__:
        obj_type = type(obj)
        obj.__class__ = type(obj_type.__name__, (mixin, obj_type), {
            '_mixin_class': obj_type,
            '_mixin_pp': pp,
        })


class Mp4DecryptPP(PostProcessor):
    def __init__(self, downloader=None, **kwargs):
        self._decryptor = Mp4DecryptDecryptor()
        super().__init__(downloader)
        self._kwargs = kwargs
        self._pssh = {}
        self._license_urls = {}
        self._keys = {}

    def set_downloader(self, downloader):
        _inject_mixin(downloader, Mp4DecryptDownloader, self)
        self._decryptor.set_downloader(downloader)
        return super().set_downloader(downloader)

    def add_mpd(self, mpd_url, pssh, license_url):
        if pssh:
            self._pssh[mpd_url] = pssh

        self._license_urls[mpd_url] = license_url

    def run(self, info):
        has_license = any(key in info for key in ('_cenc_key', '_license_url', '_license_callback'))

        for part in info.get('requested_formats', (info,)):
            if (has_license and part.get('protocol') == 'm3u8_native') or self._is_encrypted(part):
                self._add_keys(info, part)

        return [], info

    def _is_encrypted(self, part):
        return part.get('container') in ('mp4_dash', 'm4a_dash') and \
            part.get('manifest_url') in self._license_urls

    def _add_keys(self, info, part):
        if '__real_download' in info:
            raise PostProcessingError(f'{self.PP_NAME} must be used with \'when=before_dl\'')

        if keys := self._get_keys(info, part):
            part['_mp4decrypt'] = keys
        else:
            raise UnavailableVideoError('No keys found for ' + part['format_id'])

        if self._decryptor not in info.get('__postprocessors', []):
            info.setdefault('__postprocessors', [])
            info['__postprocessors'].append(self._decryptor)

    def _get_keys(self, info, part):
        if keys := info.get('_cenc_key'):
            return tuple([arg for key in variadic(keys, str) for arg in ('--key', key)])

        mpd_url = part['manifest_url']

        if mpd_url in self._pssh:
            pssh = self._pssh[mpd_url]
        else:
            pssh = self._pssh[mpd_url] = self._pssh_from_init(part)

        if not pssh:
            return ()

        if keys := self._keys.get(pssh):
            return keys

        cache_args = ('mp4decrypt-pssh', hashlib.md5(pssh.encode('ascii')).hexdigest())

        if (data := self._downloader.cache.load(*cache_args)) \
                and data['pssh'] == pssh and (keys := data['keys']):
            for i in range(1, len(keys), 2):
                self.to_screen(f'Loaded key from cache: {keys[i]}')
            self._keys[pssh] = keys
            return keys

        license_callback = info.get('_license_callback')
        license_urls = info.get('_license_url', self._license_urls.get(mpd_url))
        license_url = license_urls[mpd_url] if isinstance(license_urls, dict) else license_urls

        if not license_callback and license_url:

            def license_callback(challenge, license_url):
                self.to_screen('Fetching keys from ' + truncate_string(license_url, 100, 20))
                return self._downloader.urlopen(Request(
                    license_url, data=challenge,
                    headers={'Content-Type': 'application/octet-stream'})).read()

        if license_callback:
            return self._fetch_keys(pssh, license_callback, cache_args, mpd_url, license_url)

        return ()

    def _pssh_from_init(self, part):
        def find_wv_pssh_offsets(raw):
            offset = 0

            while (offset := raw.find(b'pssh', offset)) != -1:
                pssh_offset = offset - 4
                size = int.from_bytes(raw[pssh_offset:offset], byteorder='big')
                offset += size
                yield PSSH(raw[pssh_offset:pssh_offset + size])

        init_data = b''
        temp_file = tempfile.NamedTemporaryFile(suffix='.tmp', delete=False)
        temp_file.close()
        success, _ = self._downloader.dl(temp_file.name, part, test=True)

        if success:
            with open(temp_file.name, 'rb') as f:
                init_data = f.read()
            os.remove(temp_file.name)

        for pssh in find_wv_pssh_offsets(init_data):
            if pssh.system_id == PSSH.SystemId.Widevine:
                self.to_screen('Extracted PSSH from init segment')
                return pssh.dumps()

        self.report_warning('Could not find PSSH for ' + part['format_id'])
        return None

    def _fetch_keys(self, pssh, callback, cache_args, mpd_url, license_url=None):
        keys = ()

        if devicepath := self._kwargs.get('devicepath'):
            cdm = Cdm.from_device(Device.load(devicepath))
            session_id = cdm.open()
            challenge = cdm.get_license_challenge(session_id, PSSH(pssh), 'STREAMING', privacy_mode=True)
            license_msg = callback(challenge, license_url) if license_url else callback(challenge)
            cdm.parse_license(session_id, license_msg)

            for key in cdm.get_keys(session_id):
                if key.type == 'CONTENT':
                    keyarg = f'{key.kid.hex}:{key.key.hex()}'
                    self.to_screen(f'Fetched key: {keyarg}')
                    keys += ('--key', keyarg)

        self._keys[pssh] = keys
        self._downloader.cache.store(*cache_args, {'pssh': pssh, 'keys': keys})
        return keys


class Mp4DecryptDownloader:
    def add_info_extractor(self, ie):
        _inject_mixin(ie, Mp4DecryptExtractor, self._mixin_pp)
        return self._mixin_class.add_info_extractor(self, ie)


class Mp4DecryptExtractor:
    def _parse_mpd_periods(self, mpd_doc, mpd_id=None, *args, **kwargs):
        elements = mpd_doc.findall('.//{*}ContentProtection')
        found = False

        for element in elements:
            if element.get('schemeIdUri').lower() == PSSH.SystemId.Widevine.urn:
                self._mixin_pp.add_mpd(
                    kwargs.get('mpd_url') or args[1],
                    element.findtext('./{*}pssh'),
                    element.get('{urn:brightcove:2015}licenseAcquisitionUrl'),
                )
                found = True

        if elements and found:
            # treat formats as unprotected
            for parent in mpd_doc.findall('.//*/..[{*}ContentProtection]'):
                for child in parent.findall('{*}ContentProtection'):
                    parent.remove(child)

        roles = {}

        for adaptation_set in mpd_doc.findall('.//{*}AdaptationSet'):
            if adaptation_set.get('mimeType') != 'audio/mp4' and \
                    adaptation_set.get('contentType') != 'audio':
                continue
            if (role := adaptation_set.find('{*}Role')) is not None:
                for representation in adaptation_set.findall('{*}Representation[@id]'):
                    format_id = representation.get('id')

                    if mpd_id:
                        format_id = mpd_id + '-' + format_id

                    roles[format_id] = role.get('value')

        for period_entry in self._mixin_class._parse_mpd_periods(self, mpd_doc, mpd_id, *args, **kwargs):
            for fmt in period_entry['formats']:
                if role := roles.get(fmt['format_id']):
                    fmt['format_note'] += f' ({role})'
                    if role in ('description', 'alternate'):
                        fmt['preference'] = -2

            yield period_entry

    def _parse_brightcove_metadata(self, json_data, *args, **kwargs):
        for source in json_data.get('sources') or []:
            if 'com.widevine.alpha' in source.get('key_systems', {}):
                del source['key_systems']

        return self._mixin_class._parse_brightcove_metadata(self, json_data, *args, **kwargs)

    def _extract_from_streaks_api(self, *args, **kwargs):
        real_methods = {}
        license_urls = {}

        def swap_method(name, method):
            real_methods[name] = getattr(self, name)
            setattr(self, name, method)

        def unswap_method(name):
            if method := real_methods.get(name):
                setattr(self, name, method)
                del real_methods[name]

        def _m3u8_override(m3u8_url, video_id, *args, **kwargs):
            kwargs.update(zip(real_methods['_extract_m3u8_formats_and_subtitles'].__code__.co_varnames[3:], args))
            return self._extract_mpd_formats_and_subtitles(
                m3u8_url, video_id, mpd_id=kwargs.get('m3u8_id'),
                **{key: kwargs[key] for key in self._extract_mpd_periods.__code__.co_varnames if key in kwargs})

        def _parse_json_override(*args, **kwargs):
            response = real_methods['_parse_json'](*args, **kwargs)
            unswap_method('_parse_json')
            drm_sources = []

            for source in response.get('sources', []):
                if key_system := source.get('key_systems', {}).get('com.widevine.alpha'):
                    drm_sources.append({**source, 'key_systems': {}, 'type': 'application/x-mpegURL'})
                    license_urls[source['src']] = key_system['license_url']

            if drm_sources:
                response['sources'] = drm_sources
                swap_method('_extract_m3u8_formats_and_subtitles', _m3u8_override)

            return response

        swap_method('_parse_json', _parse_json_override)
        info_dict = self._mixin_class._extract_from_streaks_api(self, *args, **kwargs)
        unswap_method('_extract_m3u8_formats_and_subtitles')

        if license_urls:
            info_dict['_license_url'] = license_urls

        return info_dict


class Mp4DecryptDecryptor(PostProcessor):
    def run(self, info):
        to_delete, encrypted = [], []

        if 'requested_formats' in info:
            encrypted = [p for p in info['requested_formats'] if self._is_encrypted(p)]
        elif info.get('__real_download') and self._is_encrypted(info):
            encrypted.append(info)

        if encrypted:
            self.to_screen('[Mp4Decrypt] Decrypting format(s)', prefix=False)
            for part in encrypted:
                self._decrypt_part(info, part, to_delete)
                del part['_mp4decrypt']

        return to_delete, info

    def _is_encrypted(self, info):
        return 'filepath' in info and '_mp4decrypt' in info

    def _decrypt_part(self, info, part, to_delete):
        filepath = part['filepath']
        tmppath = prepend_extension(filepath, 'decrypted')

        if not os.path.exists(tmppath):
            self._run_mp4decrypt(filepath, tmppath, part['_mp4decrypt'])

        if filepath in info.get('__files_to_merge', []):
            idx = info['__files_to_merge'].index(filepath)
            info['__files_to_merge'][idx] = tmppath
            to_delete.append(filepath)
        else:
            os.replace(tmppath, filepath)

    def _run_mp4decrypt(self, filepath, tmppath, keys):
        cwd = os.path.dirname(filepath)
        filename = os.path.basename(filepath)
        tmpname = os.path.basename(tmppath)
        renames = {}

        if os.name == 'nt':
            # mp4decrypt on Windows cannot handle certain filenames
            safe_filename = re.sub(r'[^\x20-\x7E]+', '', filename)

            if safe_filename != filename:
                os.rename(filepath, os.path.join(cwd, safe_filename))
                renames[safe_filename] = filename
                filename = safe_filename
                safe_tmpname = prepend_extension(safe_filename, 'decrypted')
                renames[safe_tmpname] = tmpname
                tmpname = safe_tmpname

        cmd = ('mp4decrypt', *keys, filename, tmpname)
        _, stderr, returncode = Popen.run(
            cmd, cwd=cwd or None, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.PIPE)

        if returncode != 0:
            raise PostProcessingError(stderr)

        for from_name, to_name in renames.items():
            os.replace(os.path.join(cwd, from_name), os.path.join(cwd, to_name))
