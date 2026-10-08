# LRCLIB Lyrics for MusicBrainz Picard 3

This is the Picard Plugin API v3 edition of LRCLIB Lyrics.

Version 2.0.1 fixes configuration loading on Picard 3.0.1.

## Install in Picard 3.0+

1. Extract the supplied ZIP to a permanent folder.
2. In Picard, open **Options > Options > Plugins**.
3. Choose **Install plugin > Local repository**.
4. Select the extracted `picard-plugin-lrclib-lyrics` folder.
5. Enable **LRCLIB Lyrics**, then open its settings and confirm your options.

Picard 3 installs plugins from Git repositories rather than individual `.py` files. The supplied folder is already a local Git repository.

## Queue logging

The plugin logs queue startup, retry notices, progress every 25 requests, a per-album completion summary, and a final queue completion summary.
