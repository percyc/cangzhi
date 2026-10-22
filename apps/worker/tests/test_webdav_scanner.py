import asyncio
import threading

from apps.worker.services import webdav_scanner


def test_scanner_reuses_one_client_and_stops_between_batches(monkeypatch):
    stop = threading.Event()
    clients = []
    async def batch(_sessions, *, client):
        clients.append(client)
        if len(clients) == 2:
            stop.set()
        return True
    monkeypatch.setattr(webdav_scanner, 'process_scan_batch', batch)
    asyncio.run(webdav_scanner.scan_loop('sqlite://', stop))
    assert len(clients) == 2 and clients[0] is clients[1]
    assert clients[0].is_closed


def test_scanner_does_not_log_raw_exception(monkeypatch, capsys):
    stop = threading.Event()
    async def batch(_sessions, *, client):
        stop.set()
        raise RuntimeError('secret-connection-string')
    monkeypatch.setattr(webdav_scanner, 'process_scan_batch', batch)
    asyncio.run(webdav_scanner.scan_loop('sqlite://', stop))
    assert 'secret-connection-string' not in capsys.readouterr().out
