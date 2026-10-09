# beets-oldestdate

Beets plugin that fetches oldest recording or release date for each track. This is especially useful when tracks are
from best-of compilations, remasters, or re-releases. Originally based on `beets-recordingdate` by tweitzel, but almost
entirely rewritten to actually work with MusicBrainz's incomplete information. The found date is stored in the
`recording_year`, `recording_month` and `recording_day` tags, for compatibility with `beets-recordingdate`, and can
optionally overwrite the date tags.

# Installation

Simply run `pip install beets-oldestdate` then add `oldestdate` to the list of active plugins in beets and configure as
necessary. The plugin works both in singleton and album import modes, using a different approach for each (see below).

# Approaches

The date is looked up on MusicBrainz (JSON web service) using one of three approaches:

| Approach    | How the date is found                                                                                                                                         |
|:-----------:|:--------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `release`   | Item's `mb_albumid` → release → first release date of its release group (i.e. the album's original date).                                                  |
| `recording` | Item's `mb_trackid` (recording) → first release date of the recording.                                                                                       |
| `work`      | Item's recording → its work → oldest first release date among all recordings of that work (covers, artists and attributes are filtered, see below). |

For example, for the [Thriller recording](https://musicbrainz.org/recording/2eec3a3b-33af-4ea1-b169-7450b941732d) found
on a 2005 release, the `recording` and `release` approaches give 2005 while the `work` approach gives 1982.

The approach is chosen per item, in this order of priority:

1. the `--approach` option of the `oldestdate` command;
2. the `album_type` option, if one of the album types (`albumtypes`/`albumtype`) of the item's album matches;
3. `singleton` for singletons, `compilation` for compilations, `album` for other albums.

Approach names are case-insensitive; `album` and `releases` are accepted as aliases of `release`, `singleton`
and `recordings` of `recording`, `works` of `work`.

# Configuration

|          Key           | Default Value |                                                                                       Description                                                                                       |
|:----------------------:|:-------------:|:---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------:|
|          auto          |     True      |                                                                         Run oldestdate during the import phase                                                                          |
|       singleton        |   recording   |                                                                      Approach used for singleton imports / items                                                                       |
|         album          |    release    |                                                                      Approach used for standard album imports                                                                         |
|      compilation       |   recording   |                                                                     Approach used for compilations (`comp` flag)                                                                      |
|       album_type       |      {}       |       Approach by album type, takes priority over `album` and `compilation`. Either a mapping (`{soundtrack: release, live: work}`) or a list of pairs (`[[soundtrack, release]]`)        |
|     overwrite_date     |     False     | Overwrite the date tag fields (year, month, day). `yes`/`no` for all approaches, or a list of approaches, e.g. `[release]`, so that only album dates are changed (useful for e.g. Plex)  |
|    overwrite_month     |     True      |                                                                 If overwriting date, also overwrite month field, otherwise leave blank                                                                 |
|     overwrite_day      |     True      |                                                                  If overwriting date, also overwrite day field, otherwise leave blank                                                                  |
|       prompt_for       |      []       |                     List of approaches for which found dates must be validated (once per album, or per singleton) before being applied, e.g. `[recording, work]`                     |
|    ignore_track_id     |     False     |                                       During import, ignore existing track_id. Needed if using plugin on a library already tagged by MusicBrainz                                       |
|    filter_on_import    |     True      |                                   During import, weight down candidates with no work_id so you are more likely to choose a recording with a work_id                                    |
| prompt_missing_work_id |     True      |                                   During import, prompt to fix work_id if missing from chosen recording. Only applies to items using the `work` approach                                   |
|         force          |     False     |                                                          Run even if `recording_` tags have already been applied to the track                                                          |
|   filter_recordings    |     True      |                                        `work` approach: skip recordings that have attributes (usually live recordings) |
|     release_types      |     None      |                     Only consider releases with given status, e.g. `['Official']`. Usually not needed, requires fetching the releases of each recording                     |
|     use_file_date      |     False     |                                                           Use the file's embedded date too when looking for the oldest date                                                            |
|  max_network_retries   |       3       |                                       Maximum amount of times a given network call will be retried, using exponential backoff, before giving up.                                       |

The MusicBrainz server, `https` and rate limit settings are taken from beets' `musicbrainz` configuration.

## Command

    beet oldestdate [-a {release,recording,work}] [-f] [QUERY]

`-a/--approach` forces the approach for all matched items, `-f/--force` processes items that were already processed.

## Example Configuration

    musicbrainz:
      searchlimit: 20
    plugins: oldestdate

    oldestdate:
      auto: yes
      singleton: recording
      album: release
      compilation: recording
      album_type:
        soundtrack: release
        live: release
      overwrite_date: [release]
      prompt_for: [work]
      ignore_track_id: yes
      filter_on_import: yes
      prompt_missing_work_id: yes
      filter_recordings: yes

## How the work approach works

The plugin will take the recording that was chosen and get its `work_id`. From this, it gets all recordings associated
with said work, and keeps the oldest first release date among them. The first release dates are provided by the
work's recording relations, so this usually needs only two API calls per track. Recordings with attributes (usually
live recordings) are skipped if `filter_recordings` is enabled.

### Missing work_id

This only applies to items processed with the `work` approach. If the chosen recording has no Work associated with it, the plugin cannot do its job. This is where `filter_on_import`
comes in: it applies a negative score to tracks that don't have an associated work so they are much less likely to be
chosen. However, this means some of the displayed tracks will be irrelevant. Thus, setting the `searchlimit` to 20 or so
tracks is needed to hit the one recording that *does* have a work. This happens to work quite well with famous songs
because there is usually a single recording with an associated work that is the original recording, and thus the oldest.
If we match with this one, the other recordings that we can't get to because they are not associated with the same work
are irrelevant, because we already have the oldest date.

However, it sometimes happens that there is no available recording that matches our track with an associated work. This
is what `prompt_missing_work_id` is for: it will prompt us to either just use the single matched recording, in which
case only the matched recording's date is used, or we can try again, or skip the
track (or album). Trying again is so that we may go to the website and amend the data, so that the recordings will have an
associated work. To help with this process, the plugin prints out a URL to a search for that specific track. Your task
is to create a work and associate it with all the relevant recordings, then press try again. This can be quite a
laborious task, so if we see that the date printed by the plugin as being the oldest date found with just the selected
recording seems accurate, choosing `Use this recording` would be the best choice.

### Covers

The plugin is also programmed to deal with covers effectively. Because a `work` actually contains both the recordings of
a song by the original author and any cover artists, when the song we are processing is not a cover, any recordings
tagged as covers are discarded, to save API calls. Conversely, if the processed song *is* a cover, then we only keep
cover recordings, and filter them by author, so only the relevant recordings are kept. This is so the oldest date for a
cover will be the oldest date in which that cover was made, and not the original song. This only applies to
the `work` approach, where cover recordings are fetched to get the author data.

## Tests

    python -m unittest discover -s tests

Tests use offline fixtures modelled on MusicBrainz JSON responses. Set `OLDESTDATE_ONLINE_TESTS=1` to also run the tests against the live
MusicBrainz API.
