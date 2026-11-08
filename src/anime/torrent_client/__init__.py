"""Torrent client adapters (JD-1b).

Only this package (and the batch session in batch/qb_client.py, until JD-4)
talks to the torrent client. Adapters never touch files: qBittorrent writes the
download, and the adapter reports which files a torrent uses, in one format
for every supported version (contract.ClientFile).
"""
