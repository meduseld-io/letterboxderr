# Letterboxderr

<p align="center">
  <img src="static/logo.png" alt="Letterboxderr" width="200">
</p>

Sync your [Letterboxd](https://letterboxd.com) watchlist to [Seerr](https://github.com/seerr-team/seerr). Also works with [Jellyseerr](https://github.com/Fallenbagel/jellyseerr) and [Overseerr](https://github.com/sct/overseerr).

Movies and TV shows from your Letterboxd watchlist are automatically added to your Seerr watchlist, so you can browse and request them whenever you're ready.

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
```

## How it works

1. Sign in with your Seerr account
2. Enter your Letterboxd username
3. Letterboxderr scrapes your public Letterboxd watchlist page
4. Each movie or TV show is matched to a TMDb ID from the film page
5. New items are added to your Seerr watchlist
6. Background sync runs automatically (configurable interval)

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `SEERR_URL` | `http://localhost:5055` | Your Seerr/Jellyseerr/Overseerr internal URL |
| `SEERR_API_KEY` | | API key from Seerr Settings > General |
| `SEERR_PUBLIC_URL` | | Public URL for the "Open Seerr" button (e.g. `https://requests.example.com`) |
| `SYNC_INTERVAL` | `3600` | Seconds between auto-syncs (0 = manual only) |
| `WEB_PORT` | `8484` | Port for the web UI |
| `DRY_RUN` | `false` | Log without making changes |

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
