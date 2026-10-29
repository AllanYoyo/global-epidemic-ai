# -*- coding: utf-8 -*-
"""抓取状态与快照存储 —— 移植自 urlwatch 2.29(thp/urlwatch master) 的存储抽象。

来源: urlwatch-master/lib/urlwatch/storage.py:454-502(CacheStorage 抽象)
      :559-576(minidb 的 CacheEntry + CacheMiniDBStorage —— **实现不移植**)

实现替换: minidb → sqlite3(stdlib)。理由: minidb 是第三方依赖, 会破坏「不加硬依赖」
红线; 且本项目已有 SQLite 运维口径(epidemic.db), 同技术栈少一套心智负担。

**与上游的关键差异: 状态粒度从「作业」细化到「URL」。**
  上游以 guid(=作业位置) 为缓存主键; 本项目一个源要展开成「入口页 + N 篇候选
  文章页」, 条件请求的前提是「同一 URL 上轮抓过」—— 源粒度记不住文章页的
  ETag, 所以 fetch_state 以 URL 为主键。

建表方式照 normalize.py open_db() 的既有模式: 幂等建表 + 缺列 ALTER TABLE,
无独立迁移脚本(见 ARCHITECTURE.md §3「迁移方式」)。
"""
import logging
import os
import sqlite3
import threading

logger = logging.getLogger(__name__)

# 单个 URL 保留的历史快照数(超过则裁掉最旧的)
DEFAULT_RETAIN_LIMIT = 3


class CacheStorage(object):
    """抓取缓存抽象(移植自 urlwatch storage.py:454-502 的接口形状)。"""

    def load_state(self, url):
        """取 URL 的抓取状态 dict(无则 None)。"""
        raise NotImplementedError()

    def save_state(self, url, **fields):
        """写回 URL 的抓取状态。"""
        raise NotImplementedError()

    def load_body(self, url):
        """取 URL 最近一版快照 (body, sha1, timestamp); 无则 (None, None, None)。"""
        raise NotImplementedError()

    def save_body(self, url, content_sha1, body, timestamp):
        """写入一版快照。"""
        raise NotImplementedError()

    def history(self, url, count=1):
        """取最近 count 版快照 [(body, timestamp), ...](新→旧)。"""
        raise NotImplementedError()

    def get_urls(self):
        """已知 URL 列表。"""
        raise NotImplementedError()

    def delete(self, url):
        """删除某 URL 的全部记录。"""
        raise NotImplementedError()

    def clean(self, url, retain_limit=DEFAULT_RETAIN_LIMIT):
        """裁掉旧快照, 返回删除条数。"""
        raise NotImplementedError()

    def close(self):
        raise NotImplementedError()


class SQLiteCacheStorage(CacheStorage):
    """SQLite 实现的抓取缓存(独立库文件, 不碰 epidemic.db)。"""

    def __init__(self, filename):
        self.filename = filename
        parent = os.path.dirname(filename)
        if parent:
            os.makedirs(parent, exist_ok=True)

        # 多线程共用一个连接(check_same_thread=False), 写操作走锁串行化
        self.conn = sqlite3.connect(filename, check_same_thread=False)
        self._lock = threading.Lock()
        self._ensure_schema()

    def _ensure_schema(self):
        with self._lock:
            cur = self.conn.cursor()
            cur.execute(
                "CREATE TABLE IF NOT EXISTS fetch_state ("
                " url TEXT PRIMARY KEY, source_id TEXT, etag TEXT, last_modified TEXT,"
                " content_sha1 TEXT, last_status INTEGER, last_fetched REAL,"
                " last_archived INTEGER DEFAULT 0, tries INTEGER DEFAULT 0)"
            )
            cur.execute(
                "CREATE TABLE IF NOT EXISTS page_snapshot ("
                " url TEXT, fetched_at REAL, content_sha1 TEXT, body TEXT,"
                " PRIMARY KEY (url, fetched_at))"
            )
            cur.execute("CREATE INDEX IF NOT EXISTS idx_snapshot_url ON page_snapshot(url, fetched_at DESC)")
            # 缺列补齐(幂等迁移, 照 normalize.open_db() 的模式)
            for table, col, decl in (
                ("fetch_state", "source_id", "TEXT"),
                ("fetch_state", "etag", "TEXT"),
                ("fetch_state", "last_modified", "TEXT"),
                ("fetch_state", "content_sha1", "TEXT"),
                ("fetch_state", "last_status", "INTEGER"),
                ("fetch_state", "last_fetched", "REAL"),
                ("fetch_state", "last_archived", "INTEGER DEFAULT 0"),
                ("fetch_state", "tries", "INTEGER DEFAULT 0"),
            ):
                cols = {r[1] for r in cur.execute("PRAGMA table_info(%s)" % table).fetchall()}
                if col not in cols:
                    cur.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, col, decl))
            self.conn.commit()

    def load_state(self, url):
        with self._lock:
            row = self.conn.execute(
                "SELECT source_id, etag, last_modified, content_sha1, last_status,"
                " last_fetched, last_archived, tries FROM fetch_state WHERE url = ?",
                (url,)).fetchone()
        if row is None:
            return None
        return {
            'source_id': row[0],
            'etag': row[1],
            'last_modified': row[2],
            'content_sha1': row[3],
            'last_status': row[4],
            'last_fetched': row[5],
            'last_archived': row[6],
            'tries': row[7],
        }

    def save_state(self, url, **fields):
        current = self.load_state(url) or {}
        merged = dict(current)
        merged.update({k: v for k, v in fields.items() if v is not None or k in ('etag', 'last_modified')})
        with self._lock:
            self.conn.execute(
                "INSERT INTO fetch_state (url, source_id, etag, last_modified, content_sha1,"
                " last_status, last_fetched, last_archived, tries)"
                " VALUES (?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(url) DO UPDATE SET source_id=excluded.source_id, etag=excluded.etag,"
                " last_modified=excluded.last_modified, content_sha1=excluded.content_sha1,"
                " last_status=excluded.last_status, last_fetched=excluded.last_fetched,"
                " last_archived=excluded.last_archived, tries=excluded.tries",
                (url, merged.get('source_id'), merged.get('etag'), merged.get('last_modified'),
                 merged.get('content_sha1'), merged.get('last_status'), merged.get('last_fetched'),
                 merged.get('last_archived') or 0, merged.get('tries') or 0))
            self.conn.commit()

    def load_body(self, url):
        with self._lock:
            row = self.conn.execute(
                "SELECT body, content_sha1, fetched_at FROM page_snapshot"
                " WHERE url = ? ORDER BY fetched_at DESC LIMIT 1", (url,)).fetchone()
        if row is None:
            return None, None, None
        return row[0], row[1], row[2]

    def save_body(self, url, content_sha1, body, timestamp):
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO page_snapshot (url, fetched_at, content_sha1, body)"
                " VALUES (?,?,?,?)", (url, timestamp, content_sha1, body))
            self.conn.commit()

    def history(self, url, count=1):
        with self._lock:
            rows = self.conn.execute(
                "SELECT body, fetched_at FROM page_snapshot WHERE url = ?"
                " ORDER BY fetched_at DESC LIMIT ?", (url, int(count))).fetchall()
        return list(rows)

    def get_urls(self):
        with self._lock:
            rows = self.conn.execute("SELECT url FROM fetch_state").fetchall()
        return [r[0] for r in rows]

    def delete(self, url):
        with self._lock:
            self.conn.execute("DELETE FROM fetch_state WHERE url = ?", (url,))
            self.conn.execute("DELETE FROM page_snapshot WHERE url = ?", (url,))
            self.conn.commit()

    def clean(self, url, retain_limit=DEFAULT_RETAIN_LIMIT):
        if retain_limit < 1:
            raise ValueError('快照保留数至少为 1(收到 %r)' % (retain_limit,))
        with self._lock:
            cur = self.conn.execute(
                "DELETE FROM page_snapshot WHERE url = ? AND fetched_at NOT IN ("
                " SELECT fetched_at FROM page_snapshot WHERE url = ?"
                " ORDER BY fetched_at DESC LIMIT ?)", (url, url, int(retain_limit)))
            removed = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
            self.conn.commit()
        return removed

    def close(self):
        with self._lock:
            try:
                self.conn.close()
            except sqlite3.Error:
                pass
