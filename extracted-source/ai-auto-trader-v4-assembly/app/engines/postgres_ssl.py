"""TLS defaults for server-side PostgreSQL connections."""
from __future__ import annotations

from psycopg.conninfo import conninfo_to_dict

_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
_TLS_MODES = {"require", "verify-ca", "verify-full"}


def postgres_connection_ssl_options(dsn: str) -> dict[str, str]:
    """Require TLS for remote PostgreSQL while leaving disposable local tests usable.

    An explicitly stronger SSL mode (verify-ca or verify-full) is preserved.
    Remote connections that omit sslmode, or request disable/allow/prefer, are
    upgraded to require so credentials and paper state are not sent in plaintext.
    """
    parameters = conninfo_to_dict(dsn)
    hosts = [host.strip().lower() for host in parameters.get("host", "localhost").split(",") if host.strip()]
    if not hosts:
        hosts = ["localhost"]
    if all(host in _LOCAL_HOSTS for host in hosts):
        return {}
    selected_mode = parameters.get("sslmode", "prefer").lower()
    if selected_mode in _TLS_MODES:
        return {}
    return {"sslmode": "require"}
