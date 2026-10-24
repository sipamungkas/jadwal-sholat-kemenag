"""Scraper for jadwal sholat (prayer times) from Kemenag's Bimas Islam API.

Endpoints used (all GET, JSON):
  /api/sihat/provinsi                      -> list of provinces
  /api/sihat/kabko?prov=<id>               -> regencies/cities in a province
  /api/sihat/jadwal?prov=&kabko=&thn=&bln= -> one month of prayer times

The `id` values are opaque encrypted tokens; pass them through verbatim.
"""

__version__ = "1.0.0"
