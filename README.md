# Letterboxderr

<p align="center">
  <img src="static/logo.png" alt="Letterboxderr" width="200">
</p>

Sync your [Letterboxd](https://letterboxd.com) watchlist to [Seerr](https://github.com/seerr-team/seerr). Also works with [Jellyseerr](https://github.com/Fallenbagel/jellyseerr) and [Overseerr](https://github.com/sct/overseerr).

Movies and TV shows from your Letterboxd watchlist are automatically added to your Seerr watchlist, so you can browse and request them whenever you're ready.

**Compatibility:** Letterboxderr needs Seerr's watchlist API (`POST /api/v1/watchlist`), which Seerr and Jellyseerr provide. Overseerr's watchlist is synced from Plex and may not accept items from Letterboxderr.

**Signing in:** the web UI accepts Plex accounts, Jellyfin/Emby accounts and local Seerr accounts (email and password). Plex sign-in needs the Letterboxderr server to reach `plex.tv`.

## Quick start

### Docker

```bash
docker run -d \
  --name letterboxderr \
  --restart unless-stopped \
  -p 8484:8484 \
  -e SEERR_URL=http://your-seerr:5055 \
  -e SEERR_API_KEY=your-api-key \
  -v /path/to/data:/data \
  -e PUID=1000 -e PGID=1000 \
  ghcr.io/meduseld-io/letterboxderr:latest
```

Open `http://localhost:8484`, sign in with your Seerr credentials, and link your Letterboxd username.

### Docker Compose

```yaml
services:
  letterboxderr:
    image: ghcr.io/meduseld-io/letterboxderr:latest
    container_name: letterboxderr
    restart: unless-stopped
    ports:
      - "8484:8484"
    volumes:
      - /path/to/data:/data
    environment:
      - SEERR_URL=http://your-seerr:5055
      - SEERR_API_KEY=your-api-key
      - SYNC_INTERVAL=3600
      - PUID=1000
      - PGID=1000
```

`SEERR_URL` is resolved from inside the container, so `localhost` won't reach Seerr. Use the Seerr service name on a shared Docker network, or the host's IP.

## How it works

1. Sign in with your Seerr account
2. Enter your Letterboxd username
3. Letterboxderr scrapes your public Letterboxd watchlist page
4. Each movie or TV show is matched to a TMDb ID from the film page (looked up once, then cached)
5. New items are added to your Seerr watchlist
6. Background sync runs automatically (configurable interval); linking an account starts a sync straight away

### Sync options

Each user can turn these on in the web UI (or per user in `config.json` for CLI mode):

- **Letterboxd lists:** sync films from any public Letterboxd list along with the watchlist. Paste the list's URL.
- **Also request new titles:** titles added to Letterboxd after this is turned on are requested in Seerr as that user, not just added to their watchlist. Seerr's permissions, quotas and auto-approve settings for the user still apply. The first sync with it on only records what's already there, so turning it on never requests a whole existing watchlist. Server admins can disable it with `ALLOW_AUTO_REQUEST=false`.
- **Remove titles I remove from Letterboxd:** titles Letterboxderr added to the Seerr watchlist are removed once they're no longer on the watchlist or any synced list. Titles you added in Seerr yourself are never touched, and requests are never cancelled. If a sync finds no films at all, removals are skipped in case Letterboxd's page changed.

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `SEERR_URL` | `http://localhost:5055` | Your Seerr/Jellyseerr internal URL |
| `SEERR_API_KEY` | | API key from Seerr Settings > General |
| `SEERR_PUBLIC_URL` | | Public URL for the "Open Seerr" button (e.g. `https://requests.example.com`) |
| `SYNC_INTERVAL` | `3600` | Seconds between auto-syncs (0 = manual only) |
| `FIRST_SYNC_DELAY` | `60` | Seconds after startup before the first background sync |
| `DRY_RUN` | `false` | Log what would be added, requested or removed without changing Seerr or the sync state |
| `ALLOW_AUTO_REQUEST` | `true` | Let users turn on auto-request |
| `MAX_LISTS` | `10` | Most Letterboxd lists one user can sync |
| `PLEX_CLIENT_ID` | derived from `SEERR_URL` | Identifier this install uses when signing in with Plex |
| `WEB_PORT` | `8484` | Port for the web UI |
| `WEB_HOST` | `0.0.0.0` | Address the web UI listens on |
| `COOKIE_SECURE` | `false` | Set to `true` when serving the UI over HTTPS |
| `PUID` / `PGID` | `1000` | User and group the container runs as; owns the files in `/data` |
| `STATE_FILE` | `state.json` (`/data/state.json` in Docker) | Sync state and cached film lookups |
| `USERS_FILE` | `users.json` (`/data/users.json` in Docker) | Linked accounts and last sync results |
| `CONFIG_FILE` | `config.json` | CLI mode config file |

Environment variables take precedence over values in `config.json`.

## CLI mode

For headless/cron usage without the web UI. The core sync engine (`letterboxderr.py`) can run standalone with a JSON config file - no Flask or browser needed. The Docker image runs the web UI (`web.py`) by default, which imports the sync engine and wraps it with auth, caching, and a browser interface.

```bash
pip install -r requirements.txt
cp config.example.json config.json
# Edit config.json

# Run continuously - syncs on startup, then repeats every SYNC_INTERVAL seconds
python letterboxderr.py

# Run once and exit - good for cron jobs
python letterboxderr.py --once
```

Each user in `config.json` can also set `lists` (list URLs or `owner/list-name`), `auto_request` and `remove_missing`. See `config.example.json`.

## Development

```bash
pip install -r requirements-dev.txt
ruff check .
pytest
```

## Finding your Seerr API key

1. Open Seerr
2. Go to Settings > General
3. Copy the API key

## Contributing

Letterboxderr is open source under the MIT License.

Contributions are welcome - feel free to open issues or submit pull requests on [GitHub](https://github.com/meduseld-io/letterboxderr).

Letterboxderr is developed and maintained by [@quietarcade](https://github.com/quietarcade) as part of [Meduseld](https://github.com/meduseld-io).

## License

MIT
