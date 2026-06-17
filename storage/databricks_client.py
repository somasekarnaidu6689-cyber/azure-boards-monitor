import logging
from databricks import sql as dbsql
from config import Config

logger = logging.getLogger(__name__)


def get_connection():
    """
    Return an open Databricks SQL connector connection.
    Caller is responsible for closing it (use as context manager).
    """
    return dbsql.connect(
        server_hostname=Config.DATABRICKS_SERVER_HOSTNAME,
        http_path=Config.DATABRICKS_HTTP_PATH,
        access_token=Config.DATABRICKS_ACCESS_TOKEN,
    )


def execute(conn, sql: str, parameters=None) -> None:
    """Execute a single DDL or DML statement."""
    with conn.cursor() as cur:
        if parameters:
            cur.execute(sql, parameters)
        else:
            cur.execute(sql)


def executemany(conn, sql: str, rows: list[tuple]) -> None:
    """Bulk-insert a list of row tuples using executemany."""
    if not rows:
        return
    with conn.cursor() as cur:
        cur.executemany(sql, rows)