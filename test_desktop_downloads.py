from contextlib import closing
import http.client
import json
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch
import zipfile

from desktop_downloads import download_name, import_download, public_https, validate_book, validate_image, apply_downloaded_cover
from server import Desk, make_server


class DownloadTest(unittest.TestCase):
    def test_public_https_never_allows_loopback_private_hosts_or_credentials(self):
        public = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 443))]
        with patch('desktop_downloads.socket.getaddrinfo', return_value=public):
            self.assertTrue(public_https('https://books.example/catalog'))
            for url in ['http://books.example', 'file:///etc/passwd', 'https://localhost/', 'https://thing.local/',
                        'https://user:password@books.example/', 'javascript:alert(1)', 'https://books.example:bad/']:
                with self.subTest(url=url): self.assertFalse(public_https(url))
        for address in ['127.0.0.1', '192.168.1.1', '10.0.0.1', '169.254.169.254', '::1', 'fd00::1', '::ffff:127.0.0.1']:
            with patch('desktop_downloads.socket.getaddrinfo', return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, '', (address, 443))]):
                self.assertFalse(public_https('https://any-name.example/'))
        with patch('desktop_downloads.socket.getaddrinfo', side_effect=OSError('DNS failed')):
            self.assertFalse(public_https('https://invalid.example/'))

    def test_names_cannot_escape_and_executable_downloads_are_refused(self):
        self.assertEqual(download_name('../../Book.epub'), '_.._Book.epub')
        for name in ['setup.exe', 'Book.html', '', 'x' * 240 + '.epub']:
            with self.subTest(name=name), self.assertRaises(ValueError): download_name(name)
        self.assertNotIn('/', download_name('Books/Book.pdf'))
        self.assertNotIn('\\', download_name('Books\\Book.pdf'))

    def test_format_check_rejects_login_pages_corruption_empty_files_and_symlinks(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            epub = root / 'Book.epub'
            with zipfile.ZipFile(epub, 'w') as archive:
                archive.writestr('mimetype', 'application/epub+zip')
                archive.writestr('META-INF/container.xml', '<container/>')
            validate_book(epub)
            for name, content in [('login.epub', b'<html>Login</html>'), ('login.txt', b'<!doctype html><form>Login</form>'),
                                  ('bad.pdf', b'not PDF'), ('empty.txt', b''), ('bad.mobi', b'invalid')]:
                path = root / name; path.write_bytes(content)
                with self.subTest(name=name), self.assertRaises(ValueError): validate_book(path)
            (root / 'link.epub').symlink_to(epub)
            with self.assertRaises(ValueError): validate_book(root / 'link.epub')
            pdf = root / 'Book.pdf'; pdf.write_bytes(b'%PDF-1.7\nfixture')
            validate_book(pdf)
            txt = root / 'Book.txt'; txt.write_text('中文正文')
            validate_book(txt)

    def test_images_are_strictly_separate_from_books_and_bounded(self):
        from PIL import Image
        self.assertEqual(download_name('wallpaper.jpeg', image=True), 'wallpaper.jpeg')
        with self.assertRaises(ValueError): download_name('wallpaper.jpeg')
        with self.assertRaises(ValueError): download_name('Book.epub', image=True)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'wallpaper.png'
            Image.new('RGB', (720, 1280), 'white').save(path)
            validate_image(path)
            with self.assertRaises(ValueError): validate_book(path)
            path.write_bytes(b'<html>not a wallpaper</html>')
            with self.assertRaises(ValueError): validate_image(path)
            Image.new('RGB', (5000, 5000)).save(path)
            with self.assertRaises(ValueError): validate_image(path)

    def test_native_cover_stream_uses_existing_endpoint_without_deleting_image(self):
        from PIL import Image
        from unittest.mock import MagicMock
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'cover.png'; Image.new('RGB', (30, 40)).save(path)
            connection = MagicMock()
            connection.getresponse.return_value.status = 200
            connection.getresponse.return_value.read.return_value = b'{"message":"covered"}'
            with patch('desktop_downloads.local_json', return_value={'token': 'local-token'}), patch('desktop_downloads.http.client.HTTPConnection', return_value=connection):
                result = apply_downloaded_cover(path, 'file-id')
            self.assertEqual(result['message'], 'covered')
            request = connection.request.call_args
            self.assertEqual(request.args[:2], ('POST', '/api/library/cover?id=file-id'))
            self.assertEqual(request.kwargs['headers']['X-Desk-Token'], 'local-token')
            self.assertTrue(path.exists())

    def test_native_stream_import_uses_existing_token_endpoint_deduplicates_and_keeps_download(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / 'Downloaded.txt'; path.write_text('正文\n' * 10)
            desk = Desk(root / 'device', root / 'data'); desk.library.metadata_tool = None
            server = make_server(desk, 0)
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            connection = http.client.HTTPConnection
            try:
                with patch('desktop_downloads.http.client.HTTPConnection', side_effect=lambda host, port, timeout: connection(host, server.server_address[1], timeout=timeout)):
                    first = import_download(path)
                    second = import_download(path)
                self.assertFalse(first['duplicate']); self.assertTrue(second['duplicate'])
                self.assertTrue(path.exists())
                self.assertEqual(desk.library.listing()['total'], 1)
            finally:
                server.shutdown(); server.server_close(); thread.join()


if __name__ == '__main__':
    unittest.main()
