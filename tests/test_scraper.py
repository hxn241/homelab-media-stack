from mediaserver import scraper


def test_collect_torrent_links():
    html = """
    <a href="/torrents/peliculas/movie.torrent">Descargar</a>
    <a href="magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567">Magnet</a>
    <a href="/another-page">nothing</a>
    """
    links = scraper._collect_torrent_links("https://example.org/pelicula/1", html)

    assert "https://example.org/torrents/peliculas/movie.torrent" in links
    assert any(l.startswith("magnet:?xt=urn:btih:0123456789abcdef") for l in links)
    assert not any("another-page" in l for l in links)


def test_block_page_reason():
    assert scraper._block_page_reason("<title>Just a moment...</title>")
    assert scraper._block_page_reason("<html><body>ok</body></html>") is None


def test_add_page_without_url():
    client = scraper.app.test_client()
    r = client.get("/add_page")
    assert r.status_code == 400
