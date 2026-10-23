"""Native-only download import; no local-path HTTP endpoint is exposed to websites."""
import http.client
import ipaddress
import json
from pathlib import Path
import re
import socket
import urllib.parse
import zipfile

MAX_BOOK = 200 * 1024 * 1024
BOOK_TYPES = {'.epub', '.pdf', '.azw', '.azw3', '.mobi', '.prc', '.txt'}
IMAGE_TYPES = {'.jpg', '.jpeg', '.png'}
MAX_IMAGE = 8 * 1024 * 1024


def public_https(url):
    try:
        parsed = urllib.parse.urlsplit(url)
        host = parsed.hostname
        if parsed.scheme != 'https' or not host or parsed.username is not None or parsed.password is not None:
            return False
        if host.lower().rstrip('.') == 'localhost' or host.lower().rstrip('.').endswith(('.localhost', '.local')):
            return False
        addresses = socket.getaddrinfo(host, parsed.port or 443, type=socket.SOCK_STREAM)
        return bool(addresses) and all(ipaddress.ip_address(item[4][0]).is_global for item in addresses)
    except (ValueError, OSError):
        return False


def download_name(value, image=False):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', '_', value).strip(' .')
    if not name or len(name.encode('utf-8')) > 220:
        raise ValueError('下载文件名为空或过长，请在网站选择较短的文件名。')
    if Path(name).suffix.lower() not in (IMAGE_TYPES if image else BOOK_TYPES):
        raise ValueError('封面入口仅保存 JPEG/PNG。' if image else '下载入口仅保存 EPUB、PDF、AZW/AZW3、MOBI、PRC、TXT，不下载安装程序或网页。')
    return name


def validate_book(path):
    if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= MAX_BOOK:
        raise ValueError('文件缺失、为空或超过 200 MiB。')
    ext = path.suffix.lower()
    with path.open('rb') as stream:
        header = stream.read(8192)
    if ext == '.epub':
        try:
            with zipfile.ZipFile(path) as archive:
                info = archive.getinfo('mimetype')
                valid = info.file_size <= 100 and archive.read(info) == b'application/epub+zip' and 'META-INF/container.xml' in archive.namelist()
        except (OSError, KeyError, zipfile.BadZipFile, RuntimeError):
            valid = False
    elif ext == '.pdf':
        valid = header.startswith(b'%PDF-')
    elif ext in {'.mobi', '.azw', '.azw3', '.prc'}:
        valid = header[60:68] == b'BOOKMOBI'
    elif ext == '.txt':
        valid = b'\0' not in header
        try:
            text = header.decode('utf-8-sig')
        except UnicodeDecodeError:
            text = header.decode('gb18030', errors='replace')
        valid = valid and not re.search(r'<!doctype\s+html|<html\b|<form\b|<script\b', text, re.I)
    else:
        valid = False
    if not valid:
        raise ValueError('内容与电子书格式不符，可能下载到了登录页或错误页面。请检查后重试。')


def validate_image(path):
    import warnings
    try:
        from PIL import Image, UnidentifiedImageError
    except ImportError as error:
        raise ValueError('图片校验需要系统 python3-pil。') from error
    if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= MAX_IMAGE:
        raise ValueError('图片缺失、为空或超过 8 MiB。')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(path) as image:
                if image.format not in {'JPEG', 'PNG'} or image.width * image.height > 20000000 or getattr(image, 'n_frames', 1) != 1:
                    raise ValueError('只支持单帧 JPEG/PNG，最多 2000 万像素。')
                image.verify()
    except (OSError, UnidentifiedImageError, Image.DecompressionBombError, Image.DecompressionBombWarning) as error:
        raise ValueError('图片损坏、格式无效或尺寸过大。') from error


def local_json(path):
    connection = http.client.HTTPConnection('127.0.0.1', 8765, timeout=30)
    try:
        connection.request('GET', path)
        response = connection.getresponse()
        result = json.loads(response.read())
        if response.status != 200:
            raise ValueError(result.get('error', '本地图书服务不可用。'))
        return result
    finally:
        connection.close()


def apply_downloaded_cover(path, file_id):
    validate_image(path)
    state = local_json('/api/state')
    connection = http.client.HTTPConnection('127.0.0.1', 8765, timeout=240)
    try:
        with path.open('rb') as source:
            connection.request('POST', '/api/library/cover?' + urllib.parse.urlencode({'id': file_id}), body=source,
                               headers={'X-Desk-Token': state['token'], 'Content-Length': str(path.stat().st_size)})
            response = connection.getresponse()
            result = json.loads(response.read())
        if response.status != 200:
            raise ValueError(result.get('error', '补封面失败。'))
        return result
    finally:
        connection.close()


def import_download(path):
    validate_book(path)
    connection = http.client.HTTPConnection('127.0.0.1', 8765, timeout=240)
    try:
        connection.request('GET', '/api/state')
        response = connection.getresponse()
        state = json.loads(response.read())
        if response.status != 200 or not isinstance(state.get('token'), str):
            raise ValueError('本地图书服务未就绪，请返回图书馆重试。')
        # Stream from the native-selected file, never accept a path supplied by a remote page.
        with path.open('rb') as source:
            connection.request('POST', '/api/library/import?' + urllib.parse.urlencode({'name': path.name}), body=source,
                               headers={'X-Desk-Token': state['token'], 'Content-Length': str(path.stat().st_size)})
            response = connection.getresponse()
            result = json.loads(response.read())
        if response.status != 200:
            raise ValueError(result.get('error', '导入失败，请重试。'))
        return result
    finally:
        connection.close()
