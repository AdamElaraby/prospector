# Host Prospector

Deploy the included Dockerfile as one private, single-owner web service on a host that supports Linux containers, HTTPS, and a persistent disk. Once deployed, open the HTTPS address in your browser. The server runs Find, Audit, and the optional pipeline steps.

1. Mount a persistent disk at `/data`. Make it writable by container UID 10001.
2. Set `PROSPECTOR_PUBLIC_ORIGIN` to the full HTTPS origin (for example, `https://prospector.example.com`, with no trailing slash). Set `PROSPECTOR_WEB_PASSWORD` to a random password of at least 20 characters through your host's secrets settings.
3. Route HTTPS traffic to the container's `PORT` (8080 by default). Keep the container port behind your host's HTTPS proxy. Use a TCP health check or supply authentication for an HTTP health check.
4. Open the HTTPS URL and sign in with username `prospector` and the configured password.
5. For an existing installation, stop writes to the old database and transfer a SQLite backup to `/data/prospector.db` privately. Copy your `config.local.yaml` to the disk if you want to reuse your settings. If it names a custom database path, update it to match the backup on the disk. Set service keys through the host's secrets settings or the site's Setup tab.

Search can use Chromium on the server. If Google Maps blocks requests from your host, choose Google Places API in Setup and add its API key. Use one replica because job progress is held in server memory.
