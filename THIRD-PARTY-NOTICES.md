# Third-party components

WEB PANEL PROXY distributes the following unmodified upstream Linux binaries:

- NaiveProxy Caddy `v2.11.7-naive2`. The bundled archive contains the upstream license and README. Source: https://github.com/klzgrad/naiveproxy/tree/v2.11.7-naive2
- Mieru `mita` `v3.39.0`, licensed under GNU GPL v3. The complete corresponding upstream source, license and build instructions are bundled as `assets/mieru-v3.39.0-source.tar.gz`. Upstream: https://github.com/enfein/mieru/tree/v3.39.0

The files are installed as separate services and are not linked into WEB PANEL PROXY.

The package also includes the public MTProxy bootstrap files from
`https://core.telegram.org/getProxySecret` and `https://core.telegram.org/getProxyConfig`,
retrieved over verified HTTPS on 2026-10-31. These public protocol files are used
only when the normal download fails; they are not user access credentials.
Their expected SHA-256 values are recorded in `mtproxy-download.sh`.
