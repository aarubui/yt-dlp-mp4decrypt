import os
import subprocess
from os import rename, replace
from re import sub

from pywidevine.cdm import Cdm
from pywidevine.device import Device
from pywidevine.pssh import PSSH
from yt_dlp.networking.common import Request
from yt_dlp.postprocessor import FFmpegMergerPP
from yt_dlp.postprocessor.common import PostProcessor
from yt_dlp.utils import Popen, PostProcessingError


class Mp4DecryptPP(PostProcessor):
    _WVXPATH = './/{*}ContentProtection[@schemeIdUri=\'' + PSSH.SystemId.Widevine.urn + '\']'

    def __init__(self, downloader=None, **kwargs):
        PostProcessor.__init__(self, downloader)
        self._sniff_mpds(downloader)
        self._kwargs = kwargs
        self._pssh = {}
        self._license_urls = {}
        self._keys = {}

    def _sniff_mpds(self, downloader):
        oldextmethod = downloader.add_info_extractor

        def newextmethod(ie):
            oldmpdmethod = ie._parse_mpd_periods

            def newmpdmethod(mpd_doc, *args, **kwargs):
                if (element := mpd_doc.find(self._WVXPATH)) is not None:
                    mpd_url = kwargs.get('mpd_url') or args[2]
                    self._pssh[mpd_url] = element.findtext('./{*}pssh')
                    self._license_urls[mpd_url] = element.get('{urn:brightcove:2015}licenseAcquisitionUrl')
                elif mpd_doc.find('.//{*}ContentProtection') is not None:
                    # remove playready, etc.
                    return []

                return oldmpdmethod(mpd_doc, *args, **kwargs)

            ie._parse_mpd_periods = newmpdmethod
            oldextmethod(ie)

        downloader.add_info_extractor = newextmethod

    def run(self, info):
        encrypted = []

        if 'requested_formats' in info:
            encrypted = [p for p in info['requested_formats'] if self._is_encrypted(p)]
        elif info['__real_download'] and self._is_encrypted(info):
            encrypted.append(info)

        if encrypted:
            decrypted = True

            for part in encrypted:
                if part['protocol'] == 'http_dash_segments' and (keys := self._get_keys(info, part)):
                    self._decrypt_part(keys, part['filepath'])
                else:
                    self.to_screen('Cannot decrypt ' + part['format_id'])
                    decrypted = False

            if decrypted and '+' in info['format_id']:
                info['__files_to_merge'] = [part['filepath'] for part in info['requested_formats']]
                info = self._downloader.run_pp(FFmpegMergerPP(self._downloader), info)

        return [], info

    def _is_encrypted(self, info):
        return 'filepath' in info and info.get('has_drm')

    def _get_keys(self, info, part):
        if key := info.get('_cenc_key'):
            return ('--key', key)

        mpd_url = part['manifest_url']
        pssh = self._pssh.get(mpd_url)
        license_callback = info.get('_license_callback')
        license_url = info.get('_license_url', self._license_urls.get(mpd_url))

        if not license_callback and license_url:
            def license_callback(challenge):
                self.to_screen(f'Fetching keys from {license_url}')
                return self._downloader.urlopen(Request(license_url, data=challenge)).read()

        if not pssh:
            pssh = self._pssh_from_init(part)

        if pssh and license_callback:
            return self._fetch_keys(pssh, license_callback)

        return ()

    def _pssh_from_init(self, part):
        def find_wv_pssh_offsets(raw):
            offset = 0

            while True:
                offset = raw.find(b'pssh', offset)

                if offset == -1:
                    break

                pssh_offset = offset - 4
                size = int.from_bytes(raw[pssh_offset:offset], byteorder='big')
                offset += size
                yield PSSH(raw[pssh_offset:pssh_offset + size])

        init_data = self._downloader.urlopen(Request(
            part['fragment_base_url'] + part['fragments'][0]['path'],
            headers=part['http_headers'])).read()

        for pssh in find_wv_pssh_offsets(init_data):
            if pssh.system_id == PSSH.SystemId.Widevine:
                self.to_screen('Extracted PSSH from init segment')
                return pssh.dumps()

        return None

    def _fetch_keys(self, pssh, callback):
        if keys := self._keys.get(pssh):
            return keys

        keys = ()

        if devicepath := self._kwargs.get('devicepath'):
            cdm = Cdm.from_device(Device.load(devicepath))
            session_id = cdm.open()
            challenge = cdm.get_license_challenge(session_id, PSSH(pssh), 'STREAMING', privacy_mode=True)
            cdm.parse_license(session_id, callback(challenge))

            for key in cdm.get_keys(session_id):
                if key.type == 'CONTENT':
                    keyarg = f'{key.kid.hex}:{key.key.hex()}'
                    self.to_screen(f'Fetched key: {keyarg}')
                    keys += ('--key', keyarg)

        self._keys[pssh] = keys
        return keys

    def _decrypt_part(self, keys, filepath):
        originalpath = filepath

        if os.name == 'nt':
            # mp4decrypt on Windows cannot handle certain filenames
            filepath = sub(r'[^0-9A-z_\-.]+', '', filepath)
            rename(originalpath, filepath)

        tmppath = '_decrypted_' + filepath
        cmd = ('mp4decrypt', *keys, filepath, tmppath)

        _, stderr, returncode = Popen.run(
            cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.PIPE)

        if returncode != 0:
            raise PostProcessingError(stderr)

        if filepath != originalpath:
            rename(filepath, originalpath)
            filepath = originalpath

        replace(tmppath, filepath)
