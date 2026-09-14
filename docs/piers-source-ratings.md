# Piers source-labelled rating repair

This change is confined to `cube-custom`. The skin, Skin Info Service,
TMDb/Trakt rating rows and AVDVPlus channel are not modified.

## Sources

- IMDb: only a named `imdb` rating from an adjacent, readable NFO.
- RT Critics: `tomatometerallcritics` from that NFO, otherwise Jellyfin's
  `CriticRating` (0–100, converted to Kodi's 0–10 scale).
- RT Audience: only `tomatometerallaudience` from that NFO. Average critic
  scores and Jellyfin `CommunityRating` are never used as audience scores.
- Jellyfin's generic `default` score retains its original meaning and update
  behavior. It is never labelled IMDb based merely on the presence of an IMDb ID.

Named ratings are written in the existing Kodi video database transaction.
The default-rating lookup now explicitly selects `rating_type = 'default'`,
so future syncs cannot accidentally relabel another source's row. Missing
default rows are recreated without overwriting another source.

## NFO access and limitations

Uses the add-on's existing path mapping and Kodi VFS credentials, read-only.
SMB/NFS media paths are supported in both playback modes. Local absolute paths
are used only in native/direct-path mode, to avoid interpreting a server-local
path as a path on the client. HTTP, plugin, stack and STRM paths are skipped.
There is no server filesystem access API, scraping, new API key or credential
discovery in this patch.

Movies/episodes first use the matching filename with `.nfo` replacing the
extension. Shows use `tvshow.nfo`. A movie's generic `movie.nfo` fallback
requires an external ID matching the Jellyfin item to avoid using another film's
scores in shared directories. Disc directories also support this guarded fallback.
Conflicting external IDs and wrong media root elements are rejected.

Only the three target rating names are read. NFO scale, numeric bounds and
finite values are checked. File reads are capped at 1 MiB, and XML DTD/entity
declarations are rejected. Missing, malformed or inaccessible files do not
abort sync or delete previous ratings. Therefore an unavailable or removed
source may leave a previously stored score intact; this is deliberate.

If Kodi cannot read the NFO, IMDb and RT Audience cannot be reconstructed from
Jellyfin's generic rating. Existing online sources in Skin Info Service still
work unchanged; the skin's existing online-first precedence also remains intact.

## Deployment and verification

The Piers deploy workflow runs `tests/test_source_ratings.py` before building.
Its existing run-number version increment creates a higher Kodi add-on version.
This changes Jellyfin for Kodi, not the skin's version.

After updating, existing titles need a metadata synchronization to gain the
new rows. Do not reset/delete the Kodi database or change content scrapers.
First verify one known movie with a readable NFO: the supplied sample should
produce `imdb = 6.5` and `tomatometerallcritics = 8.6` (displayed as 86%).
It must not create an RT Audience score because that sample has none.
Confirm with an additional title that contains `tomatometerallaudience`.
Then synchronize the rest of the affected library and reload the skin/service
if its cached properties still show old values.

Unit and in-memory SQLite tests are not a live Kodi/CoreELEC test. Client NFO
access and the actual displayed flags still require verification on the Cube.
