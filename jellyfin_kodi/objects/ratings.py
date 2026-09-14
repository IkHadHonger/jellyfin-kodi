"""Conservative, source-labelled ratings for the Piers library sync.

Never relabel CommunityRating as IMDb. Only read adjacent NFO files over
already configured Kodi paths; no scraping, directory scans or new credentials.
"""

import math
import ntpath
import posixpath
from urllib.parse import urlsplit
from xml.etree import ElementTree


MAX_NFO_BYTES = 1024 * 1024
SOURCES = frozenset(("imdb", "tomatometerallcritics", "tomatometerallaudience"))


def scaled_rating(value, maximum):
    try:
        value, maximum = float(value), float(maximum)
        if not math.isfinite(value) or not math.isfinite(maximum):
            return None
        if maximum <= 0 or not 0 <= value <= maximum:
            return None
        return round(value * 10.0 / maximum, 6)
    except (TypeError, ValueError, OverflowError):
        return None


def parse_nfo(data, media_type, provider_ids=None, require_identity=False):
    """Return only verified target rating names, scaled to Kodi's 0-10 range."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    if not data or len(data) > MAX_NFO_BYTES:
        return {}
    # Reject entity declarations, including UTF-16/32 encodings, before parsing.
    declaration_check = data.replace(b"\x00", b"").upper()
    if b"<!DOCTYPE" in declaration_check or b"<!ENTITY" in declaration_check:
        return {}
    try:
        root = ElementTree.fromstring(data)
    except (ElementTree.ParseError, ValueError):
        return {}
    if root.tag != {"movie": "movie", "tvshow": "tvshow", "episode": "episodedetails"}.get(media_type):
        return {}
    expected = {str(k).lower(): str(v).strip() for k, v in (provider_ids or {}).items() if v}
    actual = {node.get("type", "").lower(): (node.text or "").strip() for node in root.findall("uniqueid")}
    legacy_id = root.findtext("id", "").strip()
    if legacy_id.startswith("tt"):
        actual.setdefault("imdb", legacy_id)
    shared = set(expected) & set(actual)
    if any(expected[key] != actual[key] for key in shared):
        return {}
    # movie.nfo can belong to another movie in a shared directory.
    if require_identity and not shared:
        return {}
    result = {}
    duplicates = set()
    for node in root.findall("./ratings/rating"):
        source = node.get("name", "").lower()
        if source not in SOURCES:
            continue
        rating = scaled_rating(node.findtext("value"), node.get("max", "10"))
        if rating is None:
            continue
        try:
            votes = min(2**63 - 1, max(0, int(node.findtext("votes", "0"))))
        except (TypeError, ValueError):
            votes = 0
        entry = (rating, votes)
        if source in result and result[source] != entry:
            duplicates.add(source)
        result[source] = entry
    for source in duplicates:
        result.pop(source, None)
    return result


def nfo_candidates(path, media_type, direct_path):
    """(Path, require identity) pairs; never treat a server-local path as client-local."""
    if not isinstance(path, str) or not path:
        return []
    pathmod = ntpath if "\\" in path else posixpath
    if "://" in path:
        try:
            parsed = urlsplit(path)
        except ValueError:
            return []
        if parsed.scheme.lower() not in ("smb", "nfs") or parsed.query or parsed.fragment:
            return []
    elif not direct_path or not pathmod.isabs(path):
        return []
    path = path.rstrip("/\\")
    if media_type == "tvshow":
        return [(pathmod.join(path, "tvshow.nfo"), False)]
    if media_type not in ("movie", "episode"):
        return []
    directory, filename = pathmod.split(path)
    stem, ext = pathmod.splitext(filename)
    if not ext or ext.lower() in (".nfo", ".strm"):
        return []
    candidates = [(pathmod.join(directory, stem + ".nfo"), False)]
    if media_type == "movie":
        if pathmod.basename(directory).upper() in ("BDMV", "VIDEO_TS"):
            directory = pathmod.dirname(directory)
        generic = pathmod.join(directory, "movie.nfo")
        if generic != candidates[0][0]:
            candidates.append((generic, True))
    return candidates


def collect_source_ratings(item, path, media_type, direct_path, vfs=None):
    """Read bounded adjacent NFOs, preserving sync when files are unavailable."""
    result = {}
    critic = scaled_rating(item.get("CriticRating"), 100)
    if critic is not None and media_type in ("movie", "tvshow"):
        result["tomatometerallcritics"] = (critic, 0)
    candidates = nfo_candidates(path, media_type, direct_path)
    if not candidates:
        return result
    if vfs is None:
        import xbmcvfs as vfs
    for candidate, require_identity in candidates:
        try:
            if not vfs.exists(candidate):
                continue
            handle = vfs.File(candidate)
            try:
                if not 0 < handle.size() <= MAX_NFO_BYTES:
                    continue
                data = bytes(handle.readBytes(MAX_NFO_BYTES + 1))
            finally:
                handle.close()
            ratings = parse_nfo(data, media_type, item.get("ProviderIds"), require_identity)
        except Exception:
            # Optional metadata must not stop a library sync; do not log paths
            # because SMB URLs may contain credentials.
            continue
        if ratings:
            result.update(ratings)
            break
    return result


def sync_source_ratings(cursor, media_id, media_type, ratings):
    """Upsert only our three named sources; leave default/TMDb/Trakt untouched.

    No deletions: a missing/unreachable NFO cannot erase previously valid ratings.
    The caller owns the existing library transaction.
    """
    if media_type not in ("movie", "tvshow", "episode"):
        raise ValueError("Unsupported media type")
    for source, (rating, votes) in ratings.items():
        if source not in SOURCES or scaled_rating(rating, 10) is None:
            continue
        cursor.execute(
            "SELECT rating_id FROM rating WHERE media_id = ? AND media_type = ? AND rating_type = ?",
            (media_id, media_type, source),
        )
        row = cursor.fetchone()
        if row:
            cursor.execute(
                "UPDATE rating SET rating = ?, votes = ? WHERE rating_id = ?",
                (rating, votes, row[0]),
            )
        else:
            cursor.execute("SELECT coalesce(max(rating_id), 0) + 1 FROM rating")
            rating_id = cursor.fetchone()[0]
            cursor.execute(
                "INSERT INTO rating(rating_id, media_id, media_type, rating_type, rating, votes) VALUES (?, ?, ?, ?, ?, ?)",
                (rating_id, media_id, media_type, source, rating, votes),
            )
