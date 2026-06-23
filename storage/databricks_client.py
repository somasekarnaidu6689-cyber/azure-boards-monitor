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

def fetch_latest_snapshot(work_item_ids: list[int]) -> dict[int, dict]:
    """
    For each work item ID, fetch the most recent snapshot row regardless
    of quality. Used to check if the latest comment is already good.
 
    Returns dict of work_item_id -> {
        "latest_date": date str "YYYY-MM-DD",
        "latest_score": int,
        "is_good": bool,  # meets all good comment criteria
    }
    """
    if not work_item_ids:
        return {}
 
    fq = Config.db_table("TaskDailySnapshot")
    threshold = Config.EMAIL_GOOD_QUALITY_THRESHOLD
    ids_str = ", ".join(str(i) for i in work_item_ids)
 
    sql = f"""
        SELECT
            work_item_id,
            snapshot_date,
            comment_quality_score,
            eod_compliant,
            copy_paste_detected
        FROM {fq}
        WHERE work_item_id IN ({ids_str})
            AND eod_compliant = true
            AND comment_quality_score IS NOT NULL
            AND snapshot_date = (
                SELECT MAX(inner_t.snapshot_date)
                FROM {fq} AS inner_t
                WHERE inner_t.work_item_id = {fq}.work_item_id
                    AND inner_t.eod_compliant = true
                    AND inner_t.comment_quality_score IS NOT NULL
            )
    """
 
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql)
                rows = cur.fetchall()
 
        result: dict[int, dict] = {}
        for row in rows:
            wid = int(row[0])
            score = int(row[2])
            eod_compliant = bool(row[3])
            copy_paste = bool(row[4])
            is_good = eod_compliant and not copy_paste and score >= threshold
            result[wid] = {
                "latest_date": str(row[1]),
                "latest_score": score,
                "is_good": is_good,
            }
        return result
 
    except Exception as exc:
        logger.warning(
            "fetch_latest_snapshot failed — treating all quality gate tasks "
            "as unverified: %s", exc
        )
        return {}


def fetch_last_good_scores(work_item_ids: list[int]) -> dict[int, dict]:
    """
    For each work item ID in the quality gate state, look up the most
    recent snapshot row where:
      - eod_compliant = true  (comment was added that day)
      - copy_paste_detected = false
      - comment_quality_score >= EMAIL_GOOD_QUALITY_THRESHOLD

    Returns dict of work_item_id -> {
        "last_good_date": date str "YYYY-MM-DD",
        "last_good_score": int,
    }

    Work items with no prior good score are absent from the dict.
    If Databricks is unavailable or the table doesn't exist yet,
    returns an empty dict so the pipeline degrades gracefully.
    """
    if not work_item_ids:
        return {}

    fq = Config.db_table("TaskDailySnapshot")
    threshold = Config.EMAIL_GOOD_QUALITY_THRESHOLD
    ids_str = ", ".join(str(i) for i in work_item_ids)

    sql = f"""
        SELECT
            work_item_id,
            MAX(snapshot_date)          AS last_good_date,
            MAX(comment_quality_score)  AS last_good_score
        FROM {fq}
        WHERE work_item_id IN ({ids_str})
          AND eod_compliant = true
          AND copy_paste_detected = false
          AND comment_quality_score >= {threshold}
        GROUP BY work_item_id
    """

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql)
                rows = cur.fetchall()

        result: dict[int, dict] = {}
        for row in rows:
            wid        = int(row[0])
            good_date  = str(row[1])   # "YYYY-MM-DD"
            good_score = int(row[2])
            result[wid] = {
                "last_good_date":  good_date,
                "last_good_score": good_score,
            }
        return result

    except Exception as exc:
        logger.warning(
            "fetch_last_good_scores failed (table may not exist yet) — "
            "treating all quality gate tasks as unverified: %s", exc
        )
        return {}