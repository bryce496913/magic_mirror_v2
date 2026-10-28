"""Bounded RSS/Atom headline retrieval with safe XML parsing."""

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import requests
from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException

from . import ProviderUnavailable

MAX_FEED_BYTES = 512_000


def _child(node, name):
    return next((item for item in node if item.tag.rsplit("}", 1)[-1] == name), None)


def _text(node, name):
    child = _child(node, name)
    return " ".join("".join(child.itertext()).split()) if child is not None else ""


def _published(value):
    if not value:
        return None
    try:
        date = parsedate_to_datetime(value) if "," in value else datetime.fromisoformat(value.replace("Z", "+00:00"))
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        return date.astimezone(timezone.utc).isoformat()
    except (ValueError, TypeError):
        return None


def _parse_feed(content):
    root = ElementTree.fromstring(content)
    channel = _child(root, "channel") if root.tag.rsplit("}", 1)[-1] == "rss" else root
    if channel is None or root.tag.rsplit("}", 1)[-1] not in {"rss", "feed"}:
        raise ProviderUnavailable("News feed format is unsupported")
    publisher = _text(channel, "title") or "Unknown publisher"
    items = [item for item in channel if item.tag.rsplit("}", 1)[-1] in {"item", "entry"}]
    return [{"title": _text(item, "title")[:220],
             "publisher": (_text(item, "source") or publisher)[:80],
             "published_at": _published(_text(item, "pubDate") or _text(item, "published") or _text(item, "updated"))}
            for item in items if _text(item, "title")]


def read_news(settings):
    if not settings.news_feeds:
        raise ProviderUnavailable("Feeds not configured")
    headlines, seen = [], set()
    for url in settings.news_feeds:
        try:
            with requests.get(url, timeout=(3, 5), stream=True) as response:
                response.raise_for_status()
                chunks, length = [], 0
                for chunk in response.iter_content(8192):
                    length += len(chunk)
                    if length > MAX_FEED_BYTES:
                        raise ProviderUnavailable("News feed is too large")
                    chunks.append(chunk)
            for item in _parse_feed(b"".join(chunks)):
                key = item["title"].casefold()
                if key not in seen:
                    seen.add(key)
                    headlines.append(item)
        except (requests.RequestException, ElementTree.ParseError, DefusedXmlException, ProviderUnavailable):
            continue
    if not headlines:
        raise ProviderUnavailable("No headlines available")
    return {"headlines": headlines[:3]}
